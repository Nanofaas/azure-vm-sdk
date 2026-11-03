"""A stalled SSH session fails within its keepalive deadline instead of hanging."""

import threading
import time
from pathlib import Path

import pytest

import azure_vm.vm as vm_module
from azure_vm import AzureClient, SshConnectionError, SshStallError
from azure_vm._backend import FakeBackend
from azure_vm._liveness import KEEPALIVE_REQUEST, Liveness
from azure_vm.vm import AzureVM

INTERVAL = 0.02
COUNT_MAX = 2


class Transport:
    """A transport whose peer answers keepalives, or goes silent."""

    def __init__(self, *, answers: bool = True) -> None:
        self.answers = answers
        self.closed = threading.Event()
        self.probes = 0

    def is_active(self):
        return not self.closed.is_set()

    def is_authenticated(self):
        return True

    def global_request(self, kind, wait=True):
        assert kind == KEEPALIVE_REQUEST
        assert wait
        self.probes += 1
        if not self.answers:
            self.closed.wait()

    def close(self):
        self.closed.set()


class Channel:
    def __init__(self, transport, finished):
        self.transport = transport
        self.finished = finished

    def recv_exit_status(self):
        # A channel unlinked by a closed transport never got its status.
        return 0 if self.finished.is_set() else -1

    def close(self):
        pass


class Output:
    """Command output that arrives after `seconds`, or never if the peer stalls."""

    def __init__(self, transport, finished, seconds):
        self.channel = Channel(transport, finished)
        self.transport = transport
        self.finished = finished
        self.seconds = seconds

    def read(self):
        if not self.transport.closed.wait(self.seconds):
            self.finished.set()
        return b""


class Sftp:
    def __init__(self, transport, *, error=None):
        self.transport = transport
        self.error = error
        self.calls = []

    def put(self, source, dest):
        self.calls.append(("put", source, dest))
        if self.error is not None:
            raise self.error
        if not self.transport.answers:
            # A stalled upload ends only when the session is torn down.
            self.transport.closed.wait()
            raise EOFError("Server connection dropped")

    def posix_rename(self, old, new):
        self.calls.append(("rename", old, new))

    def close(self):
        pass


class Client:
    """One SSH session: its transport, the command it runs and its SFTP."""

    def __init__(self, *, answers=True, seconds=0.0, sftp_error=None):
        self.transport = Transport(answers=answers)
        self.seconds = seconds
        self.sftp = Sftp(self.transport, error=sftp_error)
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        pass

    def get_transport(self):
        return self.transport

    def exec_command(self, command):
        finished = threading.Event()
        output = Output(self.transport, finished, self.seconds)
        return None, output, Output(self.transport, threading.Event(), 0)

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True
        self.transport.close()


@pytest.fixture
def sessions(monkeypatch):
    """Hand out the queued clients, one per connection."""
    queued: list[Client] = []
    monkeypatch.setattr(vm_module.paramiko, "SSHClient", lambda: queued.pop(0))
    monkeypatch.setattr(AzureVM, "_ip", lambda _self: "192.0.2.1")
    return queued


def vm(**kwargs):
    return AzureVM(
        "my-vm",
        Path("/tmp/ws/my-vm"),
        FakeBackend(),
        ssh_keepalive_interval=INTERVAL,
        ssh_keepalive_count_max=COUNT_MAX,
        **kwargs,
    )


# ------------------------------------------------------------- watchdog


def test_a_silent_peer_closes_the_transport_within_the_deadline():
    transport = Transport(answers=False)
    liveness = Liveness(transport, interval=INTERVAL, count_max=COUNT_MAX, label="x")
    started = time.monotonic()
    liveness.start()

    assert transport.closed.wait(5)
    assert liveness.expired
    assert time.monotonic() - started < 1


def test_an_answering_peer_keeps_its_transport():
    transport = Transport()
    liveness = Liveness(transport, interval=INTERVAL, count_max=COUNT_MAX, label="x")
    liveness.start()
    time.sleep(INTERVAL * COUNT_MAX * 5)
    liveness.stop()

    assert transport.probes >= 3
    assert not liveness.expired
    assert transport.is_active()


def test_a_probe_that_cannot_be_sent_on_a_live_transport_is_a_stall():
    transport = Transport()
    transport.global_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("key exchange timed out")
    )
    liveness = Liveness(transport, interval=INTERVAL, count_max=COUNT_MAX, label="x")
    liveness.start()

    assert transport.closed.wait(5)
    assert liveness.expired


def test_a_stopped_watchdog_never_closes_its_transport():
    transport = Transport(answers=False)
    liveness = Liveness(transport, interval=INTERVAL, count_max=COUNT_MAX, label="x")
    liveness.stop()
    liveness.start()
    time.sleep(INTERVAL * COUNT_MAX * 5)

    assert not liveness.expired
    assert transport.is_active()


# ------------------------------------------------------------------ exec


def test_a_long_silent_command_is_not_mistaken_for_a_stall(sessions):
    sessions.append(Client(seconds=INTERVAL * COUNT_MAX * 10))

    result = vm().exec(["sleep", "1"])

    assert result.returncode == 0


def test_a_stalled_command_raises_and_the_next_call_reconnects(sessions):
    stalled, fresh = Client(answers=False, seconds=60), Client()
    sessions.extend([stalled, fresh])
    handle = vm()

    with pytest.raises(SshStallError) as caught:
        handle.exec(["docker", "load"])

    assert isinstance(caught.value, SshConnectionError)
    assert caught.value.host == "192.0.2.1"
    assert caught.value.seconds == INTERVAL * COUNT_MAX
    assert stalled.closed
    assert handle.exec(["true"]).returncode == 0
    handle.close()


# -------------------------------------------------------------- transfer


def test_a_stalled_upload_is_retried_on_a_new_connection(sessions, tmp_path):
    stalled, fresh = Client(answers=False), Client()
    sessions.extend([stalled, fresh])

    vm().transfer(str(tmp_path / "image.tar"), "/srv/image.tar")

    assert stalled.closed
    assert fresh.closed
    partial = fresh.sftp.calls[0][2]
    assert partial.startswith("/srv/image.tar.") and partial.endswith(".partial")
    assert fresh.sftp.calls == [
        ("put", str(tmp_path / "image.tar"), partial),
        ("rename", partial, "/srv/image.tar"),
    ]


def test_a_dropped_upload_is_retried_until_the_attempts_run_out(sessions, tmp_path):
    sessions.extend(
        Client(sftp_error=EOFError("Server connection dropped")) for _ in range(2)
    )

    with pytest.raises(EOFError):
        vm(ssh_transfer_attempts=2).transfer(str(tmp_path / "a"), "/srv/a")

    assert sessions == []


def test_a_missing_file_is_not_retried(sessions, tmp_path):
    missing = Client(sftp_error=FileNotFoundError("no such file"))
    sessions.extend([missing, Client()])

    with pytest.raises(FileNotFoundError):
        vm().transfer(str(tmp_path / "missing"), "/srv/missing")

    assert len(sessions) == 1


@pytest.mark.parametrize("option", ["ssh_keepalive_count_max", "ssh_transfer_attempts"])
def test_counts_below_one_are_refused(option):
    with pytest.raises(ValueError, match=option):
        AzureVM("my-vm", Path("/tmp/ws/my-vm"), FakeBackend(), **{option: 0})


def test_the_client_hands_its_keepalive_and_transfer_limits_to_vms(sessions, tmp_path):
    (tmp_path / "booting").mkdir()
    (tmp_path / "booting" / "main.tf").touch()
    client = AzureClient(
        work_dir=str(tmp_path),
        backend=FakeBackend(),
        ssh_keepalive_interval=INTERVAL,
        ssh_keepalive_count_max=COUNT_MAX,
        ssh_transfer_attempts=2,
    )
    sessions.extend([Client(answers=False), Client(answers=False)])

    with pytest.raises(SshStallError) as caught:
        client.get_vm("booting").transfer(str(tmp_path / "a"), "/srv/a")

    assert caught.value.seconds == INTERVAL * COUNT_MAX
    assert sessions == []


@pytest.mark.parametrize("option", ["ssh_keepalive_count_max", "ssh_transfer_attempts"])
def test_the_client_refuses_counts_below_one(option):
    with pytest.raises(ValueError, match=option):
        AzureClient(**{option: 0})
