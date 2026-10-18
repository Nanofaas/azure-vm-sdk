"""Retry connection failures without replaying remote commands."""

import errno
import io
import random
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

import azure_vm.vm as vm_module
from azure_vm._backend import FakeBackend
from azure_vm.client import AzureClient
from azure_vm.exceptions import SshConnectionError
from azure_vm.vm import AzureVM


def refused():
    return paramiko.ssh_exception.NoValidConnectionsError(
        {("192.0.2.1", 22): ConnectionRefusedError(errno.ECONNREFUSED, "not ready")}
    )


@pytest.fixture
def connections(monkeypatch):
    attempts = []
    pauses = []
    failures = []
    commands = []
    command_errors = []

    class Client:
        closed = False

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            attempts.append(self)
            if failures:
                raise failures.pop(0)

        def get_transport(self):
            return SimpleNamespace(set_keepalive=lambda _interval: None)

        def exec_command(self, command):
            commands.append(command)
            if command_errors:
                raise command_errors.pop(0)

            class Output(io.BytesIO):
                channel = SimpleNamespace(recv_exit_status=lambda: 0)

            return None, Output(b"ready"), io.BytesIO()

        def close(self):
            self.closed = True

    monkeypatch.setattr(vm_module.paramiko, "SSHClient", Client)
    monkeypatch.setattr(vm_module.time, "sleep", pauses.append)
    monkeypatch.setattr(random, "uniform", lambda _low, high: high)
    monkeypatch.setattr(AzureVM, "_ip", lambda _self: "192.0.2.1")
    return SimpleNamespace(
        attempts=attempts,
        pauses=pauses,
        failures=failures,
        commands=commands,
        command_errors=command_errors,
    )


def test_exec_waits_for_boot_with_exponential_backoff(connections):
    connections.failures.extend(
        [
            refused(),
            refused(),
            EOFError(),
            paramiko.SSHException("Error reading SSH protocol banner"),
        ]
    )
    vm = AzureVM("booting", Path("/unused"), FakeBackend())
    result = vm.exec(["check-ready"])
    assert result.stdout == "ready"
    assert result.returncode == 0
    assert connections.pauses == [1.0, 2.0, 4.0, 8.0]
    assert connections.commands == ["check-ready"]
    assert all(client.closed for client in connections.attempts[:-1])
    assert not connections.attempts[-1].closed


def test_connect_stops_after_six_attempts_and_caps_backoff(connections):
    connections.failures.extend(refused() for _ in range(6))
    vm = AzureVM("booting", Path("/unused"), FakeBackend())
    with pytest.raises(SshConnectionError) as failure:
        vm.exec(["check-ready"])
    assert failure.value.name == "booting"
    assert isinstance(
        failure.value.__cause__, paramiko.ssh_exception.NoValidConnectionsError
    )
    assert len(connections.attempts) == 6
    assert all(client.closed for client in connections.attempts)
    assert connections.pauses == [1.0, 2.0, 4.0, 8.0, 10.0]
    assert not connections.commands


@pytest.mark.parametrize(
    "error",
    [
        paramiko.AuthenticationException("wrong credentials"),
        paramiko.PasswordRequiredException("encrypted key"),
        paramiko.ssh_exception.IncompatiblePeer("no common algorithm"),
        FileNotFoundError(errno.ENOENT, "missing key"),
        PermissionError(errno.EACCES, "unreadable key"),
        paramiko.SSHException("not a valid RSA private key file"),
        OSError(errno.EINVAL, "invalid configuration"),
    ],
)
def test_connect_does_not_retry_permanent_failures(connections, error):
    connections.failures.append(error)
    vm = AzureVM("booting", Path("/unused"), FakeBackend())
    with pytest.raises(SshConnectionError) as failure:
        vm.exec(["check-ready"])
    assert failure.value.__cause__ is error
    assert len(connections.attempts) == 1
    assert connections.attempts[0].closed
    assert not connections.pauses
    assert not connections.commands


def test_client_passes_configurable_attempt_limit_to_vm(connections, tmp_path):
    workspace = tmp_path / "booting"
    workspace.mkdir()
    (workspace / "main.tf").touch()
    client = AzureClient(
        work_dir=str(tmp_path), backend=FakeBackend(), ssh_connect_attempts=2
    )
    connections.failures.extend([refused(), refused()])
    with pytest.raises(SshConnectionError):
        client.get_vm("booting").exec(["check-ready"])
    assert len(connections.attempts) == 2
    assert connections.pauses == [1.0]


@pytest.mark.parametrize("attempts", [0, -1])
def test_vm_rejects_an_invalid_attempt_limit(attempts):
    with pytest.raises(ValueError, match="ssh_connect_attempts"):
        AzureVM(
            "booting", Path("/unused"), FakeBackend(), ssh_connect_attempts=attempts
        )


def test_connect_applies_jitter_to_each_backoff(connections, monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda low, _high: low)
    connections.failures.extend([refused(), refused()])
    result = AzureVM("booting", Path("/unused"), FakeBackend()).exec(["check-ready"])
    assert result.stdout == "ready"
    assert connections.pauses == [0.5, 1.0]


@pytest.mark.parametrize("attempts", [0, -1])
def test_client_rejects_an_invalid_attempt_limit(attempts):
    with pytest.raises(ValueError, match="ssh_connect_attempts"):
        AzureClient(ssh_connect_attempts=attempts)


def test_exec_does_not_replay_a_command_after_connection_reset(connections):
    error = ConnectionResetError(errno.ECONNRESET, "reset during command")
    connections.command_errors.append(error)
    vm = AzureVM("booting", Path("/unused"), FakeBackend())
    with pytest.raises(ConnectionResetError) as failure:
        vm.exec(["perform-side-effect"])
    assert failure.value is error
    assert connections.commands == ["perform-side-effect"]
    assert len(connections.attempts) == 1
    assert connections.attempts[0].closed
    assert not connections.pauses


def test_exec_connects_after_a_real_tcp_refusal(monkeypatch):
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    accepted = threading.Event()
    server_errors = []
    delays = []
    host_key = paramiko.RSAKey.generate(2048)
    transports = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_channel_exec_request(self, channel, command):
            accepted.set()
            return True

    def serve():
        try:
            connection, _address = listener.accept()
            transport = paramiko.Transport(connection)
            transports.append(transport)
            transport.add_server_key(host_key)
            transport.start_server(server=Server())
            channel = transport.accept(timeout=5)
            assert channel is not None and accepted.wait(5)
            channel.sendall(b"ready-after-boot")
            channel.send_exit_status(0)
            channel.shutdown_write()
            channel.close()
        except Exception as error:
            server_errors.append(error)

    server_thread = threading.Thread(target=serve, daemon=True)

    def boot_after_first_failure(delay):
        delays.append(delay)
        listener.listen(1)
        server_thread.start()

    # Use real TCP and SSH; redirect port 22 to the unprivileged test listener.
    connect = paramiko.SSHClient.connect

    def connect_to_test_port(self, **kwargs):
        return connect(
            self,
            **kwargs,
            port=port,
            password="test-only",
            allow_agent=False,
            look_for_keys=False,
        )

    monkeypatch.setattr(paramiko.SSHClient, "connect", connect_to_test_port)
    monkeypatch.setattr(AzureVM, "_ip", lambda _self: "127.0.0.1")
    monkeypatch.setattr(
        vm_module, "time", SimpleNamespace(sleep=boot_after_first_failure)
    )
    vm = AzureVM("booting", Path("/unused"), FakeBackend(), ssh_client_id=None)
    try:
        result = vm.exec(["check-ready"])
        assert result.stdout == "ready-after-boot"
        assert result.returncode == 0
        assert len(delays) == 1 and 0.5 <= delays[0] <= 1.0
        server_thread.join(timeout=5)
        assert not server_thread.is_alive()
        assert not server_errors
    finally:
        vm.close()
        listener.close()
        for transport in transports:
            transport.close()
        if server_thread.ident is not None:
            server_thread.join(timeout=5)


def test_authentication_failure_is_fatal_even_if_it_mentions_the_banner(connections):
    error = paramiko.AuthenticationException("Error reading SSH protocol banner")
    connections.failures.append(error)
    with pytest.raises(SshConnectionError) as failure:
        AzureVM("booting", Path("/unused"), FakeBackend()).exec(["check-ready"])
    assert failure.value.__cause__ is error
    assert len(connections.attempts) == 1
    assert not connections.pauses
