from __future__ import annotations

from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.client import AzureClient


def _ok() -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout="", stderr="")


def _client(ws, backend) -> AzureClient:
    return AzureClient(
        resource_group="rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )


def test_launch_renders_extra_nsg_rules_for_open_ports(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(name="vm1", open_ports=[30080, 30090])

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert 'destination_port_range     = "30080"' in hcl
    assert 'destination_port_range     = "30090"' in hcl
    assert 'destination_port_range     = "22"' in hcl  # SSH rule untouched


def test_launch_without_open_ports_keeps_ssh_only(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(name="vm1")

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert hcl.count("security_rule") == 1  # only SSH


def test_open_ports_dedupe_skip_22_and_unique_priorities(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(name="vm1", open_ports=[22, 30080, 30080, 30090])

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert hcl.count('destination_port_range     = "30080"') == 1
    priorities = [
        line.split("=")[1].strip() for line in hcl.splitlines() if "priority" in line
    ]
    assert len(priorities) == len(set(priorities)) == 3  # SSH + 2 ports


def test_ensure_running_rerenders_open_ports(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(name="vm1")  # SSH-only
    _client(ws, backend).ensure_running("vm1", open_ports=[30080])

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert 'destination_port_range     = "30080"' in hcl
