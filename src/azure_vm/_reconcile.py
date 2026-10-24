"""Recover an SDK VM omitted from state using its recorded NIC as ownership evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ._backend import CommandBackend, run_command
from .exceptions import AzureVmCommandError, AzureVmError

_NIC_ID = re.compile(
    r"(/subscriptions/[^/]+/resourceGroups/[^/]+)/providers/"
    r"Microsoft.Network/networkInterfaces/[^/]+",
    re.IGNORECASE,
)


def _show(backend: CommandBackend, command: list[str], resource_id: str) -> dict | None:
    args = ["az", *command, "--ids", resource_id, "-o", "json"]
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
        or str(data.get("id", "")).lower() != resource_id.lower()
    ):
        raise AzureVmError(f"Cannot verify resource identity: {resource_id}")
    return data


def reconcile_vm(workspace: Path, backend: CommandBackend, name: str) -> bool:
    """Import only the expected VM attached to the SDK NIC recorded in this state.

    No cloud inventory scan or name-prefix deletion is used. Azure authorization
    errors and foreign attachments fail closed, leaving the workspace retryable.
    """
    state = workspace / "terraform.tfstate"
    if not state.exists():
        return False
    resources = json.loads(state.read_text()).get("resources", [])
    managed = [
        resource
        for resource in resources
        if resource.get("mode") == "managed"
        and resource.get("name") == "vm"
        and not resource.get("module")
    ]
    if any(
        r.get("type") == "azurerm_linux_virtual_machine" and r.get("instances")
        for r in managed
    ):
        return False
    nics = [
        instance.get("attributes", {}).get("id", "")
        for r in managed
        if r.get("type") == "azurerm_network_interface"
        for instance in r.get("instances", [])
    ]
    if not nics:
        return False
    if len(nics) != 1 or not isinstance(nics[0], str):
        raise AzureVmError("Cannot verify a unique SDK NIC identity")
    nic_id = nics[0]
    match = _NIC_ID.fullmatch(nic_id)
    if match is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise AzureVmError("Cannot verify expected VM/NIC identity")
    nic = _show(backend, ["network", "nic", "show"], nic_id)
    if nic is None or nic.get("virtualMachine") is None:
        return False
    attachment = nic["virtualMachine"]
    expected = f"{match[1]}/providers/Microsoft.Compute/virtualMachines/{name}"
    if (
        not isinstance(attachment, dict)
        or str(attachment.get("id", "")).lower() != expected.lower()
    ):
        raise AzureVmError(f"Unexpected VM identity attached to SDK NIC: {nic_id}")
    vm = _show(backend, ["vm", "show"], expected)
    if vm is None:
        return False  # Spot eviction may have completed between the two lookups.
    profile = vm.get("networkProfile")
    interfaces = profile.get("networkInterfaces") if isinstance(profile, dict) else None
    if not isinstance(interfaces, list) or not any(
        isinstance(n, dict) and str(n.get("id", "")).lower() == nic_id.lower()
        for n in interfaces
    ):
        raise AzureVmError(f"VM network identity does not confirm SDK NIC: {nic_id}")
    run_command(
        backend,
        ["tofu", "import", "azurerm_linux_virtual_machine.vm", expected],
        cwd=str(workspace),
    )
    return True
