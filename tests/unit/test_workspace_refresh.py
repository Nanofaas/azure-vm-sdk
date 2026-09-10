"""Config changes must never be silently ignored by existing workspaces.

Regression for the nanofaas incident (2026-06-11): the first run baked a
placeholder resource group into ~/.azure-vm-sdk/<vm>/main.tf and every later
run reused the stale file, ignoring the corrected configuration.
"""

from __future__ import annotations

import json

from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.client import AzureClient

OUTPUT_JSON = json.dumps(
    {
        "vm_ip": {"value": "1.2.3.4"},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
        "resource_group": {"value": "old-rg"},
    }
)


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout=stdout, stderr="")


def _client(ws, rg: str, backend: FakeBackend) -> AzureClient:
    return AzureClient(
        resource_group=rg,
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )


def test_ensure_running_rerenders_workspace_from_current_config(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok(OUTPUT_JSON))

    _client(ws, "old-rg", backend).launch(name="vm1")
    assert "old-rg" in (ws / "vm1" / "main.tf").read_text()

    _client(ws, "new-rg", backend).ensure_running("vm1", vm_size="Standard_B2s")

    main_tf = (ws / "vm1" / "main.tf").read_text()
    assert "new-rg" in main_tf
    assert "old-rg" not in main_tf
    tfvars = (ws / "vm1" / "terraform.tfvars").read_text()
    assert 'vm_size = "Standard_B2s"' in tfvars


def test_ensure_shared_infra_reapplies_when_config_changes(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok(OUTPUT_JSON))

    _client(ws, "old-rg", backend).launch(name="vm1")
    shared_applies_before = _shared_applies(backend, ws)

    _client(ws, "new-rg", backend).ensure_running("vm1")

    shared_tf = (ws / ".shared" / "main.tf").read_text()
    assert "new-rg" in shared_tf
    assert "old-rg" not in shared_tf
    assert _shared_applies(backend, ws) > shared_applies_before


def test_ensure_shared_infra_skips_apply_when_unchanged(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok(OUTPUT_JSON))

    _client(ws, "same-rg", backend).launch(name="vm1")
    shared_applies_before = _shared_applies(backend, ws)

    _client(ws, "same-rg", backend).ensure_running("vm1")

    assert _shared_applies(backend, ws) == shared_applies_before


def _shared_applies(backend: FakeBackend, ws) -> int:
    shared = str(ws / ".shared")
    return sum(
        1
        for call, cwd in zip(backend.calls, backend.cwds, strict=True)
        if cwd == shared and call[:2] == ["tofu", "apply"]
    )
