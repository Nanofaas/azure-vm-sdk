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
