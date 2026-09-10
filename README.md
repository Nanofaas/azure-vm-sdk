# azure-vm-sdk

Python SDK for managing Azure VMs via OpenTofu.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

```bash
uv sync --group dev
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

`max_workers` caps the thread pool size (default: one thread per VM):

```python
vms = client.launch_many(configs, max_workers=4)
```

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

# Include integration tests (requires Azure + OpenTofu installed)
uv run pytest -m "integration"

# With coverage report
uv run pytest --cov=azure_vm --cov-report=term-missing
```

## Quality checks

```bash
# Run all three (ruff + basedpyright + import-linter)
uv run azure-vm-quality
```

Individual checks:

```bash
uv run ruff check .            # Linting (pyflakes + private access)
uv run basedpyright            # Type checking
uv run lint-imports            # Architecture contract enforcement
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
uv run pydeps azure_vm --show-deps --max-bacon 2
```

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
