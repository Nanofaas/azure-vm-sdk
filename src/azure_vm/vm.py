"""Handle to a single Azure VM and the OpenTofu workspace backing it."""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import time
from pathlib import Path

import paramiko

from ._backend import CommandBackend, CommandResult, run_command
from .exceptions import (
    AzureVmTimeoutError,
    SshConnectionError,
)
from .models import VmInfo


class AzureVM:
    """One Azure VM, driven through the OpenTofu workspace it was created from.

    Lifecycle calls shell out to ``tofu`` in ``workspace_dir``; the SSH methods
    open (and reuse) a paramiko connection to the VM's public IP.
    """

    def __init__(
        self,
        name: str,
        workspace_dir: Path,
        backend: CommandBackend,
        ssh_key_path: str | None = None,
        ssh_username: str = "azureuser",
        ssh_connect_timeout: float = 15.0,
        ssh_keepalive_interval: float = 30.0,
        ssh_client_id: str | None = "OpenSSH_9.6p1",
    ) -> None:
        """Bind the handle to a VM name, workspace directory and command backend.

        ``ssh_client_id`` is the client banner paramiko announces during the
        handshake; ``None`` leaves paramiko's own default in place. A
        ``ssh_keepalive_interval`` of zero disables keepalive probes.
        """
        self.name = name
        self._workspace_dir = workspace_dir
        self._backend = backend
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username
        self._ssh_connect_timeout = ssh_connect_timeout
        self._ssh_keepalive_interval = ssh_keepalive_interval
        self._ssh_client_id = ssh_client_id
        self._ssh: paramiko.SSHClient | None = None

    def _run(self, args: list[str]) -> CommandResult:
        return run_command(self._backend, args, cwd=str(self._workspace_dir))

    def _ip(self) -> str:
        result = self._run(["tofu", "output", "-json"])
        data = json.loads(result.stdout)
        ip = data.get("vm_ip", {}).get("value", "")
        if ip:
            return ip
        return ""

    # ------------------------------------------------------------ lifecycle

    def info(self) -> VmInfo:
        """Read the workspace outputs and return the VM's current details."""
        result = self._run(["tofu", "output", "-json"])
        return VmInfo.from_tofu_output(json.loads(result.stdout), self.name)

    def start(self) -> None:
        """Apply the workspace with ``desired_state=running``."""
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=running"])

    def stop(self) -> None:
        """Apply the workspace with ``desired_state=stopped``."""
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=stopped"])

    def restart(self) -> None:
        """Apply the workspace with ``desired_state=restart``.

        ``restart`` is not a ``VmState`` member, so ``info()`` reports
        ``VmState.UNKNOWN`` until a running or stopped state is applied again.
        """
        self._run(["tofu", "apply", "-auto-approve", "-var", "desired_state=restart"])

    def delete(self) -> None:
        """Destroy the workspace, removing the VM and its Azure resources."""
        self._run(["tofu", "destroy", "-auto-approve"])

    # ---------------------------------------------------------------- SSH

    def close(self) -> None:
        """Close the cached SSH connection, if one is open."""
        if self._ssh is not None:
            self._ssh.close()
            self._ssh = None

    def _ssh_client(self) -> paramiko.SSHClient:
        if self._ssh is not None:
            transport = self._ssh.get_transport()
            if (
                transport is not None
                and transport.is_active()
                and transport.is_authenticated()
            ):
                return self._ssh
            self.close()
        self._ssh = self._connect_ssh()
        return self._ssh

    def _connect_ssh(self) -> paramiko.SSHClient:
        ip = self._ip()
        if not ip:
            raise AzureVmTimeoutError(self.name, 0)
        # Announce an OpenSSH-style client identifier. Corporate SSH-inspecting
        # firewalls/IPS commonly allow-list known clients and RST the banner of
        # non-OpenSSH ones (paramiko's default "SSH-2.0-paramiko_x.y" gets reset
        # while the system `ssh` connects fine). _CLIENT_ID is the class field
        # paramiko interpolates into local_version; overriding it before the
        # handshake makes paramiko present as "SSH-2.0-OpenSSH_...".
        if self._ssh_client_id is not None:
            # paramiko exposes no public API for the client id; this class
            # field is the documented override point.
            paramiko.Transport._CLIENT_ID = self._ssh_client_id  # type: ignore[attr-defined]  # noqa: SLF001
        ssh = paramiko.SSHClient()
        # The VM was just created, so its host key cannot be in known_hosts and
        # there is no trusted channel to verify it over. AutoAddPolicy therefore
        # accepts whatever key the peer presents on this first connection, which
        # also accepts a machine-in-the-middle between the SDK and the VM. The
        # alternative — pinning the key out of band — is left to the caller;
        # silently failing every connection to a new VM would be worse.
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
        try:
            ssh.connect(
                hostname=ip,
                username=self._ssh_username,
                key_filename=self._ssh_key_path,
                timeout=self._ssh_connect_timeout,
            )
        except (OSError, paramiko.SSHException) as e:
            ssh.close()
            raise SshConnectionError(self.name, ip, str(e)) from e
        # Enable keepalive so a silently-dropped connection surfaces as an
        # error within a few missed probes instead of blocking forever in
        # recv_exit_status(). Long-lived commands (load tests, image builds)
        # over cloud NAT otherwise hang indefinitely on a dead peer.
        if self._ssh_keepalive_interval > 0:
            transport = ssh.get_transport()
            if transport is not None:
                transport.set_keepalive(self._ssh_keepalive_interval)
        return ssh

    def exec(self, command: list[str]) -> CommandResult:
        """Run ``command`` over SSH and return its captured output and exit code.

        The argv is shell-quoted and joined into a single string before being
        sent. A connection dropped mid-command clears the cached session and
        re-raises the original error.
        """
        ssh = self._ssh_client()
        try:
            _, stdout, stderr = ssh.exec_command(shlex.join(command))
            exit_status = stdout.channel.recv_exit_status()
            return CommandResult(
                args=command,
                returncode=exit_status,
                stdout=stdout.read().decode("utf-8", errors="replace"),
                stderr=stderr.read().decode("utf-8", errors="replace"),
            )
        except (EOFError, OSError, paramiko.SSHException):
            self.close()
            raise

    def exec_structured(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run ``argv`` under ``bash -lc``, optionally with ``env`` and ``cwd``.

        ``cwd`` becomes a leading ``cd`` and each ``env`` entry an ``export``;
        the parts are chained with ``&&`` so a failure in any of them
        short-circuits the command.
        """
        parts: list[str] = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={shlex.quote(v)}")
        parts.append(shlex.join(argv))
        command = " && ".join(parts)
        return self.exec(["bash", "-lc", command])

    def transfer(self, source: str, dest: str) -> None:
        """Copy a file to or from the VM over SFTP.

        A ``source`` containing ``:`` is read as the remote path and downloaded
        to the local ``dest``; otherwise the local ``source`` is uploaded to
        the remote ``dest``. The transfer uses its own connection, which is
        closed when the copy finishes.
        """
        ssh = self._connect_ssh()
        try:
            sftp = ssh.open_sftp()
            try:
                if ":" in source:
                    sftp.get(source, dest)
                else:
                    sftp.put(source, dest)
            finally:
                sftp.close()
        finally:
            ssh.close()

    # --------------------------------------------------------------- clone

    def clone(self, new_name: str) -> AzureVM:
        """Copy this VM's workspace to ``new_name`` and apply it.

        The sibling directory ``<parent>/<new_name>`` receives a copy of the
        current workspace, then ``tofu apply`` creates a VM named ``new_name``
        within it. The returned handle reuses this VM's backend, SSH key and
        username, but starts with its own connection and default SSH timing.
        """
        new_ws = self._workspace_dir.parent / new_name
        if self._workspace_dir.exists():
            shutil.copytree(self._workspace_dir, new_ws, dirs_exist_ok=True)
        else:
            new_ws.mkdir(parents=True, exist_ok=True)
        self._backend.run(
            ["tofu", "apply", "-auto-approve", "-var", f"vm_name={new_name}"],
            cwd=str(new_ws),
        )
        return AzureVM(
            new_name,
            new_ws,
            self._backend,
            self._ssh_key_path,
            self._ssh_username,
        )

    # --------------------------------------------------------- wait_for_ip

    def wait_for_ip(self, timeout: float = 120, *, interval: float = 2.0) -> str:
        """Poll the workspace outputs until the VM has a public IP.

        Returns the IP as soon as it appears, polling every ``interval``
        seconds; raises ``AzureVmTimeoutError`` once ``timeout`` seconds have
        elapsed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                return ip
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)

    # ---------------------------------------------------------- wait_ready

    def wait_ready(
        self, timeout: float = 120, port: int = 22, *, interval: float = 2.0
    ) -> str:
        """Wait until the VM's public IP accepts TCP connections on ``port``.

        Polls every ``interval`` seconds and returns the IP once a connection
        attempt succeeds; raises ``AzureVmTimeoutError`` if ``timeout`` seconds
        pass with no successful connect.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ip = self._ip()
            if ip:
                try:
                    with socket.create_connection((ip, port), timeout=1):
                        return ip
                except OSError:
                    pass
            time.sleep(interval)
        raise AzureVmTimeoutError(self.name, timeout)
