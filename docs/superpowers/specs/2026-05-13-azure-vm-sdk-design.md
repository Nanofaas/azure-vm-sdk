# Azure VM SDK — Design Specification

## Overview

Python SDK that wraps OpenTofu to manage Azure Linux VMs programmatically.  
Follows the same architecture as `multipass-sdk`: Protocol-based backend, dataclass models,
typed exception hierarchy, and clean separation between global operations (client) and
per-instance operations (VM).

Auth is inherited from the environment (`az login`, env vars). No credentials are managed
by the SDK.

## Scope

### Supported operations

| Operation | Implementation |
|---|---|
| `launch` | `tofu init && tofu apply` in VM workspace |
| `delete` | `tofu destroy` in VM workspace |
| `start` / `stop` / `restart` | `tofu apply` with toggled desired state variable |
| `info` | `tofu output -json` |
| `list` | iterate VM workspaces, call `tofu output` |
| `find` (images) | `az vm image list` (read-only, no state) |
| `exec` / `transfer` | SSH via paramiko |
| `wait_for_ip` / `wait_ready` | poll `tofu output` for IP, then TCP connect on port 22 |
| `clone` | copy workspace to new name, `tofu apply` |
| `ensure_running` | state machine: not-found → launch, deleted → purge+launch, stopped → start, running → no-op |
| `purge` | `tofu destroy` on all managed VM workspaces |

### Excluded (not applicable to Azure or out of scope)

`suspend`, `recover`, `snapshot`, `restore`, `mount`, `unmount`, `networks`, `get`, `set`, `aliases`

## Project structure

```
azure-vm-sdk/
├── pyproject.toml
├── README.md
├── LICENSE.txt
├── src/
│   └── azure_vm/
│       ├── __init__.py
│       ├── _backend.py        # CommandResult, TofuBackend, FakeBackend
│       ├── exceptions.py      # Typed exception hierarchy
│       ├── models.py          # VmInfo, VmState, ImageInfo
│       ├── vm.py              # AzureVM — per-instance operations
│       └── client.py          # AzureClient — global operations + VM factory
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_backend.py
│   │   ├── test_client.py
│   │   ├── test_exceptions.py
│   │   ├── test_models.py
│   │   └── test_vm.py
│   └── integration/
│       └── test_integration.py
```

5 source modules, mirroring `multipass-sdk`.

## Shared infrastructure

VMs must communicate with each other, so VNet and subnet are shared resources managed
once by the SDK.

```
~/.azure-vm-sdk/
├── .shared/           # VNet + subnet (owned once, referenced by all VMs)
│   ├── main.tf
│   └── terraform.tfvars
├── vm-prod/           # Per-VM workspace
│   ├── main.tf
│   └── terraform.tfvars
└── vm-staging/
    ├── main.tf
    └── terraform.tfvars
```

- On first `launch`, if `.shared` doesn't exist, the client runs `tofu apply` there.
- Each VM workspace references the shared subnet via a Terraform `data` source.
- `purge` destroys all VM workspaces but preserves `.shared` (destroy it only if
  explicitly requested).

## Module design

### `_backend.py`

```python
@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class CommandBackend(Protocol):
    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


class TofuBackend:
    """Invokes the OpenTofu CLI. Respects Azure env vars from the caller's environment."""

    def run(self, args, *, cwd=None, env=None) -> CommandResult: ...


class FakeBackend:
    """Pre-configured responses for testing. Records all calls."""

    # identical semantics to multipass-sdk FakeBackend, plus cwd tracking
```

The `cwd` parameter is the only addition over multipass-sdk's `CommandBackend`: every
tofu invocation runs inside a specific workspace directory.

### `exceptions.py`

```python
class AzureVmError(Exception):                # base
class AzureVmCommandError(AzureVmError):      # tofu/az CLI returned non-zero
class TofuNotInstalledError(AzureVmError):    # tofu binary not found
class VmNotFoundError(AzureVmError):          # VM does not exist
class VmAlreadyRunningError(AzureVmError):    # start called on running VM
class VmNotRunningError(AzureVmError):        # operation requires running VM
class AzureVmTimeoutError(AzureVmError):      # wait_for_ip / wait_ready timed out
class SshConnectionError(AzureVmError):       # SSH unreachable or auth failure
```

### `models.py`

```python
class VmState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"  # deallocated in Azure
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


@dataclass
class VmInfo:
    name: str
    state: VmState
    ipv4: list[str]
    location: str
    vm_size: str
    image_urn: str
    resource_group: str

    @classmethod
    def from_tofu_output(cls, data: dict, name: str) -> "VmInfo": ...
    @classmethod
    def from_list_item(cls, data: dict) -> "VmInfo": ...


@dataclass
class ImageInfo:
    publisher: str
    offer: str
    sku: str
    version: str

    @classmethod
    def from_az_image_list(cls, data: dict) -> list["ImageInfo"]: ...
```

### `vm.py`

```python
class AzureVM:
    def __init__(
        self,
        name: str,
        workspace_dir: Path,
        backend: CommandBackend,
        ssh_key_path: str | None = None,
    ): ...

    # Lifecycle — all via tofu
    def info(self) -> VmInfo: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def delete(self) -> None: ...

    # SSH
    def exec(self, command: list[str]) -> CommandResult: ...
    def exec_structured(self, argv, *, env=None, cwd=None) -> CommandResult: ...
    def transfer(self, source: str, dest: str) -> None: ...
    def wait_for_ip(self, timeout=120, interval=2.0) -> str: ...
    def wait_ready(self, timeout=120, port=22, interval=2.0) -> str: ...

    # Clone
    def clone(self, new_name: str) -> "AzureVM": ...
```

`start`/`stop`/`restart` toggle a `desired_state` Terraform variable (`"running"` or
`"stopped"`) and run `tofu apply`. This lets OpenTofu compute the correct Azure API
calls (start VM, deallocate, etc.).

`exec` and `transfer` use paramiko SSH. `exec_structured` mirrors the multipass-sdk
signature: builds a minimal bash invocation from structured `argv`, `env`, and `cwd`.

### `client.py`

```python
class AzureClient:
    def __init__(
        self,
        resource_group: str | None = None,
        location: str | None = None,
        work_dir: str | Path = "~/.azure-vm-sdk",
        backend: CommandBackend | None = None,
    ): ...

    def get_vm(self, name: str) -> AzureVM: ...

    def launch(
        self,
        name: str | None = None,
        image: str = "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest",
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        cloud_init_config: dict | str | None = None,
        ssh_public_key: str | None = None,
    ) -> AzureVM: ...

    def ensure_running(
        self,
        name,
        *,
        image=None,
        vm_size="Standard_B1s",
        disk_size_gb=30,
        cloud_init_config=None,
        ssh_public_key=None,
    ) -> AzureVM: ...

    def list(self) -> list[VmInfo]: ...
    def find(self, publisher="Canonical") -> list[ImageInfo]: ...
    def purge(self) -> None: ...
```

- If `resource_group` or `location` are `None`, they are read from env vars
  `AZURE_RESOURCE_GROUP` and `AZURE_LOCATION`. If still missing, launch raises an error.
- If `name` is `None`, a random name is generated (e.g., `vm-<adjective>-<noun>`).
- `list` iterates workspace directories in `work_dir` (skipping `.shared`), runs
  `tofu output -json` in each, and returns a `VmInfo` per VM. Only VMs managed by the
  SDK are listed.
- `find` shells out to `az vm image list` — it's read-only and doesn't require tofu state.
- `ensure_running` implements the same state machine as multipass-sdk:
  - `VmNotFoundError` → launch
  - `DELETED` state → purge + launch
  - `RUNNING` → no-op
  - any other → start

## HCL generation

Each VM workspace gets a `main.tf` that defines:

1. `azurerm` provider (subscription_id from env)
2. `data.azurerm_subnet.shared` — references the subnet from `.shared` workspace
3. `azurerm_public_ip` — dynamic public IP for SSH
4. `azurerm_network_interface` — attached to shared subnet + public IP
5. `azurerm_linux_virtual_machine` — with `admin_ssh_key`, `custom_data` (cloud-init),
   `source_image_reference` (parsed from URN)

The `.shared` workspace defines:

1. `azurerm_resource_group` (if not referencing an existing one)
2. `azurerm_virtual_network`
3. `azurerm_subnet`

## Dependencies

```toml
[project]
name = "azure-vm-sdk"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "paramiko>=3.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
]
```

- `paramiko` for SSH operations (exec, transfer)
- `pyyaml` for cloud-init config serialization
- No Azure SDK dependency — all Azure interaction goes through tofu and `az` CLI

## Testing strategy

Same two-layer approach as multipass-sdk:

- **Unit tests**: inject a `FakeBackend` with pre-built `CommandResult` responses.
  Assert on generated HCL and command args. SSH methods are mocked.
- **Integration tests**: require OpenTofu, `az` CLI, and valid Azure credentials.
  Run against a real Azure subscription. Marked with `@pytest.mark.integration`.

```bash
# Unit tests (no Azure required)
uv run pytest tests/unit/ -v

# Integration tests (Azure + tofu required)
uv run pytest -m integration -v
```
