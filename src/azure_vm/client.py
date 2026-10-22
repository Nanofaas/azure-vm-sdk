"""Client for creating, listing and destroying Azure VMs through OpenTofu."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Generator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
from pathlib import Path

from ._backend import CommandBackend, TofuBackend, run_command
from ._discovery import list_images
from ._discovery import list_sizes as _list_sizes
from ._disks import apply_with_disk_tracking
from ._workspace import Workspace
from .exceptions import AzureVmCommandError, VmNotFoundError
from .models import ImageInfo, VmConfig, VmInfo, VmSize, VmState
from .vm import AzureVM


@dataclass(frozen=True)
class LaunchResult:
    """Outcome for one incremental launch; exactly one of vm/error is set."""

    config: VmConfig
    vm: AzureVM | None = None
    error: Exception | None = None


class AzureClient:
    """Manages Azure VMs via OpenTofu across a shared workspace directory."""

    def __init__(
        self,
        resource_group: str | None = None,
        location: str | None = None,
        work_dir: str | None = None,
        backend: CommandBackend | None = None,
        ssh_key_path: str | None = None,
        ssh_username: str = "azureuser",
        ssh_connect_timeout: float = 15.0,
        ssh_keepalive_interval: float = 30.0,
        ssh_client_id: str | None = "OpenSSH_9.6p1",
        ssh_connect_attempts: int = 6,
    ) -> None:
        """Configure the client and locate the workspace root it manages.

        ``resource_group`` and ``location`` are used by ``launch`` and
        ``list_sizes``; they are not read from the environment here, so callers
        that want the ``AZURE_*`` variables must pass their values in. All
        ``ssh_*`` arguments are handed to every VM the client creates.
        ``work_dir`` defaults to ``~/.azure-vm-sdk`` and ``backend`` to a real
        ``TofuBackend``.
        """
        if ssh_connect_attempts < 1:
            raise ValueError("ssh_connect_attempts must be at least 1")
        self._ssh_connect_attempts = ssh_connect_attempts
        self._resource_group = resource_group
        self._location = location
        self._backend: CommandBackend = backend or TofuBackend()
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username
        self._ssh_connect_timeout = ssh_connect_timeout
        self._ssh_keepalive_interval = ssh_keepalive_interval
        self._ssh_client_id = ssh_client_id

        root = Path(work_dir) if work_dir else Path.home() / ".azure-vm-sdk"
        self._workspace = Workspace(root, self._backend)

    # ---------------------------------------------------------------- get_vm

    def get_vm(self, name: str) -> AzureVM:
        """Return a handle to an existing VM.

        Raises ``VmNotFoundError`` when the workspace holds no ``main.tf`` for
        ``name``, so no ``tofu`` command is run against a missing workspace.
        """
        if not self._workspace.vm_exists(name):
            raise VmNotFoundError(name)
        return AzureVM(
            name,
            self._workspace.vm_dir(name),
            self._backend,
            self._ssh_key_path,
            self._ssh_username,
            ssh_connect_timeout=self._ssh_connect_timeout,
            ssh_keepalive_interval=self._ssh_keepalive_interval,
            ssh_client_id=self._ssh_client_id,
            ssh_connect_attempts=self._ssh_connect_attempts,
        )

    # --------------------------------------------------------------- launch

    def launch(
        self,
        name: str | None = None,
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        image_urn: str | None = None,
        cloud_init_config: dict | str | None = None,
        ssh_key_path: str | None = None,
        open_ports: list[int] | tuple[int, ...] | None = None,
        priority: str = "Regular",
        eviction_policy: str | None = None,
        max_bid_price: float | None = None,
    ) -> AzureVM:
        """Create one VM and return a handle to it.

        A random eight-character name is generated when ``name`` is omitted.
        The workspace templates are written from the given configuration,
        then ``tofu init`` and ``tofu apply`` run to create the VM. Raises
        ``AzureVmCommandError`` when the client has no ``resource_group`` or
        ``location``. Spot priority defaults to Delete eviction; omitted bid
        uses the provider default of -1. Invalid Spot settings raise ValueError
        before any resource is created.
        """
        VmConfig(
            priority=priority,
            eviction_policy=eviction_policy,
            max_bid_price=max_bid_price,
        )
        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                [],
                -1,
                "",
                "resource_group and location are required — set via AzureClient() "
                "or AZURE_RESOURCE_GROUP / AZURE_LOCATION env vars",
            )
        if name is None:
            name = uuid.uuid4().hex[:8]

        self._workspace.ensure_shared_infra(self._resource_group, self._location)
        self._workspace.write_vm_workspace(
            name=name,
            vm_size=vm_size,
            disk_size_gb=disk_size_gb,
            image_urn=image_urn,
            cloud_init_config=cloud_init_config,
            ssh_key_path=ssh_key_path,
            resource_group=self._resource_group,
            location=self._location,
            ssh_username=self._ssh_username,
            default_ssh_key=self._ssh_key_path,
            open_ports=open_ports,
            priority=priority,
            eviction_policy=eviction_policy,
            max_bid_price=max_bid_price,
        )

        wdir = str(self._workspace.vm_dir(name))
        run_command(self._backend, ["tofu", "init"], cwd=wdir)
        apply_with_disk_tracking(
            Path(wdir), self._backend, ["tofu", "apply", "-auto-approve"]
        )

        return self.get_vm(name)

    # ----------------------------------------------------------- launch_many

    def launch_many(
        self,
        configs: list[VmConfig],
        *,
        max_workers: int | None = None,
    ) -> list[AzureVM]:
        """Launch several VMs in parallel and return their handles.

        Each config becomes one ``launch`` call on a thread pool sized by
        ``max_workers`` (one thread per config by default). If any launch
        fails, the VMs that did come up are deleted and the first error is
        re-raised; an empty ``configs`` returns an empty list without
        touching Azure.
        """
        if not configs:
            return []

        configs = [replace(cfg) for cfg in configs]
        workers = max_workers if max_workers is not None else len(configs)
        first_error: BaseException | None = None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.launch,
                    name=cfg.name,
                    vm_size=cfg.vm_size,
                    disk_size_gb=cfg.disk_size_gb,
                    image_urn=cfg.image_urn,
                    cloud_init_config=cfg.cloud_init_config,
                    ssh_key_path=cfg.ssh_key_path,
                    open_ports=cfg.open_ports,
                    priority=cfg.priority,
                    eviction_policy=cfg.eviction_policy,
                    max_bid_price=cfg.max_bid_price,
                ): cfg
                for cfg in configs
            }

            for fut in as_completed(futures):
                if fut.cancelled():
                    continue
                exc = fut.exception()
                if exc is not None and first_error is None:
                    first_error = exc
                    for pending in futures:
                        pending.cancel()

        # Every launch has ended here: a VM that came up after the first failure
        # is rolled back too, never left running unrecorded.
        created = [
            f.result() for f in futures if not f.cancelled() and f.exception() is None
        ]
        if first_error is not None:
            with ThreadPoolExecutor(max_workers=max(len(created), 1)) as rollback:
                for rf in [rollback.submit(vm.delete) for vm in created]:
                    # Best-effort rollback: a VM that cannot be destroyed must
                    # not mask the launch failure being raised below.
                    with contextlib.suppress(Exception):
                        rf.result()
            raise first_error

        return created

    def launch_many_iter(
        self,
        configs: list[VmConfig],
        *,
        max_workers: int = 8,
    ) -> Generator[LaunchResult, None, None]:
        """Yield individual launch outcomes as they complete, without rollback.

        At most ``max_workers`` launches are submitted at once. Configurations
        are validated before submission; duplicate names are rejected. Omitted
        names are generated and included in the outcome's configuration.

        Use ``contextlib.closing`` when iteration may stop early. Closing stops
        submissions, cancels unstarted futures and waits for running launches.
        Those resources and their workspaces remain available, even when their
        outcomes were not consumed. For durable recovery, give every config an
        explicit name and persist those names before starting iteration. Failed
        applies may also leave partial resources: reconcile/destroy their named
        workspaces. Closing cannot interrupt an already running OpenTofu call.
        """
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers < 1
        ):
            raise ValueError("max_workers must be a positive integer")
        prepared = [
            replace(cfg, name=cfg.name or uuid.uuid4().hex[:8]) for cfg in configs
        ]
        if len({cfg.name for cfg in prepared}) != len(prepared):
            raise ValueError("launch_many_iter requires unique VM names")
        if not prepared:
            return

        remaining = iter(prepared)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        pending = {}
        try:
            for cfg in remaining:
                pending[executor.submit(self.launch, **vars(cfg))] = cfg
                if len(pending) == max_workers:
                    break
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    cfg = pending.pop(future)
                    try:
                        result = LaunchResult(config=cfg, vm=future.result())
                    except Exception as error:
                        result = LaunchResult(config=cfg, error=error)
                    yield result
                    following = next(remaining, None)
                    if following is not None:
                        pending[executor.submit(self.launch, **vars(following))] = (
                            following
                        )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    # ---------------------------------------------------------------- list

    def list(self) -> list[VmInfo]:
        """Return the state of every VM workspace under the client root."""
        return self._workspace.list_vms()

    # ---------------------------------------------------------------- find

    def find(self, publisher: str = "Canonical") -> list[ImageInfo]:
        """List the Marketplace images offered by ``publisher``."""
        return list_images(self._backend, publisher)

    # ----------------------------------------------------------- list_sizes

    def list_sizes(self, location: str | None = None) -> list[VmSize]:
        """List the VM sizes available in ``location``.

        Falls back to the client's configured location and raises
        ``AzureVmCommandError`` when neither is set.
        """
        loc = location or self._location
        if not loc:
            raise AzureVmCommandError(
                [],
                -1,
                "",
                "location is required — pass it or set via AzureClient() "
                "or AZURE_LOCATION env var",
            )
        return _list_sizes(self._backend, loc)

    # --------------------------------------------------------------- purge

    def purge(self) -> None:
        """Destroy every VM workspace, preserving the shared infrastructure."""
        self._workspace.purge()

    # -------------------------------------------------------- ensure_running

    def ensure_running(
        self,
        name: str,
        *,
        vm_size: str = "Standard_B1s",
        disk_size_gb: int = 30,
        image_urn: str | None = None,
        cloud_init_config: dict | str | None = None,
        ssh_key_path: str | None = None,
        open_ports: list[int] | tuple[int, ...] | None = None,
        priority: str = "Regular",
        eviction_policy: str | None = None,
        max_bid_price: float | None = None,
    ) -> AzureVM:
        """Return a handle to ``name``, creating or starting the VM as needed.

        A VM with no workspace is launched with the given configuration. An
        existing workspace is re-rendered from that configuration and, unless
        the VM already reports ``VmState.RUNNING``, started; if its outputs can
        no longer be read the VM is launched again from scratch.
        """
        if not self._workspace.vm_exists(name):
            return self.launch(
                name=name,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                image_urn=image_urn,
                cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
                open_ports=open_ports,
                priority=priority,
                eviction_policy=eviction_policy,
                max_bid_price=max_bid_price,
            )

        VmConfig(
            priority=priority,
            eviction_policy=eviction_policy,
            max_bid_price=max_bid_price,
        )
        if not self._resource_group or not self._location:
            raise AzureVmCommandError(
                [],
                -1,
                "",
                "resource_group and location are required — set via AzureClient() "
                "or AZURE_RESOURCE_GROUP / AZURE_LOCATION env vars",
            )
        # Re-render the workspace from the CURRENT configuration: a pre-existing
        # workspace must never silently pin stale parameters (e.g. an old
        # resource group baked into main.tf by a previous run).
        self._workspace.ensure_shared_infra(self._resource_group, self._location)
        self._workspace.write_vm_workspace(
            name=name,
            vm_size=vm_size,
            disk_size_gb=disk_size_gb,
            image_urn=image_urn,
            cloud_init_config=cloud_init_config,
            ssh_key_path=ssh_key_path,
            resource_group=self._resource_group,
            location=self._location,
            ssh_username=self._ssh_username,
            default_ssh_key=self._ssh_key_path,
            open_ports=open_ports,
            priority=priority,
            eviction_policy=eviction_policy,
            max_bid_price=max_bid_price,
        )

        vm = self.get_vm(name)
        try:
            info = vm.info()
        except (AzureVmCommandError, json.JSONDecodeError):
            return self.launch(
                name=name,
                vm_size=vm_size,
                disk_size_gb=disk_size_gb,
                image_urn=image_urn,
                cloud_init_config=cloud_init_config,
                ssh_key_path=ssh_key_path,
                open_ports=open_ports,
                priority=priority,
                eviction_policy=eviction_policy,
                max_bid_price=max_bid_price,
            )

        if info.state == VmState.RUNNING:
            return vm

        vm.start()
        return self.get_vm(name)
