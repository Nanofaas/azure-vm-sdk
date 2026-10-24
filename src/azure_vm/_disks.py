"""Track SDK OS disks across refresh, replacement and external VM eviction."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ._backend import CommandBackend, CommandResult, run_command
from ._reconcile import reconcile_vm
from .exceptions import AzureVmCommandError, AzureVmError

LEDGER = "sdk-os-disks.json"
_DISK_ID = re.compile(
    r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Compute/disks/[^/]+",
    re.IGNORECASE,
)


def _read(workspace: Path) -> dict[str, str | None]:
    path = workspace / LEDGER
    if not path.exists():
        return {}
    disks = json.loads(path.read_text())
    if not isinstance(disks, dict) or any(
        not _DISK_ID.fullmatch(key)
        or (value is not None and (not isinstance(value, str) or not value))
        for key, value in disks.items()
    ):
        raise AzureVmError(f"Invalid OS disk registry: {path}")
    return disks


def _save(workspace: Path, disks: dict[str, str | None]) -> None:
    # Atomic replacement keeps a failed cleanup retryable after process exit.
    temporary = workspace / (LEDGER + ".tmp")
    temporary.write_text(json.dumps(disks, indent=2) + "\n")
    temporary.replace(workspace / LEDGER)


def _disk(backend: CommandBackend, disk_id: str) -> dict | None:
    args = ["az", "disk", "show", "--ids", disk_id, "-o", "json"]
    result = backend.run(args)
    if not result.success:
        if re.search(
            r"^ERROR: \((?:NotFound|ResourceNotFound|ResourceGroupNotFound)\)",
            result.stderr,
            re.MULTILINE,
        ):
            return None
        raise AzureVmCommandError(args, result.returncode, result.stdout, result.stderr)
    data = json.loads(result.stdout)
    if (
        not isinstance(data, dict)
        or str(data.get("id", "")).lower() != disk_id.lower()
        or not isinstance(data.get("uniqueId"), str)
        or not data["uniqueId"]
    ):
        raise AzureVmError(f"Cannot verify OS disk identity: {disk_id}")
    return data


def remember_os_disks(
    workspace: Path, backend: CommandBackend
) -> dict[str, str | None]:
    """Save exact managed OS disk IDs from local state before refresh forgets them."""
    disks = _read(workspace)
    current_ids: set[str] = set()
    state = workspace / "terraform.tfstate"
    if state.exists():
        for resource in json.loads(state.read_text()).get("resources", []):
            if (resource.get("mode"), resource.get("type"), resource.get("name")) != (
                "managed",
                "azurerm_linux_virtual_machine",
                "vm",
            ) or resource.get("module"):
                continue
            for instance in resource.get("instances", []):
                attributes = instance.get("attributes", {})
                for os_disk in attributes.get("os_disk", []):
                    if os_disk.get("diff_disk_settings"):
                        continue
                    disk_id = os_disk.get("id")
                    if not disk_id:
                        # AzureRM before 4.15 exports the exact name, but not ID.
                        vm_id = attributes.get("id", "")
                        disk_name = os_disk.get("name", "")
                        parent, separator, _ = vm_id.rpartition("/virtualMachines/")
                        if not separator or not disk_name or "/" in disk_name:
                            raise AzureVmError(f"Cannot identify OS disk in {state}")
                        disk_id = f"{parent}/disks/{disk_name}"
                    if not isinstance(disk_id, str) or not _DISK_ID.fullmatch(disk_id):
                        raise AzureVmError(f"Invalid OS disk ID in {state}")
                    disks.setdefault(disk_id, None)
                    current_ids.add(disk_id)
    if disks:
        # Save before remote queries too: transient CLI failures must not lose IDs.
        _save(workspace, disks)
        for disk_id, identity in disks.items():
            if identity is None and disk_id in current_ids:
                data = _disk(backend, disk_id)
                # A proven absence must never adopt a future same-name disk.
                disks[disk_id] = data["uniqueId"] if data is not None else "<absent>"
                _save(workspace, disks)
    return disks


def apply_with_disk_tracking(
    workspace: Path, backend: CommandBackend, args: list[str]
) -> CommandResult:
    """Retain disks from both generations, including partially failed applies."""
    remember_os_disks(workspace, backend)
    try:
        result = run_command(backend, args, cwd=str(workspace))
    except BaseException:
        try:
            remember_os_disks(workspace, backend)
        except Exception:
            logging.getLogger(__name__).exception("OS disk tracking failed after apply")
        raise
    remember_os_disks(workspace, backend)
    return result


def destroy_with_disk_cleanup(
    workspace: Path, backend: CommandBackend, *, vm_name: str | None = None
) -> None:
    """Destroy a VM and delete only its recorded, detached, unchanged OS disks.

    Errors leave the unresolved entries on disk for a later delete/purge call.
    No subscription-wide search or name-prefix matching is performed.
    """
    disks = remember_os_disks(workspace, backend)
    name = vm_name if vm_name is not None else workspace.name
    if reconcile_vm(workspace, backend, name):
        disks = remember_os_disks(workspace, backend)
    try:
        run_command(backend, ["tofu", "destroy", "-auto-approve"], cwd=str(workspace))
    except AzureVmCommandError as exc:
        # An asynchronous Azure creation can attach the NIC after the first
        # lookup. Recover once on this specific failure, never loop blindly.
        if "NicInUse" not in exc.stderr + exc.stdout or not reconcile_vm(
            workspace, backend, name
        ):
            raise
        disks = remember_os_disks(workspace, backend)
        run_command(backend, ["tofu", "destroy", "-auto-approve"], cwd=str(workspace))
    for disk_id, identity in list(disks.items()):
        data = _disk(backend, disk_id)
        if data is not None:
            if identity != data["uniqueId"]:
                raise AzureVmError(
                    f"OS disk identity changed; refusing deletion: {disk_id}"
                )
            if (
                data.get("managedBy")
                or data.get("managedByExtended")
                or data.get("diskState") != "Unattached"
            ):
                raise AzureVmError(
                    f"OS disk is not detached; refusing deletion: {disk_id}"
                )
            run_command(backend, ["az", "disk", "delete", "--ids", disk_id, "--yes"])
        del disks[disk_id]
        _save(workspace, disks)
