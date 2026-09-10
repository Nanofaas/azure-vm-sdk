"""Exception hierarchy raised by azure-vm-sdk."""


class AzureVmError(Exception):
    """Base exception for all azure-vm-sdk errors."""


class AzureVmCommandError(AzureVmError):
    """Raised when an external command exits with a non-zero status."""

    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        """Record the failed command and its captured output.

        The message prefers ``stderr`` and falls back to ``stdout``. The argv
        is kept on ``args_list`` because ``BaseException.args`` already holds
        the message tuple.
        """
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command {args} failed with exit code {returncode}: {stderr or stdout}"
        )


class TofuNotInstalledError(AzureVmError):
    """Raised when the OpenTofu CLI is missing from ``PATH``."""

    def __init__(self) -> None:
        """Point the caller at the OpenTofu installation page."""
        super().__init__("OpenTofu not found. Install from https://opentofu.org")


class VmNotFoundError(AzureVmError):
    """Raised when no VM workspace exists for the requested name."""

    def __init__(self, name: str) -> None:
        """Expose the missing VM's ``name`` alongside the message."""
        self.name = name
        super().__init__(f"VM '{name}' not found")


class AzureVmTimeoutError(AzureVmError):
    """Raised when a VM is not ready within the allotted timeout."""

    def __init__(self, name: str, timeout: float) -> None:
        """Expose the VM ``name`` and the ``timeout`` in seconds it exceeded."""
        self.name = name
        self.timeout = timeout
        super().__init__(f"VM '{name}' did not become ready within {timeout}s")


class SshConnectionError(AzureVmError):
    """Raised when an SSH session to a VM cannot be established."""

    def __init__(self, name: str, host: str, reason: str) -> None:
        """Expose the VM ``name``, the ``host`` tried and the failure ``reason``."""
        self.name = name
        self.host = host
        self.reason = reason
        super().__init__(f"SSH connection to '{name}' ({host}) failed: {reason}")
