import json
from pathlib import Path

import pytest

from azure_vm import AzureClient
from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.exceptions import AzureVmError
from azure_vm.vm import AzureVM

PREFIX = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/"
DISK = PREFIX + "disks/owned-os-disk"
OTHER = PREFIX + "disks/foreign-data-disk"
VM = PREFIX + "virtualMachines/node"
SHOW = ["az", "disk", "show", "--ids", DISK, "-o", "json"]
DELETE = ["az", "disk", "delete", "--ids", DISK, "--yes"]
LEDGER = "sdk-os-disks.json"


def ok(data=None):
    return CommandResult([], 0, json.dumps(data) if data is not None else "", "")


def error(code):
    return CommandResult([], 1, "", f"ERROR: ({code}) test failure\nCode: {code}")


def disk(**overrides):
    return dict(
        id=DISK,
        uniqueId="original-uuid",
        managedBy=None,
        managedByExtended=[],
        diskState="Unattached",
        **overrides,
    )


def state(path, disk_id=DISK):
    path.mkdir(parents=True, exist_ok=True)
    (path / "main.tf").write_text("placeholder")
    (path / "terraform.tfstate").write_text(
        json.dumps(
            {
                "version": 4,
                "resources": [
                    {
                        "mode": "managed",
                        "type": "azurerm_linux_virtual_machine",
                        "name": "vm",
                        "instances": [
                            {
                                "attributes": {
                                    "id": VM,
                                    "name": "node",
                                    "os_disk": [{"id": disk_id}],
                                    "data_disks": [{"id": OTHER}],
                                }
                            }
                        ],
                    }
                ],
            }
        )
    )


class Cloud(FakeBackend):
    """Only simulate external state changes; SDK lifecycle and files are real."""

    def __init__(self):
        super().__init__()
        self.set_default(ok())
        self.cloud_disk = disk()
        self.destroy_error = False

    def run(self, args, *, cwd=None, env=None):
        result = super().run(args, cwd=cwd, env=env)
        if args[:2] == ["tofu", "destroy"]:
            if self.destroy_error:
                return error("DestroyFailed")
            (Path(cwd) / "terraform.tfstate").write_text('{"resources": []}')
        if args == SHOW:
            return ok(self.cloud_disk) if self.cloud_disk else error("ResourceNotFound")
        if args == DELETE:
            self.cloud_disk = None
        return result


def test_delete_recovers_evicted_os_disk_after_destroy_forgets_vm(tmp_path):
    state(tmp_path)
    backend = Cloud()
    AzureVM("node", tmp_path, backend).delete()
    assert backend.cloud_disk is None
    assert not any(OTHER in call for call in backend.calls)
    assert json.loads((tmp_path / LEDGER).read_text()) == {}
    AzureVM("node", tmp_path, backend).delete()
    assert backend.calls.count(DELETE) == 1


def test_launch_records_disk_before_later_refresh_loses_vm(tmp_path):
    work = tmp_path / "node"
    backend = Cloud()
    original_run = backend.run

    def create(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["tofu", "apply"] and kwargs.get("cwd") == str(work):
            state(work)
        return result

    backend.run = create
    client = AzureClient("rg", "westeurope", str(tmp_path), backend=backend)
    client.launch("node")
    assert DISK in json.loads((work / LEDGER).read_text())
    (work / "terraform.tfstate").write_text('{"resources": []}')
    client.get_vm("node").delete()
    assert backend.cloud_disk is None


@pytest.mark.parametrize(
    "changes",
    [
        {"managedBy": VM},
        {"managedByExtended": [VM]},
        {"diskState": "Reserved"},
        {"diskState": "ActiveSAS"},
        {"diskState": None},
    ],
)
def test_attached_or_busy_disk_is_preserved_for_retry(tmp_path, changes):
    state(tmp_path)
    backend = Cloud()
    backend.cloud_disk.update(changes)
    with pytest.raises(AzureVmError, match="disk"):
        AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls
    assert DISK in json.loads((tmp_path / LEDGER).read_text())
    backend.cloud_disk = disk()
    AzureVM("node", tmp_path, backend).delete()
    assert backend.cloud_disk is None


def test_reused_resource_id_does_not_delete_replacement_disk(tmp_path):
    state(tmp_path)
    backend = Cloud()
    backend.destroy_error = True
    with pytest.raises(AzureVmError):
        AzureVM("node", tmp_path, backend).delete()
    backend.destroy_error = False
    backend.cloud_disk["uniqueId"] = "unrelated-replacement"
    with pytest.raises(AzureVmError, match="identity"):
        AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls


@pytest.mark.parametrize("code", ["AuthorizationFailed", "TooManyRequests"])
def test_lookup_failure_is_not_treated_as_missing(tmp_path, code):
    state(tmp_path)
    backend = FakeBackend({tuple(SHOW): error(code)})
    backend.set_default(ok())
    with pytest.raises(AzureVmError):
        AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls
    assert DISK in json.loads((tmp_path / LEDGER).read_text())


def test_missing_disk_is_idempotent(tmp_path):
    state(tmp_path)
    backend = Cloud()
    backend.cloud_disk = None
    AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls
    assert json.loads((tmp_path / LEDGER).read_text()) == {}


def test_purge_uses_same_cleanup_and_keeps_shared_workspace(tmp_path):
    state(tmp_path / "node")
    shared = tmp_path / ".shared"
    shared.mkdir()
    (shared / "main.tf").write_text("resource group")
    backend = Cloud()
    AzureClient(work_dir=str(tmp_path), backend=backend).purge()
    assert backend.cloud_disk is None
    assert str(shared) not in backend.cwds


def test_clone_never_copies_source_state_or_disk_ownership(tmp_path):
    source = tmp_path / "node"
    state(source)
    (source / LEDGER).write_text(json.dumps({DISK: "original-uuid"}))
    (source / "terraform.tfstate.backup").write_text("source backup")
    backend = FakeBackend()
    backend.set_default(ok())
    clone = AzureVM("node", source, backend).clone("clone")
    assert not (tmp_path / "clone" / "terraform.tfstate").exists()
    assert not (tmp_path / "clone" / "terraform.tfstate.backup").exists()
    assert not (tmp_path / "clone" / LEDGER).exists()
    assert clone.name == "clone"


def test_failed_disk_delete_is_retryable_after_vm_state_is_gone(tmp_path):
    state(tmp_path)
    backend = Cloud()
    original_run = backend.run

    def fail_delete(args, **kwargs):
        if args == DELETE:
            return error("AuthorizationFailed")
        return original_run(args, **kwargs)

    backend.run = fail_delete
    with pytest.raises(AzureVmError):
        AzureVM("node", tmp_path, backend).delete()
    assert DISK in json.loads((tmp_path / LEDGER).read_text())
    assert json.loads((tmp_path / "terraform.tfstate").read_text())["resources"] == []
    backend.run = original_run
    AzureVM("node", tmp_path, backend).delete()
    assert backend.cloud_disk is None


def test_failed_apply_still_records_new_os_disk(tmp_path):
    backend = Cloud()
    original_run = backend.run

    def failed_apply(args, **kwargs):
        if args[:2] == ["tofu", "apply"]:
            state(tmp_path)
            return error("ApplyFailed")
        return original_run(args, **kwargs)

    backend.run = failed_apply
    with pytest.raises(AzureVmError, match="ApplyFailed"):
        AzureVM("node", tmp_path, backend).start()
    assert DISK in json.loads((tmp_path / LEDGER).read_text())


def test_replacement_keeps_both_generations_for_cleanup(tmp_path):
    state(tmp_path)
    new_id = PREFIX + "disks/replacement-os-disk"
    disks = {DISK: disk(), new_id: {**disk(), "id": new_id, "uniqueId": "new-uuid"}}
    backend = FakeBackend()
    backend.set_default(ok())
    original_run = backend.run

    def replacing(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["tofu", "apply"]:
            state(tmp_path, new_id)
        if args[:3] == ["az", "disk", "show"]:
            return ok(disks[args[4]])
        if args[:3] == ["az", "disk", "delete"]:
            del disks[args[4]]
        return result

    backend.run = replacing
    vm = AzureVM("node", tmp_path, backend)
    vm.start()
    assert set(json.loads((tmp_path / LEDGER).read_text())) == {DISK, new_id}
    vm.delete()
    assert disks == {}


@pytest.mark.parametrize(
    "registry", ["invalid JSON", "[]", '{"invalid-disk-id": null}']
)
def test_corrupt_registry_prevents_destructive_actions(tmp_path, registry):
    state(tmp_path)
    (tmp_path / LEDGER).write_text(registry)
    backend = Cloud()
    with pytest.raises((AzureVmError, ValueError)):
        AzureVM("node", tmp_path, backend).delete()
    assert backend.calls == []


def test_no_state_or_registry_does_not_search_for_disks(tmp_path):
    backend = Cloud()
    AzureVM("node", tmp_path, backend).delete()
    assert not any(call[0] == "az" for call in backend.calls)


def test_partial_apply_error_is_preserved_when_disk_lookup_also_fails(tmp_path):
    backend = FakeBackend()
    original_run = backend.run
    backend.set_default(ok())

    def failing(args, **kwargs):
        if args[:2] == ["tofu", "apply"]:
            state(tmp_path)
            return error("ApplyFailed")
        if args == SHOW:
            return error("AuthorizationFailed")
        return original_run(args, **kwargs)

    backend.run = failing
    with pytest.raises(AzureVmError, match="ApplyFailed"):
        AzureVM("node", tmp_path, backend).start()
    assert DISK in json.loads((tmp_path / LEDGER).read_text())


def test_missing_disk_then_failed_destroy_cannot_adopt_reused_id(tmp_path):
    state(tmp_path)
    backend = Cloud()
    backend.cloud_disk = None
    backend.destroy_error = True
    with pytest.raises(AzureVmError):
        AzureVM("node", tmp_path, backend).delete()
    backend.destroy_error = False
    backend.cloud_disk = {**disk(), "uniqueId": "foreign-recreation"}
    with pytest.raises(AzureVmError, match="identity"):
        AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls


def test_pre_415_provider_state_tracks_exact_os_disk_name(tmp_path):
    state(tmp_path)
    path = tmp_path / "terraform.tfstate"
    data = json.loads(path.read_text())
    data["resources"][0]["instances"][0]["attributes"]["os_disk"] = [
        {"name": "owned-os-disk", "diff_disk_settings": []}
    ]
    path.write_text(json.dumps(data))
    backend = Cloud()
    AzureVM("node", tmp_path, backend).delete()
    assert backend.cloud_disk is None


def test_deleted_resource_group_counts_as_absent_disk(tmp_path):
    state(tmp_path)
    backend = FakeBackend({tuple(SHOW): error("ResourceGroupNotFound")})
    backend.set_default(ok())
    AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls
    assert json.loads((tmp_path / LEDGER).read_text()) == {}


def test_unverified_registry_without_vm_state_cannot_adopt_disk(tmp_path):
    (tmp_path / LEDGER).write_text(json.dumps({DISK: None}))
    backend = Cloud()
    with pytest.raises(AzureVmError, match="identity"):
        AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls


def test_azure_disk_notfound_response_counts_as_absent(tmp_path):
    state(tmp_path)
    backend = FakeBackend({tuple(SHOW): error("NotFound")})
    backend.set_default(ok())
    AzureVM("node", tmp_path, backend).delete()
    assert DELETE not in backend.calls
    assert json.loads((tmp_path / LEDGER).read_text()) == {}
