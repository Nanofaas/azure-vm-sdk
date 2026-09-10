from unittest.mock import patch

import pytest

from azure_vm._backend import CommandResult, FakeBackend, TofuBackend
from azure_vm.exceptions import TofuNotInstalledError


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


def test_tofu_backend_raises_not_installed_on_file_not_found():
    backend = TofuBackend()
    with (
        patch("subprocess.run", side_effect=FileNotFoundError("tofu not found")),
        pytest.raises(TofuNotInstalledError) as exc_info,
    ):
        backend.run(["tofu", "version"])
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)


def test_fake_backend_last_call_empty():
    backend = FakeBackend()
    assert backend.last_call() == []


def test_fake_backend_last_cwd():
    backend = FakeBackend()
    backend.set_default(CommandResult(args=[], returncode=0, stdout="", stderr=""))
    backend.run(["tofu", "init"], cwd="/tmp/a")
    backend.run(["tofu", "apply"], cwd="/tmp/b")
    assert backend.last_cwd() == "/tmp/b"


def test_fake_backend_last_cwd_empty():
    backend = FakeBackend()
    assert backend.last_cwd() is None


def test_fake_backend_envs_recorded():
    backend = FakeBackend()
    backend.set_default(CommandResult(args=[], returncode=0, stdout="", stderr=""))
    backend.run(["tofu", "init"], env={"HOME": "/root"})
    assert backend.envs == [{"HOME": "/root"}]


def test_fake_backend_envs_empty():
    backend = FakeBackend()
    assert backend.envs == []


def test_fake_backend_last_env():
    backend = FakeBackend()
    backend.set_default(CommandResult(args=[], returncode=0, stdout="", stderr=""))
    backend.run(["tofu", "init"], env={"A": "1"})
    backend.run(["tofu", "apply"], env={"B": "2"})
    assert backend.last_env() == {"B": "2"}


def test_fake_backend_last_env_empty():
    backend = FakeBackend()
    assert backend.last_env() is None


def test_tofu_backend_returns_command_result_on_success():
    backend = TofuBackend()
    fake_proc = type(
        "CompletedProcess",
        (),
        {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        },
    )
    with patch("subprocess.run", return_value=fake_proc):
        result = backend.run(["tofu", "version"])
    assert result.returncode == 0
    assert result.stdout == "ok"
