from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

import azure_vm
from azure_vm import AzureClient, VmConfig
from azure_vm._backend import CommandResult, FakeBackend


class CreationBackend(FakeBackend):
    def __init__(self, *, blocked=(), failed=()):
        super().__init__()
        self.blocked = set(blocked)
        self.failed = set(failed)
        self.release = Event()
        self.started = Event()
        self.lock = Lock()
        self.active = 0
        self.peak = 0
        self.created = []
        self.deleted = []

    def run(self, args, *, cwd=None, timeout=None):
        name = Path(cwd).name if cwd else ""
        if "destroy" in args:
            self.deleted.append(name)
        if "apply" not in args or name == ".shared":
            return CommandResult(args, 0, "", "")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if name in self.blocked:
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError("test did not release blocked creation")
            if name in self.failed:
                return CommandResult(args, 1, "", "capacity unavailable")
            self.created.append(name)
            return CommandResult(args, 0, "", "")
        finally:
            with self.lock:
                self.active -= 1


def client_at(tmp_path, backend=None):
    return AzureClient(
        resource_group="test-rg",
        location="westeurope",
        work_dir=str(tmp_path),
        backend=backend or CreationBackend(),
    )


@pytest.mark.parametrize(("policy", "bid"), [("Delete", -1), ("Deallocate", 0.125)])
def test_spot_rendered_and_retained_when_cloned(tmp_path, policy, bid):
    client = client_at(tmp_path)
    vm = client.launch(
        "spot", priority="Spot", eviction_policy=policy, max_bid_price=bid
    )
    text = (tmp_path / "spot" / "main.tf").read_text()
    assert 'priority = "Spot"' in text
    assert f'eviction_policy = "{policy}"' in text
    assert f"max_bid_price = {bid}" in text
    vm.clone("copy")
    assert (tmp_path / "copy" / "main.tf").read_text() == text


def test_spot_defaults_and_regular_omits_spot_fields(tmp_path):
    client = client_at(tmp_path)
    client.launch("regular")
    regular = (tmp_path / "regular" / "main.tf").read_text()
    assert "eviction_policy" not in regular
    assert "max_bid_price" not in regular
    client.launch("spot", priority="Spot")
    spot = (tmp_path / "spot" / "main.tf").read_text()
    assert 'eviction_policy = "Delete"' in spot


@pytest.mark.parametrize(
    "options",
    [
        {"priority": "spot"},
        {"eviction_policy": "Delete"},
        {"max_bid_price": -1},
        {"priority": "Spot", "eviction_policy": "Restart"},
        *[
            {"priority": "Spot", "max_bid_price": value}
            for value in [-2, -0.1, float("nan"), float("inf"), True, "cheap"]
        ],
    ],
)
def test_invalid_spot_rejected_before_any_resources(tmp_path, options):
    client = client_at(tmp_path)
    with pytest.raises(ValueError, match=r"priority|eviction_policy|max_bid_price"):
        VmConfig(**options)
    with pytest.raises(ValueError, match=r"priority|eviction_policy|max_bid_price"):
        client.launch("invalid", **options)
    with pytest.raises(ValueError, match=r"priority|eviction_policy|max_bid_price"):
        client.ensure_running("invalid", **options)
    assert list(tmp_path.iterdir()) == []


def test_ensure_running_and_transactional_launch_preserve_spot(tmp_path):
    client = client_at(tmp_path)
    client.ensure_running("ensured", priority="Spot", max_bid_price=0.5)
    client.launch_many([VmConfig(name="many", priority="Spot", max_bid_price=0.5)])
    for name in ["ensured", "many"]:
        assert "max_bid_price = 0.5" in (tmp_path / name / "main.tf").read_text()


def test_iterator_delivers_before_slow_peer_and_preserves_partial_success(tmp_path):
    backend = CreationBackend(blocked=["slow"], failed=["bad"])
    client = client_at(tmp_path, backend)
    configs = [VmConfig(name=name) for name in ["slow", "fast", "bad"]]
    results = client.launch_many_iter(configs, max_workers=2)
    try:
        with ThreadPoolExecutor(max_workers=1) as reader:
            first = reader.submit(next, results).result(timeout=2)
        assert backend.started.wait(1)
        assert isinstance(first, azure_vm.LaunchResult)
        assert first.config.name == "fast"
        assert first.vm.name == "fast"
        assert first.error is None
        with ThreadPoolExecutor(max_workers=1) as reader:
            second = reader.submit(next, results).result(timeout=2)
        assert second.config.name == "bad"
        assert second.vm is None
        assert "capacity unavailable" in str(second.error)
    finally:
        backend.release.set()
    remaining = list(results)
    assert [result.config.name for result in remaining] == ["slow"]
    assert backend.deleted == []
    assert set(backend.created) == {"slow", "fast"}
    assert backend.peak <= 2


def test_iterator_close_drains_started_work_without_admitting_more(tmp_path):
    backend = CreationBackend(blocked=["slow"])
    client = client_at(tmp_path, backend)
    results = client.launch_many_iter(
        [VmConfig(name=n) for n in ["slow", "fast", "never"]],
        max_workers=2,
    )
    try:
        assert next(results).config.name == "fast"
        assert backend.started.wait(1)
        with ThreadPoolExecutor(max_workers=1) as closer:
            closed = closer.submit(results.close)
            assert not closed.done()
            backend.release.set()
            closed.result(timeout=2)
    finally:
        backend.release.set()
        results.close()
    assert set(backend.created) == {"slow", "fast"}
    assert backend.deleted == []
    assert not (tmp_path / "never").exists()
    assert client.get_vm("slow").name == "slow"


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_iterator_rejects_invalid_concurrency_before_launch(tmp_path, workers):
    with pytest.raises(ValueError, match="max_workers"):
        list(
            client_at(tmp_path).launch_many_iter(
                [VmConfig(name="vm")], max_workers=workers
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_iterator_empty_and_duplicate_names(tmp_path):
    client = client_at(tmp_path)
    assert list(client.launch_many_iter([])) == []
    with pytest.raises(ValueError, match="unique"):
        list(client.launch_many_iter([VmConfig(name="same"), VmConfig(name="same")]))
    assert list(tmp_path.iterdir()) == []


def test_iterator_runs_large_batch_once_with_bounded_workers(tmp_path):
    backend = CreationBackend()
    configs = [VmConfig(name=f"vm-{n}", priority="Spot") for n in range(100)]
    outcomes = list(
        client_at(tmp_path, backend).launch_many_iter(configs, max_workers=3)
    )
    assert len(outcomes) == 100
    assert {outcome.config.name for outcome in outcomes} == {
        f"vm-{n}" for n in range(100)
    }
    assert all(outcome.vm is not None and outcome.error is None for outcome in outcomes)
    assert len(backend.created) == 100
    assert backend.peak <= 3


def test_iterator_returns_generated_name_for_failure_recovery(tmp_path):
    backend = CreationBackend()
    (result,) = client_at(tmp_path, backend).launch_many_iter([VmConfig()])
    assert result.config.name == result.vm.name
    assert (
        client_at(tmp_path, backend).get_vm(result.config.name).name
        == result.config.name
    )


def test_batch_validates_mutated_configs_before_creating_any_resources(tmp_path):
    client = client_at(tmp_path)
    invalid = VmConfig(name="invalid")
    invalid.priority = "invalid"
    configs = [VmConfig(name="valid"), invalid]
    with pytest.raises(ValueError, match="priority"):
        list(client.launch_many_iter(configs))
    with pytest.raises(ValueError, match="priority"):
        client.launch_many(configs)
    assert list(tmp_path.iterdir()) == []
