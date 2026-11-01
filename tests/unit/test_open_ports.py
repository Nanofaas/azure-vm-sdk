from __future__ import annotations

import pytest

from azure_vm import IngressRule, VmConfig
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


def _rule(hcl: str, port: int) -> str:
    start = hcl.index(f'name                       = "Port{port}"')
    return hcl[start : hcl.index("}", start)]


def test_ingress_renders_rules_restricted_to_their_sources(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(
        name="vm1",
        open_ports=[30080],
        ingress=[
            IngressRule(8080, ("198.51.100.7", "203.0.113.0/24")),
            IngressRule(9090, ("198.51.100.7/32",)),
        ],
    )

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert (
        'source_address_prefixes    = ["198.51.100.7/32", "203.0.113.0/24"]'
        in _rule(hcl, 8080)
    )
    assert 'source_address_prefixes    = ["198.51.100.7/32"]' in _rule(hcl, 9090)
    assert 'source_address_prefix      = "*"' in _rule(hcl, 30080)
    priorities = [
        line.split("=")[1].strip() for line in hcl.splitlines() if "priority" in line
    ]
    assert len(priorities) == len(set(priorities)) == 4  # SSH + 3 ports


def test_ingress_rules_for_one_port_merge_their_sources(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(
        name="vm1",
        ingress=[
            IngressRule(8080, ("198.51.100.7/32",)),
            IngressRule(8080, ("192.0.2.1/32", "198.51.100.7/32")),
        ],
    )

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert hcl.count('name                       = "Port8080"') == 1
    assert 'source_address_prefixes    = ["198.51.100.7/32", "192.0.2.1/32"]' in _rule(
        hcl, 8080
    )


def test_a_port_both_open_and_restricted_is_refused_before_tofu(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    with pytest.raises(ValueError, match="both open and restricted"):
        _client(ws, backend).launch(
            name="vm1",
            open_ports=[8080],
            ingress=[IngressRule(8080, ("198.51.100.7/32",))],
        )
    assert backend.calls == []


def test_ensure_running_rerenders_ingress(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(_ok())

    _client(ws, backend).launch(name="vm1")
    _client(ws, backend).ensure_running(
        "vm1", ingress=[IngressRule(8080, ("198.51.100.7/32",))]
    )

    hcl = (ws / "vm1" / "main.tf").read_text()
    assert 'source_address_prefixes    = ["198.51.100.7/32"]' in _rule(hcl, 8080)


@pytest.mark.parametrize(
    ("port", "sources", "message"),
    [
        (0, ("198.51.100.7/32",), "invalid ingress port"),
        (65536, ("198.51.100.7/32",), "invalid ingress port"),
        (True, ("198.51.100.7/32",), "invalid ingress port"),
        (22, ("198.51.100.7/32",), "SSH rule"),
        (8080, (), "no sources"),
        (8080, ("0.0.0.0/0",), "bounded"),
        (8080, ("::/0",), "bounded"),
        (8080, "198.51.100.7/32", "sequence"),
        (8080, ("not-an-address",), "does not appear to be"),
    ],
)
def test_ingress_rule_rejects_invalid_values(port, sources, message):
    with pytest.raises(ValueError, match=message):
        IngressRule(port, sources)


def test_vm_config_rejects_non_rule_ingress():
    with pytest.raises(ValueError, match="IngressRule"):
        VmConfig(ingress=({"port": 8080},))  # type: ignore[arg-type]
