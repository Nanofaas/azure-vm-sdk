"""Public API for creating and managing Azure VMs through OpenTofu."""

from ._backend import CommandResult
from .client import AzureClient
from .exceptions import (
    AzureVmCommandError,
    AzureVmError,
    AzureVmTimeoutError,
    SshConnectionError,
    TofuNotInstalledError,
    VmNotFoundError,
)
from .models import ImageInfo, VmArchitecture, VmConfig, VmInfo, VmSize, VmState
from .vm import AzureVM

__all__ = [
    "AzureClient",
    "AzureVM",
    "AzureVmCommandError",
    "AzureVmError",
    "AzureVmTimeoutError",
    "CommandResult",
    "ImageInfo",
    "SshConnectionError",
    "TofuNotInstalledError",
    "VmArchitecture",
    "VmConfig",
    "VmInfo",
    "VmNotFoundError",
    "VmSize",
    "VmState",
]
