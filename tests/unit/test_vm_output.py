"""Exercise command output against a real SSH channel with a bounded window."""

import socket
import threading
from pathlib import Path

import paramiko
import pytest

from azure_vm._backend import FakeBackend
from azure_vm.vm import AzureVM


@pytest.mark.parametrize("first_stream", ["stdout", "stderr"])
def test_exec_drains_both_streams_beyond_the_ssh_window(monkeypatch, first_stream):
    accepted = threading.Event()
    output = b"out\xff" * 786432
    errors = b"err\xfe" * 786432
    server_errors = []
    results = []
    command_errors = []

    class Server(paramiko.ServerInterface):
        def check_auth_none(self, username):
            return paramiko.AUTH_SUCCESSFUL

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_channel_exec_request(self, channel, command):
            accepted.set()
            return True

    client_socket, server_socket = socket.socketpair()
    server = paramiko.Transport(server_socket)
    server.add_server_key(paramiko.RSAKey.generate(2048))
    server.start_server(event=threading.Event(), server=Server())
    transport = paramiko.Transport(client_socket)
    transport.start_client(timeout=5)
    transport.auth_none("test")
    ssh = paramiko.SSHClient()
    ssh._transport = transport
    vm = AzureVM("test", Path("/unused"), FakeBackend())
    monkeypatch.setattr(vm, "_ssh_client", lambda: ssh)

    def produce():
        try:
            channel = server.accept(timeout=5)
            assert channel is not None
            assert accepted.wait(5)
            streams = [(channel.sendall, output), (channel.sendall_stderr, errors)]
            if first_stream == "stderr":
                streams.reverse()
            for send, data in streams:
                send(data)
            channel.send_exit_status(7)
            channel.shutdown_write()
            channel.close()
        except Exception as error:
            server_errors.append(error)

    def execute():
        try:
            results.append(vm.exec(["produce-output"]))
        except Exception as error:
            command_errors.append(error)

    producer = threading.Thread(target=produce, daemon=True)
    command = threading.Thread(target=execute, daemon=True)
    producer.start()
    command.start()
    try:
        command.join(timeout=10)
        assert not command.is_alive(), (
            "exec blocked after filling the SSH output window"
        )
        assert not command_errors
        assert results[0].returncode == 7
        assert results[0].stdout == output.decode("utf-8", errors="replace")
        assert results[0].stderr == errors.decode("utf-8", errors="replace")
        producer.join(timeout=5)
        assert not producer.is_alive()
        assert not server_errors
    finally:
        ssh.close()
        server.close()
        command.join(timeout=5)
        producer.join(timeout=5)


def test_exec_closes_channel_if_stderr_fails_while_stdout_waits(monkeypatch):
    from types import SimpleNamespace

    closed = threading.Event()

    class Output:
        channel = SimpleNamespace(close=closed.set)

        def read(self):
            assert closed.wait(2), "stdout remained blocked after stderr failed"
            return b""

    class Errors:
        def read(self):
            raise OSError("stderr connection failure")

    ssh = SimpleNamespace(exec_command=lambda _command: (None, Output(), Errors()))
    vm = AzureVM("test", Path("/unused"), FakeBackend())
    monkeypatch.setattr(vm, "_ssh_client", lambda: ssh)
    with pytest.raises(OSError, match="stderr connection failure"):
        vm.exec(["produce-output"])
    assert closed.is_set()
