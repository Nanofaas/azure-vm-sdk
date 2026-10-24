import json
import re

import pytest

from azure_vm._backend import CommandResult, FakeBackend
from azure_vm._workspace import Workspace
from azure_vm.client import AzureClient
from azure_vm.exceptions import AzureVmCommandError


def _backend():
    backend = FakeBackend()
    backend.set_default(CommandResult([], 0, "", ""))
    return backend


def _location(hcl, kind, name):
    block = re.search(rf'{kind} "{name}"(?: "vm")? {{(.*?)^}}', hcl, re.M | re.S)
    assert block is not None
    return re.search(r"^\s*(?:location|value)\s*=\s*(.+)$", block[1], re.M)[1]


def test_workspace_uses_requested_region_for_all_regional_resources(tmp_path):
    workspace = Workspace(tmp_path, _backend())
    workspace.write_vm_workspace(
        "north",
        "Standard_B1s",
        30,
        None,
        None,
        None,
        resource_group="shared-west-rg",
        location="northeurope",
        ssh_username="user",
    )
    hcl = (tmp_path / "north" / "main.tf").read_text()
    for resource in (
        "azurerm_virtual_network",
        "azurerm_network_security_group",
        "azurerm_public_ip",
        "azurerm_network_interface",
        "azurerm_linux_virtual_machine",
    ):
        assert _location(hcl, "resource", resource) == '"northeurope"'
    assert _location(hcl, "output", "location") == '"northeurope"'


def test_regional_clients_reference_existing_rg_without_importing_or_managing_it(
    tmp_path,
):
    backend = _backend()
    for region in ("westeurope", "northeurope"):
        root = tmp_path / region
        client = AzureClient(
            resource_group="existing-west-rg",
            location=region,
            work_dir=str(root),
            backend=backend,
            manage_resource_group=False,
        )
        vm = client.launch("original")
        client.ensure_running("original")
        vm.clone("copy")
        shared = (root / ".shared" / "main.tf").read_text()
        assert 'data "azurerm_resource_group" "main"' in shared
        assert 'resource "azurerm_resource_group"' not in shared
        assert "location" not in shared
        original = (root / "original" / "main.tf").read_text()
        assert (
            _location(original, "resource", "azurerm_linux_virtual_machine")
            == f'"{region}"'
        )
        assert (root / "copy" / "main.tf").read_text() == original
    assert all(
        call[:2] not in (["tofu", "import"], ["az", "account"])
        for call in backend.calls
    )


def test_default_client_keeps_managing_resource_group(tmp_path):
    backend = _backend()
    backend.push(
        "az",
        "account",
        "show",
        "--query",
        "id",
        "-o",
        "tsv",
        result=CommandResult([], 0, "subscription", ""),
    )
    AzureClient("rg", "westeurope", str(tmp_path), backend).launch("vm")
    shared = (tmp_path / ".shared" / "main.tf").read_text()
    assert 'resource "azurerm_resource_group" "main"' in shared
    assert 'location = "westeurope"' in shared
    assert any(call[:2] == ["tofu", "import"] for call in backend.calls)


@pytest.mark.parametrize("invalid", [0, 1, "false", None])
def test_resource_group_management_requires_boolean(tmp_path, invalid):
    with pytest.raises(ValueError, match="manage_resource_group"):
        AzureClient(
            "rg",
            "northeurope",
            str(tmp_path),
            _backend(),
            manage_resource_group=invalid,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("owned", ["config", "state"])
def test_unmanaged_mode_refuses_existing_owned_resource_group(tmp_path, owned):
    shared = tmp_path / ".shared"
    shared.mkdir()
    path = shared / ("main.tf" if owned == "config" else "terraform.tfstate")
    data = (
        'resource "azurerm_resource_group" "main" {\n name = "rg"\n}\n'
        if owned == "config"
        else json.dumps(
            {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "azurerm_resource_group",
                        "name": "main",
                    }
                ]
            }
        )
    )
    path.write_text(data)
    backend = _backend()
    client = AzureClient(
        "rg", "northeurope", str(tmp_path), backend, manage_resource_group=False
    )
    with pytest.raises(ValueError, match="fresh workspace"):
        client.launch("vm")
    assert backend.calls == []
    assert path.read_text() == data
    assert not (tmp_path / "vm").exists()


def test_missing_existing_resource_group_stops_before_vm_creation_and_can_retry(
    tmp_path,
):
    backend = _backend()
    backend.push(
        "tofu",
        "apply",
        "-auto-approve",
        result=CommandResult([], 1, "", "resource group not found"),
    )
    client = AzureClient(
        "missing", "northeurope", str(tmp_path), backend, manage_resource_group=False
    )
    with pytest.raises(AzureVmCommandError, match="resource group not found"):
        client.launch("vm")
    assert not (tmp_path / "vm").exists()
    assert not (tmp_path / ".shared" / "main.tf").exists()
    client.launch("vm")
    assert (tmp_path / "vm" / "main.tf").exists()
