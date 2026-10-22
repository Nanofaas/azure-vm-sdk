# azure-vm-sdk

Python SDK for managing Azure VMs via OpenTofu.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

```bash
uv sync
```

## Usage

### Single VM

```python
from azure_vm import AzureClient

client = AzureClient(resource_group="my-rg", location="westeurope")
vm = client.launch(name="my-vm", vm_size="Standard_B2s")
print(vm.info())
vm.delete()
```

### SSH connection retries

SSH connections retry transient network failures up to six times by default.
The delay doubles from 1 second to a 10-second cap, with jitter between half
and all of each delay. Failed sessions are closed before the next attempt.
Authentication, host-key and configuration errors fail immediately; commands
that have already started are never replayed.

Set `ssh_connect_attempts` on `AzureClient` or `AzureVM` to change the attempt
limit (`1` disables retries). `ssh_connect_timeout` defaults to 15 seconds
and limits each TCP connect, SSH banner and authentication wait separately.
A new connection is also retried when reconnecting an expired cached session.

### Multiple VMs in parallel

Use `launch_many` to create N VMs simultaneously. All VMs are provisioned in
parallel; if any one fails the already-created VMs are destroyed automatically
before the exception is re-raised (fail-fast with rollback).

```python
from azure_vm import AzureClient, VmConfig

client = AzureClient(resource_group="my-rg", location="westeurope")

# Different names, sizes, and images
vms = client.launch_many(
    [
        VmConfig(
            name="frontend",
            vm_size="Standard_B1s",
            image_urn="Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest",
        ),
        VmConfig(
            name="backend",
            vm_size="Standard_B2s",
            image_urn="Canonical:0001-com-ubuntu-server-jammy:22_04-lts:latest",
            disk_size_gb=64,
        ),
        VmConfig(
            name="db",
            vm_size="Standard_D2s_v3",
            disk_size_gb=128,
        ),
    ]
)

for vm in vms:
    print(vm.name, vm.info().ipv4)
```

`VmConfig` accepts the same keyword arguments as `launch`:

| Field | Default | Description |
|-------|---------|-------------|
| `name` | `None` (auto-generated) | VM name |
| `vm_size` | `"Standard_B1s"` | Azure VM size |
| `disk_size_gb` | `30` | OS disk size |
| `image_urn` | `None` (Ubuntu 24.04 LTS) | Marketplace image URN |
| `cloud_init_config` | `None` | cloud-init dict or YAML string |
| `ssh_key_path` | `None` (inherits from client) | Path to SSH public key |
| `open_ports` | `None` | Additional inbound TCP ports (SSH is always enabled) |
| `priority` | `"Regular"` | `"Regular"` or `"Spot"` |
| `eviction_policy` | `None` | Spot only: `"Delete"` (effective default) or `"Deallocate"` |
| `max_bid_price` | `None` | Spot only: USD/hour, finite and non-negative, or `-1` to disable price-based eviction |

`max_workers` caps the thread pool size (default: one thread per VM):

```python
vms = client.launch_many(configs, max_workers=4)
```

### OS disk cleanup after eviction

`vm.delete()` and `client.purge()` also clean up orphan managed OS disks when
Azure has already deleted a Spot VM. Each VM workspace keeps `sdk-os-disks.json`
with exact disk resource IDs and Azure `uniqueId` values, captured before and
after applies. Replacement retains both generations until cleanup succeeds.

Cleanup uses the authenticated Azure CLI (`az disk show/delete`). It deletes a
recorded disk only when its identity is unchanged, `diskState` is `Unattached`,
and both `managedBy` and `managedByExtended` are empty. It never scans the
subscription for name matches, touches user data disks, or force-detaches disks.
Missing disks are success; authorization, network, identity and attachment errors
are reported and unresolved entries remain available for retry. Call `delete()`
or `purge()` again after resolving the error.

Keep the workspace and its disk registry until cleanup completes. Older local
workspaces can recover disk IDs from their retained VM state (including AzureRM
versions before 4.15). If both the state and registry have already been lost,
the SDK cannot establish ownership and cannot recover those orphan disks.
Unverified registry entries without supporting VM state require manual
reconciliation. Lifecycle operations on the same workspace must be serialized;
parallel creation/deletion of different VM workspaces is supported. Clones copy
configuration only, with independent state and disk ownership.

### Progressive Spot creation

`launch_many_iter(configs, max_workers=8)` yields an exported `LaunchResult`
for each completed creation, including failures, while retaining successful
VMs. Each result carries `config`, `vm` and `error`: success sets `vm`, failure
sets `error`. A slow VM does not delay faster outcomes. The worker limit bounds
submitted creations independently of fleet size; consuming results advances
submission of the remaining configurations.

```python
import json
from contextlib import closing
from pathlib import Path

from azure_vm import VmConfig

configs = [
    VmConfig(name=f"worker-{i}", priority="Spot", max_bid_price=-1) for i in range(100)
]
# Record every intended name before starting any cloud work.
Path("planned-vms.json").write_text(json.dumps([cfg.name for cfg in configs]))
with closing(client.launch_many_iter(configs, max_workers=8)) as outcomes:
    for outcome in outcomes:
        if outcome.error is not None:
            print(outcome.config.name, "creation failed:", outcome.error)
        else:
            print(outcome.vm.name, "created")
            # Hand off SSH/cloud-init preparation to your own bounded worker pool.
```

Creation completion does not imply SSH or cloud-init readiness. Prepare each
VM before scheduling jobs on it. `launch` and `ensure_running` accept the same
Spot options; cloning preserves the rendered options. `None` for the bid
leaves the provider default (`-1`). A Spot VM can still be evicted for capacity
reasons. The available fields follow the
[AzureRM Linux VM resource](https://github.com/hashicorp/terraform-provider-azurerm/blob/main/website/docs/r/linux_virtual_machine.html.markdown).

Use `contextlib.closing` if the loop can break or raise (including Ctrl-C).
Close stops new submissions, cancels unstarted work and waits for running
OpenTofu calls; it never rolls back successful peers. It can therefore block
until those calls finish. Outcomes for drained work are not yielded after
close, and failed applies can leave partial resources. Reconcile the recorded
names with the workspace to resume or delete them. Do not delete the workspace
until cleanup has finished. Explicit, unique names are recommended for durable
tracking; autogenerated names are available in returned configurations.

`launch_many` retains its existing transactional rollback behavior. Spot
settings are validated before batch creation; inconsistent combinations,
non-finite prices and invalid priorities are rejected without provisioning.

---

## End-to-end smoke test

Provision a real Azure VM, wait for SSH readiness, run a verification command,
and tear it down. Requires Azure credentials and OpenTofu installed.

```bash
# Prerequisites
az login
export AZURE_RESOURCE_GROUP=my-resource-group
export AZURE_LOCATION=westeurope
export AZURE_SSH_PUBLIC_KEY=~/.ssh/id_rsa.pub   # optional

# Run with defaults (single VM)
uv run azure-vm-e2e

# Custom VM size and image
uv run azure-vm-e2e --name my-test-vm --vm-size Standard_D2s_v3 \
    --image-urn "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest" \
    --timeout 300

# Create 3 identical VMs in parallel (names: worker-0, worker-1, worker-2)
uv run azure-vm-e2e --count 3 --name worker --vm-size Standard_B2s

# Create VMs with different names, sizes, and images
uv run azure-vm-e2e --configs '[
  {"name": "frontend", "vm_size": "Standard_B1s"},
  {"name": "backend",  "vm_size": "Standard_B2s", "disk_size_gb": 64},
  {"name": "db",       "vm_size": "Standard_D2s_v3", "disk_size_gb": 128}
]'
```

Output shows a 5-step progress:

```
[1/5] Launching VM 'e2e-1747152000' (size=Standard_B1s) ...
       launch completed in 52.3s
[2/5] Waiting for public IP (timeout=300s) ...
       got IP 20.1.2.3 in 18.5s
[3/5] Waiting for SSH on 20.1.2.3:22 ...
       SSH ready on 20.1.2.3 in 42.1s
[4/5] Running verification command ...
       exit=0  stdout=Linux e2e-1747152000 6.8.0-1020-azure ...
       state=running  location=westeurope  resource_group=my-rg
[5/5] Deleting VM 'e2e-1747152000' ...
       done.

SUCCESS — VM 'e2e-1747152000' completed full lifecycle.
```

## Running tests

```bash
# Unit tests only (default, no Azure/OpenTofu needed)
uv run pytest

# Include integration tests (requires Azure + OpenTofu installed).
# --no-cov: the 80% coverage gate targets the unit suite, so it would fail on
# an integration-only run.
uv run pytest -m "integration" --no-cov

# Coverage report (already on by default, with an 80% gate; shown explicitly)
uv run pytest --cov=azure_vm --cov-report=term-missing
```

## Quality checks

```bash
# Run all three (ruff + basedpyright + import-linter)
uv run azure-vm-quality
```

Individual checks:

```bash
uv run ruff check .                    # Linting (pycodestyle, pyflakes, isort, bugbear, pyupgrade, pydocstyle, …)
uv run ruff format .                   # Formatting
uv run basedpyright                    # Type checking
uv run bandit -c pyproject.toml -r src # Security static analysis
uv run lint-imports                    # Architecture contract enforcement
uv run pre-commit run --all-files      # Every hook above, all at once
```

## Code evaluation tools

### Full audit

Runs automated detection for bugs, excessive coupling, simplification opportunities,
code smells, and god classes.

```bash
uv run azure-vm-eval           # Human-readable report
uv run azure-vm-eval --json    # JSON output (for CI / machine consumption)
```

Checks performed:

| Category | What it detects |
|----------|----------------|
| Bugs | Bare/broad except, raise without `from`, unused exception classes, mutable defaults |
| Coupling | High fan-out modules, circular dependencies, zero fan-in modules |
| Simplifications | Functions over 30 lines, duplicated logic |
| Smells | God classes (lines, methods, fan-out), modules over 250 lines, `__init__` leaking internals |

### Package cohesion report

Measures internal cohesion and inter-module coupling for every module in
`azure_vm`.

```bash
uv run azure-vm-package-report           # Cohesion/coupling table
uv run azure-vm-package-report --edges   # Include individual import edges
uv run azure-vm-package-report --orphans # Show modules with zero internal deps
```

Metrics:

| Column | Meaning |
|--------|---------|
| `internal` | Imports within the same module |
| `outgoing` | Imports from other `azure_vm` modules |
| `incoming` | Times this module is imported by other `azure_vm` modules |
| `external` | Imports from third-party packages |
| `instability` | `outgoing / (incoming + outgoing)` — 0 = highly stable (depended on by many), 1 = highly unstable (depends on everything) |

### Dependency graph visualization

```bash
uv run pydeps azure_vm --show-deps --max-bacon 2 --nodot
```

Rendering the diagram itself additionally needs Graphviz (`dot`) on `PATH`;
`--nodot` skips that step and prints the dependency analysis as JSON.

## Architecture contracts

Defined in `.importlinter`:

1. `models_and_exceptions_are_independent` — foundation layer must not import
   higher-level packages
2. `backend_is_independent` — `_backend` must not depend on `vm` or `client`
3. `internal_modules_are_independent` — `_templates`, `_workspace`,
   `_discovery` must not depend on the public API layer

## Project structure

```
src/azure_vm/
├── __init__.py        Public API re-exports
├── client.py          AzureClient — VM provisioning orchestrator
├── vm.py              AzureVM — single-VM operations (lifecycle, SSH, exec)
├── _workspace.py      Workspace — filesystem, template writing, scan, purge
├── _discovery.py      Azure Marketplace image discovery
├── _templates.py      HCL generation + image URN / SSH path helpers
├── _backend.py        CommandBackend protocol, TofuBackend, FakeBackend, run_command
├── models.py          VmConfig, VmInfo, VmState, ImageInfo
├── exceptions.py      Exception hierarchy
└── testing.py         Test doubles for consumers (FakeBackend, CommandResult)
```
