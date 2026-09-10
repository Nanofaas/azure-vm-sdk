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
        "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
        "resource_group": {"value": "my-rg"},
    }
)

OUTPUT_NO_IP = json.dumps(
    {
        "vm_ip": {"value": ""},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
        "resource_group": {"value": "my-rg"},
    }
)

OUTPUT_WITH_IP = json.dumps(
    {
        "vm_ip": {"value": "1.2.3.5"},
        "vm_state": {"value": "running"},
        "location": {"value": "westeurope"},
        "vm_size": {"value": "Standard_B1s"},
        "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
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


# ---------------------------------------------------------------- SSH


def test_ssh_client_raises_timeout_when_no_ip():
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_NO_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with pytest.raises(AzureVmTimeoutError) as exc_info:
        vm.exec(["ls"])
    assert exc_info.value.name == "my-vm"
    assert exc_info.value.timeout == 0


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
    second = vm.exec(["pwd"])

    mock_ssh_client.assert_called_once_with()
    ssh.connect.assert_called_once_with(
        hostname="1.2.3.4",
        username="azureuser",
        key_filename="/key.pem",
        timeout=15.0,
    )
    # a silent peer drop must surface, not hang: keepalive is enabled
    ssh.get_transport.return_value.set_keepalive.assert_called_once_with(30.0)
    assert result.stdout == "hello\n"
    assert result.success is True
    assert second.success is True
    ssh.close.assert_not_called()


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_reconnects_when_the_cached_transport_is_inactive(mock_ssh_client):
    first = MagicMock()
    first.get_transport.return_value.is_active.return_value = False
    second = MagicMock()
    second.get_transport.return_value.is_active.return_value = True
    second.get_transport.return_value.is_authenticated.return_value = True
    for ssh in (first, second):
        stdout = MagicMock()
        stdout.read.return_value = b""
        stdout.channel.recv_exit_status.return_value = 0
        ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    mock_ssh_client.side_effect = (first, second)

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    vm.exec(["true"])
    vm.exec(["true"])

    assert mock_ssh_client.call_count == 2
    first.close.assert_called_once_with()
    second.close.assert_not_called()


@patch("azure_vm.vm.paramiko.SSHClient")
def test_close_discards_the_cached_connection(mock_ssh_client):
    ssh = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    mock_ssh_client.return_value = ssh

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    vm.exec(["true"])

    vm.close()
    vm.close()

    ssh.close.assert_called_once_with()


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_discards_a_connection_that_fails_during_command(mock_ssh_client):
    failed = MagicMock()
    failed.exec_command.side_effect = OSError("connection reset")
    recovered = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    recovered.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    mock_ssh_client.side_effect = (failed, recovered)

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    with pytest.raises(OSError, match="connection reset"):
        vm.exec(["true"])
    vm.exec(["true"])

    failed.close.assert_called_once_with()
    assert mock_ssh_client.call_count == 2


@patch("azure_vm.vm.paramiko.Transport")
@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_announces_an_openssh_client_banner_by_default(
    mock_ssh_client, mock_transport
):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend, ssh_key_path="/key.pem").exec(
        ["true"]
    )

    # SSH-inspecting firewalls RST non-OpenSSH clients; paramiko must present
    # as OpenSSH via Transport._CLIENT_ID before the handshake.
    assert mock_transport._CLIENT_ID == "OpenSSH_9.6p1"


@patch("azure_vm.vm.paramiko.Transport")
@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_leaves_the_client_banner_untouched_when_disabled(
    mock_ssh_client, mock_transport
):
    original = object()
    mock_transport._CLIENT_ID = original
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    AzureVM(
        "my-vm",
        Path("/tmp/ws/my-vm"),
        backend,
        ssh_key_path="/key.pem",
        ssh_client_id=None,
    ).exec(["true"])

    assert mock_transport._CLIENT_ID is original


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_honours_custom_ssh_timeouts(mock_ssh_client):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM(
        "my-vm",
        Path("/tmp/ws/my-vm"),
        backend,
        ssh_key_path="/key.pem",
        ssh_connect_timeout=5.0,
        ssh_keepalive_interval=7.0,
    )
    vm.exec(["true"])

    assert ssh.connect.call_args.kwargs["timeout"] == 5.0
    ssh.get_transport.return_value.set_keepalive.assert_called_once_with(7.0)


@patch("azure_vm.vm.paramiko.SSHClient")
def test_exec_skips_keepalive_when_disabled(mock_ssh_client):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM(
        "my-vm",
        Path("/tmp/ws/my-vm"),
        backend,
        ssh_key_path="/key.pem",
        ssh_keepalive_interval=0,
    )
    vm.exec(["true"])

    ssh.get_transport.return_value.set_keepalive.assert_not_called()


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
def test_transfer_uses_a_dedicated_connection(mock_ssh_client, tmp_path):
    command_ssh = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    command_ssh.exec_command.return_value = (MagicMock(), stdout, MagicMock())
    transfer_ssh = MagicMock()
    mock_ssh_client.side_effect = (command_ssh, transfer_ssh)

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    vm.exec(["true"])
    vm.transfer(str(tmp_path / "file"), "/remote/file")

    assert mock_ssh_client.call_count == 2
    command_ssh.open_sftp.assert_not_called()
    command_ssh.close.assert_not_called()
    transfer_ssh.open_sftp.assert_called_once_with()
    transfer_ssh.close.assert_called_once_with()


@patch("azure_vm.vm.paramiko.SSHClient")
def test_transfer_discards_a_connection_that_fails(mock_ssh_client, tmp_path):
    ssh = MagicMock()
    sftp = ssh.open_sftp.return_value
    sftp.put.side_effect = OSError("connection reset")
    mock_ssh_client.return_value = ssh

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    with pytest.raises(OSError, match="connection reset"):
        vm.transfer(str(tmp_path / "file"), "/remote/file")

    sftp.close.assert_called_once_with()
    ssh.close.assert_called_once_with()


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


@patch("azure_vm.vm.paramiko.SSHClient")
def test_transfer_downloads_file(mock_ssh_client, tmp_path):
    ssh = MagicMock()
    mock_ssh_client.return_value = ssh
    sftp = MagicMock()
    ssh.open_sftp.return_value = sftp

    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_JSON))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)

    vm.transfer("remote:/path/to/file", str(tmp_path / "downloaded.txt"))

    sftp.get.assert_called_once_with(
        "remote:/path/to/file", str(tmp_path / "downloaded.txt")
    )


# --------------------------------------------------------------- clone


def test_clone_returns_new_vm():
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    new_vm = vm.clone("my-vm-clone")
    assert new_vm.name == "my-vm-clone"
    assert new_vm._workspace_dir == Path("/tmp/ws/my-vm-clone")


def test_clone_copies_existing_workspace(tmp_path):
    ws = tmp_path / "my-vm"
    ws.mkdir(parents=True)
    (ws / "main.tf").write_text("existing")
    backend = FakeBackend()
    backend.set_default(make_ok())
    vm = AzureVM("my-vm", ws, backend)
    new_vm = vm.clone("my-vm-clone")
    assert (tmp_path / "my-vm-clone" / "main.tf").read_text() == "existing"
    assert new_vm.name == "my-vm-clone"


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
    with (
        patch("azure_vm.vm.time.monotonic", side_effect=[0, 130]),
        pytest.raises(AzureVmTimeoutError) as exc_info,
    ):
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
def test_wait_ready_retries_on_oserror(mock_sleep):
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_WITH_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with (
        patch("azure_vm.vm.time.monotonic", side_effect=[0, 1, 5, 130]),
        patch("azure_vm.vm.socket.create_connection", side_effect=OSError),
        pytest.raises(AzureVmTimeoutError),
    ):
        vm.wait_ready(timeout=120, port=22)
    assert mock_sleep.call_count == 2


@patch("azure_vm.vm.time.sleep")
def test_wait_ready_raises_timeout_when_port_unreachable(mock_sleep):
    backend = FakeBackend()
    backend.set_default(make_ok(OUTPUT_WITH_IP))
    vm = AzureVM("my-vm", Path("/tmp/ws/my-vm"), backend)
    with (
        patch("azure_vm.vm.time.monotonic", side_effect=[0, 130]),
        patch("azure_vm.vm.socket.create_connection", side_effect=OSError),
        pytest.raises(AzureVmTimeoutError),
    ):
        vm.wait_ready(timeout=120, port=22)
