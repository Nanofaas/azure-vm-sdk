# Azure VM SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python SDK that wraps OpenTofu to manage Azure Linux VMs, following the same architecture as multipass-sdk.

**Architecture:** Protocol-based backend with FakeBackend for testing, 5 source modules mirroring multipass-sdk structure. OpenTofu CLI for VM lifecycle, paramiko SSH for runtime operations (exec, transfer). Shared VNet/subnet workspace with per-VM workspaces under `~/.azure-vm-sdk/`.

**Tech Stack:** Python 3.11+, OpenTofu CLI, Azure CLI (for `az vm image list` and runtime power control), paramiko (SSH), pytest

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/azure_vm/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`
- Create: `tests/unit/__init__.py` (empty)

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "azure-vm-sdk"
version = "0.1.0"
description = "Python SDK for managing Azure VMs via OpenTofu"
readme = "README.md"
license = { text = "MIT" }
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

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/azure_vm"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "integration: marks tests as integration tests (require Azure + OpenTofu installed)",
]
addopts = "-m 'not integration'"
```

- [ ] **Step 2: Create .gitignore**

```
__pycache__/
.pytest_cache/
.venv/
*.pyc
*.egg-info/
dist/
uv.lock
```

- [ ] **Step 3: Create empty init files and conftest.py**

`src/azure_vm/__init__.py` — empty for now (filled in Task 7)

`tests/__init__.py` — empty

`tests/unit/__init__.py` — empty

`tests/conftest.py`:
```python
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (require Azure + OpenTofu installed)",
    )
```

- [ ] **Step 4: Verify scaffolding**

Run: `uv sync --extra dev`
Expected: succeeds, creates `.venv`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src/ tests/
git commit -m "chore: scaffold azure-vm-sdk project structure"
```

---

### Task 2: `_backend.py` — CommandResult, Protocol, TofuBackend, FakeBackend

**Files:**
- Create: `src/azure_vm/_backend.py`
- Create: `tests/unit/test_backend.py`

- [ ] **Step 1: Write failing tests for CommandResult**

`tests/unit/test_backend.py`:
```python
import pytest
from azure_vm._backend import CommandResult, FakeBackend


def test_command_result_success():
    result = CommandResult(
        args=["tofu", "output"], returncode=0, stdout='{"vm_ip":""}', stderr=""
    )
    assert result.success is True


def test_command_result_failure():
    result = CommandResult(
        args=["tofu", "apply"], returncode=1, stdout="", stderr="error"
    )
    assert result.success is False


def test_fake_backend_records_calls():
    backend = FakeBackend()
    ok = CommandResult(args=[], returncode=0, stdout="", stderr="")
    backend.set_default(ok)
    backend.run(["tofu", "output", "-json"], cwd="/tmp/vm1")
    backend.run(["tofu", "apply", "-auto-approve"], cwd="/tmp/vm1")
    assert backend.calls == [
        ["tofu", "output", "-json"],
        ["tofu", "apply", "-auto-approve"],
    ]


def test_fake_backend_returns_configured_response():
    expected = CommandResult(
        args=["tofu", "output", "-json"],
        returncode=0,
        stdout='{"vm_ip":{"value":"1.2.3.4"}}',
        stderr="",
    )
    backend = FakeBackend(responses={("tofu", "output", "-json"): expected})
    result = backend.run(["tofu", "output", "-json"])
    assert result.stdout == '{"vm_ip":{"value":"1.2.3.4"}}'


def test_fake_backend_raises_on_unconfigured_call():
    backend = FakeBackend()
    with pytest.raises(KeyError):
        backend.run(["tofu", "unknown"])


def test_fake_backend_last_call():
    backend = FakeBackend()
    ok = CommandResult(args=[], returncode=0, stdout="", stderr="")
    backend.set_default(ok)
    backend.run(["tofu", "init"])
    assert backend.last_call() == ["tofu", "init"]


def test_fake_backend_push_consumes_in_order():
    backend = FakeBackend()
    r1 = CommandResult(args=[], returncode=0, stdout="first", stderr="")
    r2 = CommandResult(args=[], returncode=0, stdout="second", stderr="")
    backend.push("tofu", "apply", result=r1)
    backend.push("tofu", "apply", result=r2)
    assert backend.run(["tofu", "apply"]).stdout == "first"
    assert backend.run(["tofu", "apply"]).stdout == "second"


def test_fake_backend_cwd_is_recorded():
    backend = FakeBackend()
    ok = CommandResult(args=[], returncode=0, stdout="", stderr="")
    backend.set_default(ok)
    backend.run(["tofu", "init"], cwd="/home/user/.azure-vm-sdk/my-vm")
    assert backend.cwds == ["/home/user/.azure-vm-sdk/my-vm"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_backend.py -v`
Expected: all FAIL with import errors

- [ ] **Step 3: Implement `_backend.py`**

```python
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


class CommandBackend(Protocol):
    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


class TofuBackend:
    """Real backend — invokes the OpenTofu CLI via subprocess."""

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, cwd=cwd, env=env
            )
        except FileNotFoundError:
            from .exceptions import TofuNotInstalledError

            raise TofuNotInstalledError()
        return CommandResult(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


class FakeBackend:
    """Test backend — returns pre-configured responses and records all calls."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], CommandResult] | None = None,
    ) -> None:
        self._responses: dict[tuple[str, ...], CommandResult] = responses or {}
        self._queues: dict[tuple[str, ...], list[CommandResult]] = {}
        self._calls: list[list[str]] = []
        self._cwds: list[str | None] = []
        self._default: CommandResult | None = None

    def set_default(self, result: CommandResult) -> None:
        self._default = result

    def push(self, *args: str, result: CommandResult) -> None:
        self._queues.setdefault(args, []).append(result)

    def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        self._calls.append(list(args))
        self._cwds.append(cwd)
        key = tuple(args)
        if key in self._queues and self._queues[key]:
            return self._queues[key].pop(0)
        if key in self._responses:
            return self._responses[key]
        if self._default is not None:
            return self._default
        raise KeyError(f"FakeBackend: no response configured for {args!r}")

    @property
    def calls(self) -> list[list[str]]:
        return list(self._calls)

    @property
    def cwds(self) -> list[str | None]:
        return list(self._cwds)

    def last_call(self) -> list[str]:
        return self._calls[-1] if self._calls else []

    def last_cwd(self) -> str | None:
        return self._cwds[-1] if self._cwds else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_backend.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/_backend.py tests/unit/test_backend.py
git commit -m "feat: add CommandResult, CommandBackend Protocol, TofuBackend, FakeBackend"
```

---

### Task 3: `exceptions.py` — Typed exception hierarchy

**Files:**
- Create: `src/azure_vm/exceptions.py`
- Create: `tests/unit/test_exceptions.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/test_exceptions.py`:
```python
from azure_vm.exceptions import (
    AzureVmError,
    AzureVmCommandError,
    TofuNotInstalledError,
    VmNotFoundError,
    VmAlreadyRunningError,
    VmNotRunningError,
    AzureVmTimeoutError,
    SshConnectionError,
)


def test_all_exceptions_are_subclasses_of_azure_vm_error():
    assert issubclass(AzureVmCommandError, AzureVmError)
    assert issubclass(TofuNotInstalledError, AzureVmError)
    assert issubclass(VmNotFoundError, AzureVmError)
    assert issubclass(VmAlreadyRunningError, AzureVmError)
    assert issubclass(VmNotRunningError, AzureVmError)
    assert issubclass(AzureVmTimeoutError, AzureVmError)
    assert issubclass(SshConnectionError, AzureVmError)


def test_azure_vm_command_error_stores_fields():
    err = AzureVmCommandError(["tofu", "apply"], 1, "", "resource not found")
    assert err.returncode == 1
    assert err.stderr == "resource not found"
    assert "tofu" in str(err)


def test_tofu_not_installed_error_has_message():
    err = TofuNotInstalledError()
    assert "OpenTofu" in str(err)


def test_vm_not_found_error_includes_name():
    err = VmNotFoundError("my-vm")
    assert err.name == "my-vm"
    assert "my-vm" in str(err)


def test_vm_already_running_error_includes_name():
    err = VmAlreadyRunningError("my-vm")
    assert err.name == "my-vm"
    assert "my-vm" in str(err)


def test_vm_not_running_error_includes_name():
    err = VmNotRunningError("my-vm")
    assert err.name == "my-vm"
    assert "my-vm" in str(err)


def test_azure_vm_timeout_error_stores_fields():
    err = AzureVmTimeoutError("my-vm", 120)
    assert err.name == "my-vm"
    assert err.timeout == 120
    assert "120" in str(err)


def test_ssh_connection_error_stores_fields():
    err = SshConnectionError("my-vm", "1.2.3.4", "Connection refused")
    assert err.name == "my-vm"
    assert err.host == "1.2.3.4"
    assert "1.2.3.4" in str(err)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_exceptions.py -v`
Expected: all FAIL

- [ ] **Step 3: Implement `exceptions.py`**

```python
class AzureVmError(Exception):
    """Base exception for all azure-vm-sdk errors."""


class AzureVmCommandError(AzureVmError):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command {args} failed with exit code {returncode}: {stderr or stdout}"
        )


class TofuNotInstalledError(AzureVmError):
    def __init__(self) -> None:
        super().__init__("OpenTofu not found. Install from https://opentofu.org")


class VmNotFoundError(AzureVmError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' not found")


class VmAlreadyRunningError(AzureVmError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' is already running")


class VmNotRunningError(AzureVmError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"VM '{name}' is not running")


class AzureVmTimeoutError(AzureVmError):
    def __init__(self, name: str, timeout: float) -> None:
        self.name = name
        self.timeout = timeout
        super().__init__(f"VM '{name}' did not become ready within {timeout}s")


class SshConnectionError(AzureVmError):
    def __init__(self, name: str, host: str, reason: str) -> None:
        self.name = name
        self.host = host
        self.reason = reason
        super().__init__(f"SSH connection to '{name}' ({host}) failed: {reason}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_exceptions.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/exceptions.py tests/unit/test_exceptions.py
git commit -m "feat: add typed exception hierarchy"
```

---

### Task 4: `models.py` — VmState, VmInfo, ImageInfo

**Files:**
- Create: `src/azure_vm/models.py`
- Create: `tests/unit/test_models.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/test_models.py`:
```python
from azure_vm.models import VmInfo, VmState, ImageInfo


TOFU_OUTPUT = {
    "vm_ip": {"value": "1.2.3.4"},
    "vm_state": {"value": "running"},
    "location": {"value": "westeurope"},
    "vm_size": {"value": "Standard_B1s"},
    "image_urn": {"value": "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"},
    "resource_group": {"value": "my-rg"},
}


def test_vmstate_known_values():
    assert VmState.RUNNING.value == "running"
    assert VmState.STOPPED.value == "stopped"
    assert VmState.STARTING.value == "starting"
    assert VmState.STOPPING.value == "stopping"
    assert VmState.UNKNOWN.value == "unknown"


def test_vmstate_missing_falls_back_to_unknown():
    assert VmState("nonexistent") == VmState.UNKNOWN


def test_vminfo_from_tofu_output():
    info = VmInfo.from_tofu_output(TOFU_OUTPUT, "my-vm")
    assert info.name == "my-vm"
    assert info.state == VmState.RUNNING
    assert info.ipv4 == ["1.2.3.4"]
    assert info.location == "westeurope"
    assert info.vm_size == "Standard_B1s"
    assert info.image_urn == "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
    assert info.resource_group == "my-rg"


def test_vminfo_from_tofu_output_empty_ip():
    data = {**TOFU_OUTPUT, "vm_ip": {"value": ""}}
    info = VmInfo.from_tofu_output(data, "vm-no-ip")
    assert info.ipv4 == []


def test_vminfo_from_tofu_output_missing_ip():
    data = {k: v for k, v in TOFU_OUTPUT.items() if k != "vm_ip"}
    info = VmInfo.from_tofu_output(data, "vm-no-ip")
    assert info.ipv4 == []


def test_vminfo_from_tofu_output_unknown_state():
    data = {**TOFU_OUTPUT, "vm_state": {"value": "transitioning"}}
    info = VmInfo.from_tofu_output(data, "vm")
    assert info.state == VmState.UNKNOWN


def test_vminfo_from_list_item():
    item = {
        "name": "vm-1",
        "state": "running",
        "ip": "10.0.0.1",
        "location": "westeurope",
        "vm_size": "Standard_B2s",
        "image_urn": "Canonical:ubuntu-24_04-lts:server:latest",
        "resource_group": "my-rg",
    }
    info = VmInfo.from_list_item(item)
    assert info.name == "vm-1"
    assert info.state == VmState.RUNNING


AZ_IMAGE_LIST = [
    {
        "publisher": "Canonical",
        "offer": "0001-com-ubuntu-server-noble",
        "sku": "24_04-lts",
        "version": "latest",
    },
    {
        "publisher": "Canonical",
        "offer": "0001-com-ubuntu-server-jammy",
        "sku": "22_04-lts",
        "version": "latest",
    },
]


def test_imageinfo_from_az_image_list():
    images = ImageInfo.from_az_image_list(AZ_IMAGE_LIST)
    assert len(images) == 2
    assert images[0].publisher == "Canonical"
    assert images[0].offer == "0001-com-ubuntu-server-noble"
    assert images[0].sku == "24_04-lts"
    assert images[1].sku == "22_04-lts"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_models.py -v`
Expected: all FAIL

- [ ] **Step 3: Implement `models.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VmState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "VmState":
        return cls.UNKNOWN


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
    def from_tofu_output(cls, data: dict, name: str) -> "VmInfo":
        ip = data.get("vm_ip", {}).get("value", "")
        state_raw = data.get("vm_state", {}).get("value", "unknown")
        return cls(
            name=name,
            state=VmState(state_raw),
            ipv4=[ip] if ip else [],
            location=data.get("location", {}).get("value", ""),
            vm_size=data.get("vm_size", {}).get("value", ""),
            image_urn=data.get("image_urn", {}).get("value", ""),
            resource_group=data.get("resource_group", {}).get("value", ""),
        )

    @classmethod
    def from_list_item(cls, data: dict) -> "VmInfo":
        return cls(
            name=data.get("name", ""),
            state=VmState(data.get("state", "unknown")),
            ipv4=[data["ip"]] if data.get("ip") else [],
            location=data.get("location", ""),
            vm_size=data.get("vm_size", ""),
            image_urn=data.get("image_urn", ""),
            resource_group=data.get("resource_group", ""),
        )


@dataclass
class ImageInfo:
    publisher: str
    offer: str
    sku: str
    version: str

    @classmethod
    def from_az_image_list(cls, data: list[dict]) -> list["ImageInfo"]:
        return [
            cls(
                publisher=img.get("publisher", ""),
                offer=img.get("offer", ""),
                sku=img.get("sku", ""),
                version=img.get("version", ""),
            )
            for img in data
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_models.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/models.py tests/unit/test_models.py
git commit -m "feat: add VmState, VmInfo, ImageInfo models"
```

---

### Task 5: `vm.py` — AzureVM class

**Files:**
- Create: `src/azure_vm/vm.py`
- Create: `tests/unit/test_vm.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/test_vm.py`:
```python
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.exceptions import (
    AzureVmCommandError,
    AzureVmTimeoutError,
    SshConnectionError,
)
from azure_vm.models import VmState
from azure_vm.vm import AzureVM


OUTPUT_JSON = json.dumps(
    {
        "vm_ip": {"value": "1.2.3.4"},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {
            "value": "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
        },
        "resource_group": {"value": "my-rg"},
    }
)

OUTPUT_NO_IP = json.dumps(
    {
        "vm_ip": {"value": ""},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {
            "value": "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
        },
        "resource_group": {"value": "my-rg"},
    }
)

OUTPUT_WITH_IP = json.dumps(
    {
        "vm_ip": {"value": "1.2.3.5"},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {
            "value": "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
        },
        "resource_group": {"value": "my-rg"},
    }
)


def make_ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout=stdout, stderr="")


def make_err(stderr: str, returncode: int = 1) -> CommandResult:
    return CommandResult(args=[], returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------- info


def test_info_returns_vm_info():
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
        }
    )
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    info = vm.info()
    assert info.name == "my-vm"
    assert info.state == VmState.RUNNING
    assert info.ipv4 == ["1.2.3.4"]


def test_info_raises_command_error_on_failure():
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_err("state not found"),
        }
    )
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with pytest.raises(AzureVmCommandError):
        vm.info()


# ------------------------------------------------------------ lifecycle


def test_start_applies_with_running_state():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    vm.start()
    assert backend.last_call()[:3] == ["tofu", "apply", "-auto-approve"]
    assert any("desired_state=running" in arg for arg in backend.last_call())


def test_stop_applies_with_stopped_state():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    vm.stop()
    assert backend.last_call()[:3] == ["tofu", "apply", "-auto-approve"]
    assert any("desired_state=stopped" in arg for arg in backend.last_call())


def test_restart_applies_with_restart_state():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    vm.restart()
    assert backend.last_call()[:3] == ["tofu", "apply", "-auto-approve"]
    assert any("desired_state=restart" in arg for arg in backend.last_call())


def test_delete_runs_destroy():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    vm.delete()
    assert backend.last_call() == ["tofu", "destroy", "-auto-approve"]


def test_lifecycle_raises_on_failure():
    backend = FakeBackend()
    backend.set_default(make_err("apply failed"))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with pytest.raises(AzureVmCommandError):
        vm.start()


# ---------------------------------------------------------------- exec


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_runs_command_over_ssh(mock_ssh_client):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdin = MagicMock()
    stdout = MagicMock()
    stderr = MagicMock()
    stdout.read.return_value = b"hello\n"
    stderr.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (stdin, stdout, stderr)

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend, ssh_key_path="/key.pem")
    result = vm.exec(["ls", "-la"])

    ssh.connect.assert_called_once_with(
        hostname="1.2.3.4",
        username="azureuser",
        key_filename="/key.pem",
        timeout=10,
    )
    assert result.stdout == "hello\n"
    assert result.success is True


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_raises_ssh_connection_error(mock_ssh_client):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    ssh.connect.side_effect = OSError("Connection refused")

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with pytest.raises(SshConnectionError) as exc_info:
        vm.exec(["ls"])
    assert exc_info.value.host == "1.2.3.4"


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_structured_builds_bash_command(mock_ssh_client):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdin = MagicMock()
    stdout = MagicMock()
    stderr = MagicMock()
    stdout.read.return_value = b"ok\n"
    stderr.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (stdin, stdout, stderr)

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend, ssh_key_path="/key.pem")
    vm.exec_structured(
        ["python", "train.py"],
        env={"CUDA_VISIBLE_DEVICES": "0"},
        cwd="/home/azureuser/project",
    )

    command = ssh.exec_command.call_args[0][0]
    assert "cd /home/azureuser/project" in command
    assert "export CUDA_VISIBLE_DEVICES=0" in command
    assert "python train.py" in command


# ------------------------------------------------------------- transfer


@patch("azure_vm.vm.paramiko.SSHClient")
def test_transfer_sends_file(mock_ssh_client, tmp_path):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    sftp = MagicMock()
    ssh.open_sftp.return_value = sftp

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    local_file = tmp_path / "test.txt"
    local_file.write_text("content")
    vm.transfer(str(local_file), "/home/azureuser/test.txt")

    sftp.put.assert_called_once()


# --------------------------------------------------------------- clone


def test_clone_returns_new_vm():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    new_vm = vm.clone("my-vm-clone")
    assert new_vm.name == "my-vm-clone"
    assert new_vm._workspace_dir == Path("/tmp/ws/my-vm-clone")


# --------------------------------------------------------- wait_for_ip

OUTPUT_KEY = ("tofu", "output", "-json")


@patch("azure_vm.vm.time.sleep")
def test_wait_for_ip_returns_ip_when_ready(mock_sleep):
    backend = FakeBackend()
    backend.push(*OUTPUT_KEY, result=make_ok(OUTPUT_NO_IP))
    backend.push(*OUTPUT_KEY, result=make_ok(OUTPUT_WITH_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with patch("azure_vm.vm.time.monotonic", side_effect=[0, 10, 20]):
        ip = vm.wait_for_ip(timeout=120, interval=2.0)
    assert ip == "1.2.3.5"
    mock_sleep.assert_called_once_with(2.0)


@patch("azure_vm.vm.time.sleep")
def test_wait_for_ip_raises_timeout(mock_sleep):
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_NO_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with patch("azure_vm.vm.time.monotonic", side_effect=[0, 130]):
        with pytest.raises(AzureVmTimeoutError) as exc_info:
            vm.wait_for_ip(timeout=120)
    assert exc_info.value.name == "my-vm"
    assert exc_info.value.timeout == 120


# ---------------------------------------------------------- wait_ready


@patch("azure_vm.vm.time.sleep")
@patch("azure_vm.vm.socket.create_connection")
def test_wait_ready_returns_ip_when_ssh_reachable(mock_conn, mock_sleep):
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_WITH_IP))
    mock_conn.return_value.__enter__ = MagicMock(return_value=None)
    mock_conn.return_value.__exit__ = MagicMock(return_value=False)
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with patch("azure_vm.vm.time.monotonic", side_effect=[0, 10]):
        ip = vm.wait_ready(timeout=120, port=22)
    assert ip == "1.2.3.5"
    mock_conn.assert_called_once_with(("1.2.3.5", 22), timeout=1)


@patch("azure_vm.vm.time.sleep")
@patch("azure_vm.vm.socket.create_connection", side_effect=OSError)
def test_wait_ready_raises_timeout_when_port_unreachable(mock_conn, mock_sleep):
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_WITH_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with patch("azure_vm.vm.time.monotonic", side_effect=[0, 130]):
        with pytest.raises(AzureVmTimeoutError):
            vm.wait_ready(timeout=120, port=22)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_vm.py -v`
Expected: all FAIL

- [ ] **Step 3: Implement `vm.py`**

```python
from __future__ import annotations

import json
import shlex
import socket
import time
from pathlib import Path

import paramiko

from ._backend import CommandBackend, CommandResult
from .exceptions import (
    AzureVmCommandError,
    AzureVmTimeoutError,
    SshConnectionError,
)
from .models import VmInfo


class AzureVM:
    def __init__(
        self,
        name: str,
        workspace_dir: Path,
        backend: CommandBackend,
        ssh_key_path: str | None = None,
        ssh_username: str = "azureuser",
    ) -> None:
        self.name = name
        self._workspace_dir = workspace_dir
        self._backend = backend
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username

    def _run(self, args: list[str]) -> CommandResult:
        result = self._backend.run(args, cwd=str(self._workspace_dir))
        if not result.success:
            raise AzureVmCommandError(
                result.args, result.returncode, result.stdout, result.stderr
            )
        return result

    def _ip(self) -> str:
        result = self._run(["tofu", "output", "-json"])
        data = json.loads(result.stdout)
        ip = data.get("vm_ip", {}).get("value", "")
        if ip:
            return ip
        return ""

    # ------------------------------------------------------------ lifecycle

    def info(self) -> VmInfo:
        result = self._run(["tofu", "output", "-json"])
        return VmInfo.from_tofu_output(json.loads(result.stdout), self.name)

    def start(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=running"])

    def stop(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=stopped"])

    def restart(self) -> None:
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=restart"])

    def delete(self) -> None:
        self._run(["tofu", "destroy", "-auto-approve"])

    # ---------------------------------------------------------------- SSH

    def _ssh_client(self) -> paramiko.SSHClient:
        ip = self._ip()
        if not ip:
            raise AzureVmTimeoutError(self.name, 0)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=ip,
                username=self._ssh_username,
                key_filename=self._ssh_key_path,
                timeout=10,
            )
        except (OSError, paramiko.SSHException) as e:
            raise SshConnectionError(self.name, ip, str(e)) from e
        return ssh

    def exec(self, command: list[str]) -> CommandResult:
        ssh = self._ssh_client()
        try:
            _, stdout, stderr = ssh.exec_command(shlex.join(command))
            exit_status = stdout.channel.recv_exit_status()
            result = CommandResult(
                args=command,
                returncode=exit_status,
                stdout=stdout.read().decode("utf-8", errors="replace"),
                stderr=stderr.read().decode("utf-8", errors="replace"),
            )
            return result
        finally:
            ssh.close()

    def exec_structured(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={shlex.quote(v)}")
        parts.append(shlex.join(argv))
        command = " && ".join(parts)
        return self.exec(["bash", "-lc", command])

    def transfer(self, source: str, dest: str) -> None:
        ssh = self._ssh_client()
        try:
            sftp = ssh.open_sftp()
            if ":" in source:
                sftp.get(source, dest)
            else:
                sftp.put(source, dest)
            sftp.close()
        finally:
            ssh.close()

    # --------------------------------------------------------------- clone

    def clone(self, new_name: str) -> "AzureVM":
        new_ws = self._workspace_dir.parent / new_name
        import shutil

        shutil.copytree(self._workspace_dir, new_ws, dirs_exist_ok=True)
        self._backend.run(
            ["tofu", "apply", "-auto-approve", "-var", f"vm_name={new_name}"],
            cwd=str(new_ws),
        )
        return AzureVM(
            new_name, new_ws, self._backend, self._ssh_key_path, self._ssh_username
        )

    # --------------------------------------------------------- wait_for_ip

    def wait_for_ip(self, timeout: float = 120, *, interval: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                return ip
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)

    # ---------------------------------------------------------- wait_ready

    def wait_ready(
        self, timeout: float = 120, port: int = 22, *, interval: float = 2.0
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                try:
                    with socket.create_connection((ip, port), timeout=1):
                        return ip
                except OSError:
                    pass
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_vm.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/vm.py tests/unit/test_vm.py
git commit -m "feat: add AzureVM with lifecycle, SSH exec/transfer, wait, clone"
```

---

### Task 6: `client.py` — AzureClient with workspace management and HCL generation

**Files:**
- Create: `src/azure_vm/client.py`
- Create: `tests/unit/test_client.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/test_client.py`:
```python
import json
from pathlib import Path
import pytest
from azure_vm._backend import CommandResult, FakeBackend
from azure_vm.client import AzureClient
from azure_vm.exceptions import AzureVmCommandError
from azure_vm.models import VmState
from azure_vm.vm import AzureVM


OUTPUT_JSON = json.dumps(
    {
        "vm_ip": {"value": "1.2.3.4"},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {
            "value": "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest"
        },
        "resource_group": {"value": "my-rg"},
    }
)


def make_ok(stdout: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout=stdout, stderr="")


def make_err(stderr: str = "error") -> CommandResult:
    return CommandResult(args=[], returncode=1, stdout="", stderr=stderr)


# ------------------------------------------------------------ get_vm


def test_get_vm_returns_azure_vm():
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=FakeBackend(),
    )
    vm = client.get_vm("my-vm")
    assert isinstance(vm, AzureVM)
    assert vm.name == "my-vm"


# ------------------------------------------------------------ launch


def test_launch_creates_workspace_and_runs_tofu(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.launch(name="test-vm")
    assert vm.name == "test-vm"
    assert (ws / "test-vm").is_dir()
    assert (ws / "test-vm" / "main.tf").exists()
    calls = backend.calls
    assert any(call[0] == "tofu" and call[1] == "init" for call in calls)
    assert any(call[0] == "tofu" and call[1] == "apply" for call in calls)


def test_launch_shared_infra_created_on_first_call(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="vm1")
    assert (ws / ".shared" / "main.tf").exists()
    shared_init = [
        call
        for call, cwd in zip(backend.calls, backend.cwds)
        if "init" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_init) == 1


def test_launch_shared_infra_skipped_on_second_call(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir(parents=True)
    (ws / ".shared" / "main.tf").write_text("")
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="vm2")
    shared_apply = [
        call
        for call, cwd in zip(backend.calls, backend.cwds)
        if "apply" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_apply) == 0


def test_launch_generates_random_name_when_none():
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    vm = client.launch()
    assert vm.name
    assert len(vm.name) > 0


def test_launch_with_cloud_init_config_dict(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(
        name="test-vm",
        cloud_init_config={"packages": ["git", "curl"]},
    )
    workspace = ws / "test-vm"
    main = workspace / "main.tf"
    content = main.read_text()
    assert "custom_data" in content
    cloud_init = workspace / "cloud-init.yaml"
    assert cloud_init.exists()
    cloud_content = cloud_init.read_text()
    assert "git" in cloud_content
    assert "#cloud-config" in cloud_content


def test_launch_with_vm_size_and_disk(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.launch(name="test-vm", vm_size="Standard_D2s_v3", disk_size_gb=100)
    tfvars = (ws / "test-vm" / "terraform.tfvars").read_text()
    assert "Standard_D2s_v3" in tfvars
    assert "100" in tfvars


def test_launch_raises_on_failure():
    backend = FakeBackend()
    backend.set_default(make_err("launch failed"))
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    with pytest.raises(AzureVmCommandError):
        client.launch(name="bad-vm")


def test_launch_raises_when_missing_rg():
    backend = FakeBackend()
    client = AzureClient(work_dir="/tmp/ws", backend=backend)
    with pytest.raises(AzureVmCommandError) as exc_info:
        client.launch(name="vm")
    assert "AZURE_RESOURCE_GROUP" in str(exc_info.value)


# --------------------------------------------------------------- list


def test_list_returns_vms_from_workspace(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    vm_ws = ws / "vm-a"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
        }
    )
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 1
    assert vms[0].name == "vm-a"
    assert vms[0].state == VmState.RUNNING


def test_list_skips_shared_and_non_dirs(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "readme.txt").write_text("not a vm")
    vm_ws = ws / "real-vm"
    vm_ws.mkdir()
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
        }
    )
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vms = client.list()
    assert len(vms) == 1
    assert vms[0].name == "real-vm"


# --------------------------------------------------------------- find

AZ_IMAGE_LIST = json.dumps(
    [
        {
            "offer": "0001-com-ubuntu-server-noble",
            "publisher": "Canonical",
            "sku": "24_04-lts",
            "version": "latest",
        },
    ]
)


def test_find_returns_image_list():
    backend = FakeBackend(
        {
            (
                "az",
                "vm",
                "image",
                "list",
                "--publisher",
                "Canonical",
                "--all",
                "--output",
                "json",
            ): make_ok(AZ_IMAGE_LIST),
        }
    )
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    images = client.find()
    assert len(images) == 1
    assert images[0].sku == "24_04-lts"


def test_find_with_custom_publisher():
    backend = FakeBackend(
        {
            (
                "az",
                "vm",
                "image",
                "list",
                "--publisher",
                "Debian",
                "--all",
                "--output",
                "json",
            ): make_ok(AZ_IMAGE_LIST),
        }
    )
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir="/tmp/ws",
        backend=backend,
    )
    images = client.find(publisher="Debian")
    assert len(images) == 1


# -------------------------------------------------------------- purge


def test_purge_destroys_all_vm_workspaces(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    (ws / "vm-a").mkdir()
    (ws / "vm-a" / "main.tf").write_text("")
    (ws / "vm-b").mkdir()
    (ws / "vm-b" / "main.tf").write_text("")
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    destroy_calls = [c for c in backend.calls if c[1] == "destroy"]
    assert len(destroy_calls) == 2
    destroy_cwds = [cwd for cwd in backend.cwds if cwd and "vm-" in cwd]
    assert len(destroy_cwds) == 2


def test_purge_preserves_shared(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    (ws / ".shared").mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    client.purge()
    shared_destroy = [
        call
        for call, cwd in zip(backend.calls, backend.cwds)
        if "destroy" in call and cwd and ".shared" in cwd
    ]
    assert len(shared_destroy) == 0


# ---------------------------------------------------- ensure_running


def test_ensure_running_launches_when_not_found(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    backend = FakeBackend()
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("new-vm")
    assert vm.name == "new-vm"
    assert any(call[1] == "apply" for call in backend.calls)


def test_ensure_running_is_noop_when_running(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir(parents=True)
    (vm_ws / "main.tf").write_text("")
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_ok(OUTPUT_JSON),
        }
    )
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("my-vm")
    assert vm.name == "my-vm"
    assert not any(call[1] == "launch" for call in backend.calls)


def test_ensure_running_starts_stopped_vm(tmp_path):
    ws = tmp_path / "azure-vm-sdk"
    ws.mkdir()
    vm_ws = ws / "my-vm"
    vm_ws.mkdir(parents=True)
    (vm_ws / "main.tf").write_text("")
    stopped = json.dumps(
        {
            "vm_ip": {"value": "1.2.3.4"},
            "vm_state": {"value": "stopped"},
            "location": {"value": "westeurope"},
            "vm_size": {"value": "Standard_B1s"},
            "image_urn": {"value": "Canonical:..."},
            "resource_group": {"value": "my-rg"},
        }
    )
    backend = FakeBackend(
        {
            ("tofu", "output", "-json"): make_ok(stopped),
        }
    )
    backend.set_default(make_ok())
    client = AzureClient(
        resource_group="my-rg",
        location="westeurope",
        work_dir=str(ws),
        backend=backend,
    )
    vm = client.ensure_running("my-vm")
    assert vm.name == "my-vm"
    assert any(
        call[1] == "apply" and "desired_state=running" in str(call)
        for call in backend.calls
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_client.py -v`
Expected: all FAIL

- [ ] **Step 3: Implement `client.py`**

```python
from __future__ import annotations

import json
import os
import random
import shutil
import string
from pathlib import Path

import yaml

from ._backend import CommandBackend, CommandResult, TofuBackend
from .exceptions import AzureVmCommandError, VmNotFoundError
from .models import ImageInfo, VmInfo, VmState
from .vm import AzureVM


_SHARED_TF = """\
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
    }
  }
}

provider "azurerm" {{
  subscription_id = "{subscription_id}"
  features {{}}
}}

resource "azurerm_resource_group" "main" {{
  name     = "{resource_group}"
  location = "{location}"
}}

resource "azurerm_virtual_network" "main" {{
  name                = "{resource_group}-vnet"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  address_space       = ["10.0.0.0/16"]
}}

resource "azurerm_subnet" "main" {{
  name                 = "{resource_group}-subnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.1.0/24"]
}}

output "subnet_id" {{
  value = azurerm_subnet.main.id
}}

output "resource_group_name" {{
  value = azurerm_resource_group.main.name
}}
"""

_VM_TF = """\
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
    }}
  }}
}}

variable "subscription_id" {{ type = string }}
variable "resource_group_name" {{ type = string }}
variable "location" {{ type = string }}
variable "vm_name" {{ type = string }}
variable "vm_size" {{ type = string }}
variable "disk_size_gb" {{ type = number }}
variable "image_publisher" {{ type = string }}
variable "image_offer" {{ type = string }}
variable "image_sku" {{ type = string }}
variable "image_version" {{ type = string }}
variable "admin_username" {{ type = string }}
variable "ssh_public_key" {{ type = string }}
variable "custom_data" {{ type = string, default = "" }}
variable "desired_state" {{ type = string, default = "running" }}

provider "azurerm" {{
  subscription_id = var.subscription_id
  features {{}}
}}

data "terraform_remote_state" "shared" {{
  backend = "local"
  config = {{
    path = "{shared_state_path}"
  }}
}}

resource "azurerm_public_ip" "vm" {{
  name                = "${{var.vm_name}}-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Dynamic"
}}

resource "azurerm_network_interface" "vm" {{
  name                = "${{var.vm_name}}-nic"
  resource_group_name = var.resource_group_name
  location            = var.location

  ip_configuration {{
    name                          = "internal"
    subnet_id                     = data.terraform_remote_state.shared.outputs.subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.vm.id
  }}
}}

resource "azurerm_linux_virtual_machine" "vm" {{
  name                  = var.vm_name
  resource_group_name   = var.resource_group_name
  location              = var.location
  size                  = var.vm_size
  admin_username        = var.admin_username
  network_interface_ids = [azurerm_network_interface.vm.id]

  admin_ssh_key {{
    username   = var.admin_username
    public_key = var.ssh_public_key
  }}

  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = var.disk_size_gb
  }}

  source_image_reference {{
    publisher = var.image_publisher
    offer     = var.image_offer
    sku       = var.image_sku
    version   = var.image_version
  }}

  custom_data = var.custom_data != "" ? var.custom_data : null
}}

resource "terraform_data" "power_control" {{
  triggers_replace = {{
    desired_state  = var.desired_state
    vm_name        = var.vm_name
    resource_group = var.resource_group_name
  }}

  provisioner "local-exec" {{
    command = <<-EOT
      case "${{var.desired_state}}" in
        running) az vm start --ids ${{azurerm_linux_virtual_machine.vm.id}} ;;
        stopped) az vm deallocate --ids ${{azurerm_linux_virtual_machine.vm.id}} ;;
        restart) az vm restart --ids ${{azurerm_linux_virtual_machine.vm.id}} ;;
      esac
    EOT
  }}
}}

output "vm_ip" {{
  value = azurerm_public_ip.vm.ip_address
}}

output "vm_state" {{
  value = var.desired_state
}}

output "location" {{
  value = var.location
}}

output "vm_size" {{
  value = var.vm_size
}}

output "image_urn" {{
  value = "${{var.image_publisher}}:${{var.image_offer}}:${{var.image_sku}}:${{var.image_version}}"
}}

output "resource_group" {{
  value = var.resource_group_name
}}
"""


def _check(result: CommandResult) -> None:
    if not result.success:
        raise AzureVmCommandError(
            result.args, result.returncode, result.stdout, result.stderr
        )


def _random_name() -> str:
    adj = random.choice(["brave", "calm", "eager", "keen", "lucid", "swift", "warm"])
    noun = random.choice(
        ["badger", "falcon", "heron", "otter", "puma", "raven", "trout"]
    )
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"vm-{adj}-{noun}-{suffix}"


class AzureClient:
    def __init__(
        self,
        resource_group: str | None = None,
        location: str | None = None,
        work_dir: str | Path = "~/.azure-vm-sdk",
        backend: CommandBackend | None = None,
    ) -> None:
        self._resource_group = resource_group or os.environ.get(
            "AZURE_RESOURCE_GROUP", ""
        )
        self._location = location or os.environ.get("AZURE_LOCATION", "")
        self._subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
        self._work_dir = Path(work_dir).expanduser()
        self._backend: CommandBackend = backend or TofuBackend()

    def _run(self, args: list[str], *, cwd: str | None = None) -> CommandResult:
        result = self._backend.run(args, cwd=cwd)
        _check(result)
        return result

    def _shared_dir(self) -> Path:
        return self._work_dir / ".shared"

    def _vm_dir(self, name: str) -> Path:
        return self._work_dir / name

    def _ensure_shared_infra(self) -> None:
        shared = self._shared_dir()
        if shared.exists():
            return
        shared.mkdir(parents=True, exist_ok=True)
        tf = _SHARED_TF.format(
            subscription_id=self._subscription_id,
            resource_group=self._resource_group,
            location=self._location,
        )
        (shared / "main.tf").write_text(tf)
        self._run(["tofu", "init"], cwd=str(shared))
        self._run(["tofu", "apply", "-auto-approve"], cwd=str(shared))

    def _parse_image_urn(self, urn: str) -> dict[str, str]:
        parts = urn.split(":")
        if len(parts) != 4:
            raise AzureVmCommandError(
                ["parse", "urn"],
                1,
                "",
                f"Invalid image URN: {urn!r}. Expected publisher:offer:sku:version",
            )
        return {
            "publisher": parts[0],
            "offer": parts[1],
            "sku": parts[2],
            "version": parts[3],
        }

    def get_vm(self, name: str) -> AzureVM:
        return AzureVM(name, self._vm_dir(name), self._backend)

    def launch(
        self,
        name: str | None = None,
        image: str = "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest",
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        cloud_init_config: dict | str | None = None,
        ssh_public_key: str | None = None,
    ) -> AzureVM:
        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                ["launch"],
                1,
                "",
                "AZURE_RESOURCE_GROUP and AZURE_LOCATION must be set via env vars "
                "or passed to AzureClient()",
            )
        if name is None:
            name = _random_name()

        self._ensure_shared_infra()

        vm_dir = self._vm_dir(name)
        vm_dir.mkdir(parents=True, exist_ok=True)

        img = self._parse_image_urn(image)

        cloud_init_content = ""
        if cloud_init_config is not None:
            if isinstance(cloud_init_config, dict):
                cloud_init_content = "#cloud-config\n" + yaml.dump(
                    cloud_init_config, default_flow_style=False
                )
            else:
                cloud_init_content = cloud_init_config
            (vm_dir / "cloud-init.yaml").write_text(cloud_init_content)

        if ssh_public_key is None:
            ssh_public_key = os.environ.get("AZURE_SSH_PUBLIC_KEY", "")

        tfvars = (
            f'subscription_id = "{self._subscription_id}"\n'
            f'resource_group_name = "{self._resource_group}"\n'
            f'location = "{self._location}"\n'
            f'vm_name = "{name}"\n'
            f'vm_size = "{vm_size}"\n'
            f"disk_size_gb = {disk_size_gb}\n"
            f'image_publisher = "{img["publisher"]}"\n'
            f'image_offer = "{img["offer"]}"\n'
            f'image_sku = "{img["sku"]}"\n'
            f'image_version = "{img["version"]}"\n'
            f'admin_username = "azureuser"\n'
            f'ssh_public_key = "{ssh_public_key}"\n'
            f'custom_data = "{cloud_init_content}"\n'
        )
        (vm_dir / "terraform.tfvars").write_text(tfvars)

        shared_state_path = self._shared_dir() / "terraform.tfstate"
        tf = _VM_TF.format(shared_state_path=shared_state_path)
        (vm_dir / "main.tf").write_text(tf)

        self._run(["tofu", "init"], cwd=str(vm_dir))
        self._run(["tofu", "apply", "-auto-approve"], cwd=str(vm_dir))

        return AzureVM(
            name,
            vm_dir,
            self._backend,
            ssh_key_path=os.environ.get("AZURE_SSH_KEY_PATH"),
        )

    def ensure_running(
        self,
        name: str,
        image: str = "Canonical:0001-com-ubuntu-server-noble:24_04-lts:latest",
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        cloud_init_config: dict | str | None = None,
        ssh_public_key: str | None = None,
    ) -> AzureVM:
        vm_dir = self._vm_dir(name)
        if not vm_dir.exists() or not (vm_dir / "main.tf").exists():
            return self.launch(
                name,
                image,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                cloud_init_config=cloud_init_config,
                ssh_public_key=ssh_public_key,
            )

        vm = self.get_vm(name)
        try:
            info = vm.info()
        except AzureVmCommandError:
            return self.launch(
                name,
                image,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                cloud_init_config=cloud_init_config,
                ssh_public_key=ssh_public_key,
            )

        if info.state == VmState.RUNNING:
            return vm

        vm.start()
        return vm

    def list(self) -> list[VmInfo]:
        if not self._work_dir.exists():
            return []
        results: list[VmInfo] = []
        for entry in sorted(self._work_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / "main.tf").exists():
                continue
            try:
                result = self._run(["tofu", "output", "-json"], cwd=str(entry))
                info = VmInfo.from_tofu_output(json.loads(result.stdout), entry.name)
                results.append(info)
            except AzureVmCommandError:
                pass
        return results

    def find(self, publisher: str = "Canonical") -> list[ImageInfo]:
        result = self._run(
            [
                "az",
                "vm",
                "image",
                "list",
                "--publisher",
                publisher,
                "--all",
                "--output",
                "json",
            ]
        )
        return ImageInfo.from_az_image_list(json.loads(result.stdout))

    def purge(self) -> None:
        if not self._work_dir.exists():
            return
        for entry in sorted(self._work_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / "main.tf").exists():
                continue
            self._run(["tofu", "destroy", "-auto-approve"], cwd=str(entry))
            shutil.rmtree(entry, ignore_errors=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_client.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/client.py tests/unit/test_client.py
git commit -m "feat: add AzureClient with launch, ensure_running, list, find, purge"
```

---

### Task 7: `__init__.py` — Public API

**Files:**
- Modify: `src/azure_vm/__init__.py`
- Modify: `tests/unit/test_client.py` (append test)

- [ ] **Step 1: Add test for public API**

Append to `tests/unit/test_client.py`:
```python
def test_public_api_importable():
    from azure_vm import (
        AzureClient,
        AzureVM,
        AzureVmError,
        AzureVmCommandError,
        TofuNotInstalledError,
        VmNotFoundError,
        VmAlreadyRunningError,
        VmNotRunningError,
        AzureVmTimeoutError,
        SshConnectionError,
        VmInfo,
        VmState,
        ImageInfo,
        CommandResult,
        FakeBackend,
        TofuBackend,
    )

    assert AzureClient is not None
    assert AzureVM is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_client.py::test_public_api_importable -v`
Expected: FAIL

- [ ] **Step 3: Implement `__init__.py`**

```python
from .client import AzureClient
from .vm import AzureVM
from .exceptions import (
    AzureVmError,
    AzureVmCommandError,
    TofuNotInstalledError,
    VmNotFoundError,
    VmAlreadyRunningError,
    VmNotRunningError,
    AzureVmTimeoutError,
    SshConnectionError,
)
from .models import VmInfo, VmState, ImageInfo
from ._backend import CommandResult, FakeBackend, TofuBackend

__all__ = [
    "AzureClient",
    "AzureVM",
    "AzureVmError",
    "AzureVmCommandError",
    "TofuNotInstalledError",
    "VmNotFoundError",
    "VmAlreadyRunningError",
    "VmNotRunningError",
    "AzureVmTimeoutError",
    "SshConnectionError",
    "VmInfo",
    "VmState",
    "ImageInfo",
    "CommandResult",
    "FakeBackend",
    "TofuBackend",
]
```

- [ ] **Step 4: Run all unit tests**

Run: `uv run pytest tests/unit/ -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/azure_vm/__init__.py tests/unit/test_client.py
git commit -m "feat: add public API re-exports in __init__.py"
```

---

### Task 8: Integration test scaffold

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_integration.py`

- [ ] **Step 1: Write integration test scaffold**

`tests/integration/test_integration.py`:
```python
"""Integration tests — require Azure credentials + OpenTofu installed.

Prerequisites:
    - az login
    - tofu installed
    - Environment variables:
        AZURE_SUBSCRIPTION_ID
        AZURE_RESOURCE_GROUP (existing)
        AZURE_LOCATION
        AZURE_SSH_PUBLIC_KEY

Run with:
    uv run pytest tests/integration/ -v -m integration
"""

import os
import pytest
from azure_vm import AzureClient


pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    required = [
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_RESOURCE_GROUP",
        "AZURE_LOCATION",
        "AZURE_SSH_PUBLIC_KEY",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        pytest.skip(f"Missing env vars: {', '.join(missing)}")

    return AzureClient()


def test_launch_and_info(client):
    vm = client.launch()
    try:
        info = vm.info()
        assert info.name == vm.name
        assert info.vm_size == "Standard_B1s"
    finally:
        vm.delete()


def test_list_includes_launched_vm(client):
    vm = client.launch()
    try:
        vms = client.list()
        names = [v.name for v in vms]
        assert vm.name in names
    finally:
        vm.delete()


def test_start_stop_cycle(client):
    vm = client.launch()
    try:
        info = vm.info()
        assert info.state.value in ("running", "starting")

        vm.stop()
        info = vm.info()
        assert info.state.value in ("stopped", "stopping")

        vm.start()
        info = vm.info()
        assert info.state.value in ("running", "starting")
    finally:
        vm.delete()


def test_wait_for_ip(client):
    vm = client.launch()
    try:
        ip = vm.wait_for_ip(timeout=180)
        assert ip
        parts = ip.split(".")
        assert len(parts) == 4
    finally:
        vm.delete()


def test_exec_echo(client):
    vm = client.launch()
    try:
        vm.wait_ready(timeout=180)
        result = vm.exec(["echo", "hello from azure"])
        assert result.success
        assert "hello from azure" in result.stdout
    finally:
        vm.delete()


def test_ensure_running_idempotent(client):
    vm = client.ensure_running("test-idempotent")
    try:
        info1 = vm.info()
        vm2 = client.ensure_running("test-idempotent")
        info2 = vm2.info()
        assert vm2.name == vm.name
    finally:
        vm.delete()
```

- [ ] **Step 2: Run unit tests to verify nothing is broken**

Run: `uv run pytest tests/unit/ -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add tests/integration/
git commit -m "test: add integration test scaffold"
```
