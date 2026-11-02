"""Public API for creating and managing Azure VMs through OpenTofu."""

from ._backend import CommandResult
from .client import AzureClient, LaunchResult
from .exceptions import (
    AzureVmCommandError,
    AzureVmError,
    AzureVmTimeoutError,
    SshConnectionError,
    TofuNotInstalledError,
    VmNotFoundError,
)
from .models import (
    MIN_OS_DISK_GB,
    ImageInfo,
    IngressRule,
    VmArchitecture,
    VmConfig,
    VmInfo,
    VmSize,
    VmState,
)
from .vm import AzureVM

__all__ = [
    "MIN_OS_DISK_GB",
    "AzureClient",
    "AzureVM",
    "AzureVmCommandError",
    "AzureVmError",
    "AzureVmTimeoutError",
    "CommandResult",
    "ImageInfo",
    "IngressRule",
    "LaunchResult",
    "SshConnectionError",
    "TofuNotInstalledError",
    "VmArchitecture",
    "VmConfig",
    "VmInfo",
    "VmNotFoundError",
    "VmSize",
    "VmState",
]
