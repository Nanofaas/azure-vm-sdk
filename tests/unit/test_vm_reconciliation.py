"""Recover partially recorded SDK VMs without deleting another VM's network."""

import json
from pathlib import Path

import pytest

from azure_vm import AzureClient
from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.exceptions import AzureVmError
from azure_vm.vm import AzureVM

ROOT = "/subscriptions/sub/resourceGroups/rg/providers/"
VM = ROOT + "Microsoft.Compute/virtualMachines/node"
NIC = ROOT + "Microsoft.Network/networkInterfaces/node-nic"
DISK = ROOT + "Microsoft.Compute/disks/node-os"
IMPORT = ["tofu", "import", "azurerm_linux_virtual_machine.vm", VM]
DESTROY = ["tofu", "destroy", "-auto-approve"]


def reply(data=None, error=None):
    return CommandResult(
        [],
        1 if error else 0,
        json.dumps(data) if data else "",
        f"ERROR: ({error}) failure" if error else "",
    )


class PartialCloud(FakeBackend):
    """Simulate cloud/state divergence; actual SDK orchestration edits the workspace."""

    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "main.tf").write_text("SDK workspace")
        self.nic = {
            "mode": "managed",
            "type": "azurerm_network_interface",
            "name": "vm",
            "instances": [{"attributes": {"id": NIC}}],
        }
        self.write_state([self.nic])
        self.vm = VM
        self.disk = True
        self.nic_error = None
        self.import_error = None
        self.late_attachment = False
        self.lookups = 0
        self.destroy_attempts = 0
        self.profile_nic = NIC
        self.set_default(reply())

    def write_state(self, resources):
        (self.workspace / "terraform.tfstate").write_text(
            json.dumps({"resources": resources})
        )

    def run(self, args, *, cwd=None, env=None):
        super().run(args, cwd=cwd, env=env)
        if args[:4] == ["az", "network", "nic", "show"]:
            self.lookups += 1
            if self.nic_error:
                return reply(error=self.nic_error)
            attached = self.vm if not self.late_attachment or self.lookups > 1 else None
            return reply(
                {"id": NIC, "virtualMachine": {"id": attached} if attached else None}
            )
        if args[:3] == ["az", "vm", "show"]:
            return reply(
                {
                    "id": self.vm,
                    "networkProfile": {"networkInterfaces": [{"id": self.profile_nic}]},
                }
            )
        if args == IMPORT:
            if self.import_error:
                return reply(error=self.import_error)
            self.write_state(
                [
                    self.nic,
                    {
                        "mode": "managed",
                        "type": "azurerm_linux_virtual_machine",
                        "name": "vm",
                        "instances": [
                            {"attributes": {"id": VM, "os_disk": [{"id": DISK}]}}
                        ],
                    },
                ]
            )
        if args == DESTROY:
            self.destroy_attempts += 1
            resources = json.loads((self.workspace / "terraform.tfstate").read_text())[
                "resources"
            ]
            if self.vm and not any(
                r["type"] == "azurerm_linux_virtual_machine" for r in resources
            ):
                return reply(error="NicInUse")
            self.vm = None
            self.write_state([])
        if args[:3] == ["az", "disk", "show"]:
            return (
                reply(
                    {
                        "id": DISK,
                        "uniqueId": "original",
                        "managedBy": self.vm,
                        "diskState": "Attached" if self.vm else "Unattached",
                    }
                )
                if self.disk
                else reply(error="ResourceNotFound")
            )
        if args[:3] == ["az", "disk", "delete"]:
            self.disk = False
        return reply()


def test_delete_imports_untracked_vm_then_deletes_network_and_os_disk(tmp_path):
    cloud = PartialCloud(tmp_path)
    AzureVM("node", tmp_path, cloud).delete()
    assert cloud.vm is None and not cloud.disk
    assert cloud.calls.index(IMPORT) < cloud.calls.index(DESTROY)
    assert json.loads((tmp_path / "sdk-os-disks.json").read_text()) == {}


def test_purge_reconciles_partial_vm_without_touching_shared_state(tmp_path):
    cloud = PartialCloud(tmp_path / "node")
    (tmp_path / ".shared").mkdir()
    (tmp_path / ".shared/main.tf").write_text("shared")
    AzureClient(work_dir=str(tmp_path), backend=cloud).purge()
    assert cloud.vm is None and not cloud.disk
    assert str(tmp_path / ".shared") not in cloud.cwds


@pytest.mark.parametrize(
    "foreign",
    [
        VM.replace("/node", "/other"),
        VM.replace("/sub/", "/elsewhere/"),
        VM.replace("/rg/", "/foreign/"),
    ],
)
def test_foreign_vm_attached_to_owned_nic_is_not_destroyed(tmp_path, foreign):
    cloud = PartialCloud(tmp_path)
    cloud.vm = foreign
    with pytest.raises(AzureVmError, match=r"(?i)(identity|unexpected|foreign)"):
        AzureVM("node", tmp_path, cloud).delete()
    assert DESTROY not in cloud.calls and IMPORT not in cloud.calls


@pytest.mark.parametrize("error", ["AuthorizationFailed", "TooManyRequests"])
def test_nic_lookup_failure_is_not_absence(tmp_path, error):
    cloud = PartialCloud(tmp_path)
    cloud.nic_error = error
    with pytest.raises(AzureVmError):
        AzureVM("node", tmp_path, cloud).delete()
    assert DESTROY not in cloud.calls


def test_failed_import_preserves_state_and_is_retryable(tmp_path):
    cloud = PartialCloud(tmp_path)
    cloud.import_error = "ImportFailed"
    with pytest.raises(AzureVmError, match="ImportFailed"):
        AzureVM("node", tmp_path, cloud).delete()
    assert DESTROY not in cloud.calls and cloud.vm == VM
    cloud.import_error = None
    AzureVM("node", tmp_path, cloud).delete()
    assert cloud.vm is None and not cloud.disk


def test_late_attachment_after_initial_lookup_reconciles_once_on_nicinuse(tmp_path):
    cloud = PartialCloud(tmp_path)
    cloud.late_attachment = True
    AzureVM("node", tmp_path, cloud).delete()
    assert cloud.destroy_attempts == 2 and cloud.calls.count(IMPORT) == 1
    assert cloud.vm is None and not cloud.disk


def test_vm_profile_must_confirm_same_owned_nic(tmp_path):
    cloud = PartialCloud(tmp_path)
    cloud.profile_nic = NIC.replace("node-nic", "foreign-nic")
    with pytest.raises(AzureVmError, match=r"(?i)(identity|network|NIC)"):
        AzureVM("node", tmp_path, cloud).delete()
    assert IMPORT not in cloud.calls and DESTROY not in cloud.calls


@pytest.mark.parametrize("error", ["ResourceNotFound", "ResourceGroupNotFound"])
def test_missing_nic_allows_remaining_state_cleanup(tmp_path, error):
    cloud = PartialCloud(tmp_path)
    cloud.nic_error = error
    cloud.vm = None
    AzureVM("node", tmp_path, cloud).delete()
    assert IMPORT not in cloud.calls and cloud.destroy_attempts == 1


def test_unattached_nic_does_not_adopt_a_vm_by_name(tmp_path):
    cloud = PartialCloud(tmp_path)
    cloud.vm = None
    AzureVM("node", tmp_path, cloud).delete()
    assert IMPORT not in cloud.calls
    assert not any(args[:3] == ["az", "vm", "show"] for args in cloud.calls)


@pytest.mark.parametrize("nic_id", ["bad-id", NIC + "/extra"])
def test_invalid_nic_identity_blocks_cleanup(tmp_path, nic_id):
    cloud = PartialCloud(tmp_path)
    cloud.nic["instances"][0]["attributes"]["id"] = nic_id
    cloud.write_state([cloud.nic])
    with pytest.raises(AzureVmError, match="identity"):
        AzureVM("node", tmp_path, cloud).delete()
    assert not cloud.calls


def test_multiple_recorded_nics_block_cleanup(tmp_path):
    cloud = PartialCloud(tmp_path)
    cloud.nic["instances"].append({"attributes": {"id": NIC + "-other"}})
    cloud.write_state([cloud.nic])
    with pytest.raises(AzureVmError, match="unique"):
        AzureVM("node", tmp_path, cloud).delete()
    assert not cloud.calls


@pytest.mark.parametrize("resource", ["nic", "vm"])
def test_lookup_response_must_match_requested_identity(tmp_path, monkeypatch, resource):
    cloud = PartialCloud(tmp_path)
    original = cloud.run
    prefix = (
        ["az", "network", "nic", "show"] if resource == "nic" else ["az", "vm", "show"]
    )

    def run(args, **kwargs):
        if args[: len(prefix)] == prefix:
            return reply({"id": "foreign"})
        return original(args, **kwargs)

    monkeypatch.setattr(cloud, "run", run)
    with pytest.raises(AzureVmError, match="identity"):
        AzureVM("node", tmp_path, cloud).delete()
    assert DESTROY not in cloud.calls and IMPORT not in cloud.calls


def test_persistent_nicinuse_stops_after_one_retry(tmp_path, monkeypatch):
    cloud = PartialCloud(tmp_path)
    cloud.late_attachment = True
    original = cloud.run
    destroys = []

    def run(args, **kwargs):
        if args == DESTROY:
            destroys.append(args)
            return reply(error="NicInUse")
        return original(args, **kwargs)

    monkeypatch.setattr(cloud, "run", run)
    with pytest.raises(AzureVmError, match="NicInUse"):
        AzureVM("node", tmp_path, cloud).delete()
    assert len(destroys) == 2 and cloud.calls.count(IMPORT) == 1
    assert cloud.vm == VM and cloud.disk
