"""Regressions for dropped writes and independent transfer staging files."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import paramiko
import pytest

from azure_vm import AzureVM
from azure_vm._backend import FakeBackend


def handle():
    return AzureVM("vm", Path("/tmp"), FakeBackend(), ssh_transfer_attempts=3)


def test_closed_paramiko_channel_retries_upload_without_losing_original_error():
    channel = paramiko.Channel(1)
    channel.closed = True
    dead, fresh = Mock(), Mock()
    dead.open_sftp.return_value.put.side_effect = lambda *_args: channel.send(b"data")
    vm = handle()
    vm._connect_ssh = Mock(side_effect=[(dead, None, "host"), (fresh, None, "host")])
    vm.transfer("source", "dest")
    assert vm._connect_ssh.call_count == 2
    dead.close.assert_called_once()
    fresh.open_sftp.return_value.posix_rename.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        PermissionError("denied"),
        OSError(28, "No space left on device"),
    ],
)
def test_filesystem_failures_do_not_retry(error):
    ssh = Mock()
    ssh.open_sftp.return_value.put.side_effect = error
    vm = handle()
    vm._connect_ssh = Mock(return_value=(ssh, None, "host"))
    with pytest.raises(type(error)):
        vm.transfer("source", "dest")
    assert vm._connect_ssh.call_count == 1


@pytest.mark.parametrize("download", [False, True])
def test_concurrent_transfers_use_independent_staging_files(tmp_path, download):
    a_written, b_written, a_finished = (threading.Event() for _ in range(3))
    paths = []
    dest = str(tmp_path / "artifact")

    class Sftp:
        def copy(self, source, path):
            paths.append(path)
            if source.endswith("A"):
                Path(path).write_text("content-A")
                a_written.set()
                assert b_written.wait(3)
            else:
                assert a_written.wait(3)
                Path(path).write_text("content-B")
                b_written.set()
                assert a_finished.wait(3)

        put = get = copy

        def posix_rename(self, source, destination):
            Path(source).replace(destination)

        def close(self):
            pass

    vm = handle()

    def connect():
        ssh = Mock()
        ssh.open_sftp.return_value = Sftp()
        return ssh, None, "host"

    vm._connect_ssh = connect

    def transfer(label):
        try:
            vm.transfer(("remote:" if download else "") + label, dest)
            return Path(dest).read_text()
        finally:
            if label == "A":
                a_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(transfer, "A")
        b = pool.submit(transfer, "B")
        assert a.result(timeout=5) == "content-A"
        assert b.result(timeout=5) == "content-B"
    assert len(set(paths)) == 2
    assert all(Path(path).parent == tmp_path for path in paths)
    assert not any(Path(path).exists() for path in paths)


def test_closed_channel_retries_are_bounded_and_preserve_cause():
    channel = paramiko.Channel(1)
    channel.closed = True
    ssh = Mock()
    ssh.open_sftp.return_value.put.side_effect = lambda *_args: channel.send(b"data")
    vm = handle()
    vm._connect_ssh = Mock(return_value=(ssh, None, "host"))
    with pytest.raises(ConnectionError, match="Socket is closed") as caught:
        vm.transfer("source", "dest")
    assert vm._connect_ssh.call_count == 3
    assert type(caught.value.__cause__) is OSError


def test_failed_download_cleans_only_its_unique_files(tmp_path):
    destination = tmp_path / "artifact"
    destination.write_text("previous complete artifact")
    unrelated = tmp_path / "artifact.partial"
    unrelated.write_text("unrelated data")
    staging = []

    def interrupted_get(_source, partial):
        staging.append(partial)
        Path(partial).write_text("incomplete")
        raise EOFError("disconnected")

    ssh = Mock()
    ssh.open_sftp.return_value.get.side_effect = interrupted_get
    vm = handle()
    vm._connect_ssh = Mock(return_value=(ssh, None, "host"))
    with pytest.raises(EOFError):
        vm.transfer("remote:file", str(destination))
    assert len(set(staging)) == 3
    assert all(not Path(p).exists() for p in staging)
    assert destination.read_text() == "previous complete artifact"
    assert unrelated.read_text() == "unrelated data"
