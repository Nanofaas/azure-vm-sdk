"""An OS disk smaller than the Marketplace images' is refused before any resource."""

import pytest

import azure_vm
from azure_vm import MIN_OS_DISK_GB, AzureClient, VmConfig
from azure_vm._backend import FakeBackend


def client_at(tmp_path, backend):
    return AzureClient(
        resource_group="test-rg",
        location="westeurope",
        work_dir=str(tmp_path),
        backend=backend,
    )


def test_the_floor_is_the_marketplace_linux_image_disk_and_public():
    assert MIN_OS_DISK_GB == 30
    assert "MIN_OS_DISK_GB" in azure_vm.__all__
    assert VmConfig().disk_size_gb == MIN_OS_DISK_GB


@pytest.mark.parametrize("size", [MIN_OS_DISK_GB, 64, 1024])
def test_a_disk_at_least_the_image_size_is_accepted(size):
    assert VmConfig(disk_size_gb=size).disk_size_gb == size


@pytest.mark.parametrize(
    ("size", "message"),
    [
        (10, "disk_size_gb 10 is smaller than the 30 GB"),
        (29, "disk_size_gb 29 is smaller than the 30 GB"),
        (0, "disk_size_gb 0 is smaller"),
        (True, "whole gigabytes"),
        (30.0, "whole gigabytes"),
        ("30G", "whole gigabytes"),
    ],
)
def test_a_disk_below_the_image_or_not_whole_is_refused(size, message):
    with pytest.raises(ValueError, match=message):
        VmConfig(disk_size_gb=size)


def test_launch_and_ensure_running_refuse_before_any_command(tmp_path):
    backend = FakeBackend()
    client = client_at(tmp_path, backend)
    with pytest.raises(ValueError, match="smaller than the 30 GB"):
        client.launch("small", disk_size_gb=10)
    with pytest.raises(ValueError, match="smaller than the 30 GB"):
        client.ensure_running("small", disk_size_gb=10)
    assert backend.calls == []
    assert list(tmp_path.iterdir()) == []
