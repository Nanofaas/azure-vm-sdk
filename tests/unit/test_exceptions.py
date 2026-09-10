from azure_vm.exceptions import (
    AzureVmCommandError,
    AzureVmError,
    AzureVmTimeoutError,
    SshConnectionError,
    TofuNotInstalledError,
    VmNotFoundError,
)


def test_all_exceptions_are_subclasses_of_azure_vm_error():
    assert issubclass(AzureVmCommandError, AzureVmError)
    assert issubclass(TofuNotInstalledError, AzureVmError)
    assert issubclass(VmNotFoundError, AzureVmError)
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
