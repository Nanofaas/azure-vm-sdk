"""End-to-end verification of the VM lifecycle.

Creates VM(s), waits for them to become reachable, runs a command over SSH
and deletes them again.

Prerequisites:
    az login
    tofu installed on PATH
    AZURE_RESOURCE_GROUP and AZURE_LOCATION env vars set

Usage:
    uv run azure-vm-e2e
    uv run azure-vm-e2e --name my-test-vm --vm-size Standard_D2s_v3 --timeout 300
    uv run azure-vm-e2e --count 3
    uv run azure-vm-e2e --count 2 --name worker --vm-size Standard_B2s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .client import AzureClient
from .exceptions import AzureVmError
from .models import VmConfig
from .vm import AzureVM


def _env_or_raise(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def _verify_vm(vm: AzureVM, idx: int, total: int, timeout: float) -> int:
    """Run IP/SSH/exec checks on a single VM. Returns 0 on success, 1 on failure."""
    label = f"  [{idx}/{total}] {vm.name}"
    exit_code = 0

    print(f"{label}: waiting for public IP ...")
    t0 = time.monotonic()
    try:
        ip = vm.wait_for_ip(timeout=timeout)
    except AzureVmError as exc:
        print(f"{label}: IP timeout — {exc}", file=sys.stderr)
        return 1
    print(f"{label}: got IP {ip} in {time.monotonic() - t0:.1f}s")

    print(f"{label}: waiting for SSH on {ip}:22 ...")
    t0 = time.monotonic()
    try:
        vm.wait_ready(timeout=timeout, port=22)
    except AzureVmError as exc:
        print(f"{label}: SSH timeout — {exc}", file=sys.stderr)
        return 1
    print(f"{label}: SSH ready in {time.monotonic() - t0:.1f}s")

    print(f"{label}: running verification command ...")
    result = vm.exec(["uname", "-a"])
    print(f"{label}: exit={result.returncode}  stdout={result.stdout.strip()}")
    if result.stderr:
        print(f"{label}: stderr={result.stderr.strip()}")
    if not result.success:
        print(f"{label}: FAILED — non-zero exit code", file=sys.stderr)
        exit_code = 1

    info = vm.info()
    print(
        f"{label}: state={info.state.value}  location={info.location}  "
        f"resource_group={info.resource_group}"
    )
    return exit_code


def main() -> None:
    """Run the full lifecycle check for the VMs named on the command line.

    Always exits through ``SystemExit``: 0 on success, 1 when a VM failed
    verification, 2 when an ``AzureVmError`` aborted the run, 3 when cleanup
    could not delete a VM, and 130 when interrupted.
    """
    parser = argparse.ArgumentParser(
        description="End-to-end VM lifecycle test (create, verify, delete)."
    )
    parser.add_argument(
        "--name",
        default=None,
        help="VM name; used as prefix when --count > 1 (auto-generated if omitted).",
    )
    parser.add_argument(
        "--vm-size",
        default="Standard_B1s",
        help="Azure VM size (default: Standard_B1s).",
    )
    parser.add_argument(
        "--image-urn",
        default=None,
        help="Azure image URN (publisher:offer:sku:version).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="Max seconds to wait for SSH readiness (default: 300).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of identical VMs to create in parallel (default: 1).",
    )
    parser.add_argument(
        "--configs",
        help=(
            "JSON array of VM configs, e.g. "
            '\'[{"name":"web","vm_size":"Standard_B1s"},'
            '{"name":"db","vm_size":"Standard_D2s_v3"}]\'. '
            "Mutually exclusive with --count/--name/--vm-size/--image-urn."
        ),
    )
    parser.add_argument(
        "--list-sizes",
        action="store_true",
        help="List available VM sizes in the configured region and exit.",
    )
    args = parser.parse_args()

    if args.configs and args.count != 1:
        raise SystemExit("--configs and --count are mutually exclusive")
    if args.count < 1:
        raise SystemExit("--count must be at least 1")

    resource_group = _env_or_raise("AZURE_RESOURCE_GROUP")
    location = _env_or_raise("AZURE_LOCATION")
    ssh_key_path = os.environ.get("AZURE_SSH_PUBLIC_KEY")

    client = AzureClient(
        resource_group=resource_group,
        location=location,
        ssh_key_path=ssh_key_path,
    )

    if args.list_sizes:
        sizes = client.list_sizes()
        print(f"Available VM sizes in {location}:")
        print(
            f"{'Name':<24} {'Cores':>6} {'RAM (MB)':>10} {'OS Disk (MB)':>14} "
            f"{'Data Disks':>11}"
        )
        print("-" * 70)
        for s in sizes:
            print(
                f"{s.name:<24} {s.number_of_cores:>6} {s.memory_in_mb:>10} "
                f"{s.os_disk_size_in_mb:>14} {s.max_data_disk_count:>11}"
            )
        raise SystemExit(0)

    # --- build configs -------------------------------------------------------
    if args.configs:
        try:
            raw = json.loads(args.configs)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--configs: invalid JSON — {exc}") from exc
        if not isinstance(raw, list) or not raw:
            raise SystemExit("--configs: expected a non-empty JSON array")
        valid_fields = set(VmConfig.__dataclass_fields__)
        configs = []
        for i, item in enumerate(raw):
            unknown = set(item) - valid_fields
            if unknown:
                raise SystemExit(f"--configs[{i}]: unknown fields {sorted(unknown)}")
            configs.append(VmConfig(**item))
    else:
        prefix = args.name or f"e2e-{int(time.time())}"
        if args.count == 1:
            configs = [
                VmConfig(name=prefix, vm_size=args.vm_size, image_urn=args.image_urn)
            ]
        else:
            configs = [
                VmConfig(
                    name=f"{prefix}-{i}", vm_size=args.vm_size, image_urn=args.image_urn
                )
                for i in range(args.count)
            ]

    names = [cfg.name for cfg in configs]
    n = len(configs)
    vms: list[AzureVM] = []
    exit_code = 0

    try:
        # --- launch ----------------------------------------------------------
        if n == 1:
            print(f"[1/3] Launching VM '{names[0]}' (size={args.vm_size}) ...")
        else:
            print(f"[1/3] Launching {n} VMs in parallel (size={args.vm_size}) ...")
            for name in names:
                print(f"       - {name}")
        t0 = time.monotonic()
        vms = client.launch_many(configs)
        dt = time.monotonic() - t0
        print(
            f"       {'launch' if n == 1 else 'all ' + str(n) + ' launches'}"
            f" completed in {dt:.1f}s"
        )

        # --- verify each VM --------------------------------------------------
        print(f"[2/3] Verifying {n} VM(s) ...")
        for i, vm in enumerate(vms, start=1):
            rc = _verify_vm(vm, i, n, args.timeout)
            if rc != 0:
                exit_code = 1

    except AzureVmError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        exit_code = 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        exit_code = 130

    # --- cleanup -------------------------------------------------------------
    if n == 1:
        print(f"[3/3] Deleting VM '{names[0]}' ...")
    else:
        print(f"[3/3] Deleting {len(vms)} VM(s) ...")
    try:
        for vm in vms:
            vm.delete()
        print("       done.")
    except AzureVmError as exc:
        print(f"       cleanup failed: {exc}", file=sys.stderr)
        exit_code = 3

    print()
    vm_label = f"VM '{names[0]}'" if n == 1 else f"{len(vms)}/{n} VMs"
    if exit_code == 0:
        print(f"SUCCESS — {vm_label} completed full lifecycle.")
    else:
        print(f"FAILURE (exit code {exit_code}) — see errors above.")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
