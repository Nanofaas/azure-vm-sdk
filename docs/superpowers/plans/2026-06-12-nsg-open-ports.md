# NSG Open Ports Implementation Plan (azure-vm-sdk)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers open additional inbound TCP ports in the per-VM NSG (today only 22 is allowed), so HTTP services on the VM (e.g. k8s NodePorts) are reachable from outside.

**Architecture:** `VM_TEMPLATE`'s NSG resource gains an `{extra_security_rules}` placeholder rendered by a pure helper from an `open_ports` sequence (port 22 excluded — the SSH rule is always present; deterministic priorities). The parameter flows `launch`/`ensure_running` → `write_vm_workspace` → template. Thanks to the re-render fix (b61d189), changing `open_ports` on an existing workspace reconciles on the next ensure_running. Default `None` keeps today's SSH-only behavior.

**Tech Stack:** Python, OpenTofu HCL templates, pytest + FakeBackend.

---

## Context (why)

nanofaas `azure-vm-loadtest` (2026-06-12) reached step 40/48 and died on a TCP connect timeout: function registration runs from the operator's machine against `http://<vm-public-ip>:30080`, and the NSG only allows 22. Prometheus snapshots (`:30090`) and the loadgen→stack k6 traffic (separate per-VM subnets → public IP path) hit the same wall.

**Security posture (deliberate):** new rules use `source_address_prefix = "*"`, matching the existing SSH rule's posture; these are short-lived e2e VMs. Restricting the source (caller egress IP + peer VM IP) is a possible future enhancement, noted, not implemented (the caller IP is unknowable without an external service, and the peer VM's public IP doesn't exist yet at stack-creation time).

---

### Task 1: render extra NSG rules from `open_ports`

**Files:**
- Modify: `src/azure_vm/_templates.py` (NSG block in `VM_TEMPLATE` + new helper `render_security_rules`)
- Modify: `src/azure_vm/_workspace.py` (`write_vm_workspace` gains `open_ports`)
- Modify: `src/azure_vm/client.py` (`launch` + `ensure_running` gain `open_ports`, forwarded; `models.VmConfig` gains the field for `launch_many`)
- Test: `tests/unit/test_open_ports.py` (new)

- [ ] **Step 1: Failing tests** — create `tests/unit/test_open_ports.py`:

```python
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
```

(`ensure_running` on an existing workspace needs a `tofu output -json` response in some paths — if the FakeBackend default `_ok()` with empty stdout makes `get_vm`/`info()` fall into the re-launch branch, that still re-renders with open_ports, so the assertion holds either way; adapt plumbing only if a KeyError/exception surfaces.)

- [ ] **Step 2: run → expect failures** (`unexpected keyword argument 'open_ports'`). Full unit suite command: `uv run pytest tests/unit -q`.

- [ ] **Step 3: implement**

`_templates.py` — inside `VM_TEMPLATE`'s NSG resource, after the SSH `security_rule` block and before the closing `}}` of the NSG resource, add the placeholder line `{extra_security_rules}`. Then add:

```python
def render_security_rules(open_ports) -> str:
    """Extra inbound TCP allow-rules for the per-VM NSG.

    Port 22 is skipped (the SSH rule is always rendered). Source is "*",
    matching the SSH rule's posture — these are short-lived e2e VMs; source
    restriction is a possible future enhancement.
    """
    rules: list[str] = []
    for index, port in enumerate(sorted({int(p) for p in (open_ports or [])})):
        if port == 22:
            continue
        rules.append(
            "\n  security_rule {\n"
            f'    name                       = "Port{port}"\n'
            f"    priority                   = {1010 + index}\n"
            '    direction                  = "Inbound"\n'
            '    access                     = "Allow"\n'
            '    protocol                   = "Tcp"\n'
            '    source_port_range          = "*"\n'
            f'    destination_port_range     = "{port}"\n'
            '    source_address_prefix      = "*"\n'
            '    destination_address_prefix = "*"\n'
            "  }\n"
        )
    return "".join(rules)
```

`_workspace.py` — `write_vm_workspace(..., open_ports=None)` (keyword-only, after `default_ssh_key`); pass `extra_security_rules=render_security_rules(open_ports)` into `VM_TEMPLATE.format(...)`; import the helper.

`client.py` — `launch(..., open_ports: Sequence[int] | None = None)` and `ensure_running(..., open_ports: Sequence[int] | None = None)`: forward to `write_vm_workspace` in BOTH places (launch and the exists-branch re-render added by b61d189), and from `ensure_running`'s fallback `self.launch(...)` calls. `models.VmConfig` gains `open_ports: tuple[int, ...] | None = None`; `launch_many` forwards `cfg.open_ports`.

- [ ] **Step 4: full unit suite green** (`uv run pytest tests/unit -q`). NOTE: existing tests that count rendered content (e.g. workspace-render tests) may need the new empty-placeholder behavior — with `open_ports=None` the placeholder renders to empty string, so existing expectations should hold; if a test does exact-string comparison of the template, update it minimally and report.

- [ ] **Step 5: commit (NO push — publishing is the user's decision)**

```bash
git add src/azure_vm tests/unit docs/superpowers/plans/2026-06-12-nsg-open-ports.md
git commit -m "feat: open_ports — extra inbound NSG rules for caller-specified TCP ports"
```

### Task 2: publish (USER DECISION)

- [ ] User reviews + pushes; the new SHA gates the nanofaas plan's bump task (`mcFaas/docs/superpowers/plans/2026-06-12-azure-nodeport-reachability.md`).
