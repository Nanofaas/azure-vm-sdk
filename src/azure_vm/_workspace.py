from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

from ._backend import CommandBackend, run_command
from ._templates import (
    SHARED_TEMPLATE,
    VM_TEMPLATE,
    parse_image_urn,
    render_security_rules,
    resolve_ssh_path,
    write_cloud_init,
)
from .models import VmInfo


class Workspace:
    """Encapsulates filesystem operations on the Azure VM workspace directory.

    Each VM gets a sub-directory under *root*. Shared infrastructure lives in
    ``.shared/``.
    """

    def __init__(self, root: Path, backend: CommandBackend) -> None:
        self._root = root
        self._backend = backend
        # launch_many's threads share one .shared state: one apply at a time,
        # and none may start its VM before the shared infra exists.
        self._shared_lock = threading.Lock()

    # ------------------------------------------------------------------- paths

    def vm_dir(self, name: str) -> Path:
        return self._root / name

    def shared_dir(self) -> Path:
        return self._root / ".shared"

    def vm_exists(self, name: str) -> bool:
        return (self.vm_dir(name) / "main.tf").exists()

    # ----------------------------------------------------------- shared infra

    def ensure_shared_infra(self, resource_group: str, location: str) -> None:
        shared = self.shared_dir()
        hcl = SHARED_TEMPLATE.format(
            resource_group=resource_group,
            location=location,
        )
        main_tf = shared / "main.tf"
        with self._shared_lock:
            # Re-render from the current configuration: an existing workspace must
            # never silently pin stale parameters. Re-apply only on actual change.
            if main_tf.exists() and main_tf.read_text() == hcl:
                return
            shared.mkdir(parents=True, exist_ok=True)
            main_tf.write_text(hcl)
            try:
                run_command(self._backend, ["tofu", "init"], cwd=str(shared))
                _try_import_resource_group(self._backend, shared, resource_group)
                run_command(
                    self._backend, ["tofu", "apply", "-auto-approve"], cwd=str(shared)
                )
            except BaseException:
                # main.tf marks an applied config: without it the next launch retries.
                main_tf.unlink(missing_ok=True)
                raise

    # --------------------------------------------------------------- vm ops

    def write_vm_workspace(
        self,
        name: str,
        vm_size: str,
        disk_size_gb: int,
        image_urn: str | None,
        cloud_init_config: dict | str | None,
        ssh_key_path: str | None,
        *,
        resource_group: str,
        location: str,
        ssh_username: str,
        default_ssh_key: str | None = None,
        open_ports=None,
        priority: str = "Regular",
        eviction_policy: str | None = None,
        max_bid_price: float | None = None,
    ) -> None:
        workspace = self.vm_dir(name)
        workspace.mkdir(parents=True, exist_ok=True)

        cloud_init_block = write_cloud_init(workspace, cloud_init_config)
        publisher, offer, sku, version = parse_image_urn(image_urn)
        effective_ssh_path = resolve_ssh_path(ssh_key_path, default_ssh_key)

        full_urn = image_urn or "Canonical:ubuntu-24_04-lts:server-gen1:latest"

        spot_block = ""
        if priority == "Spot":
            spot_block = (
                '  priority = "Spot"\n'
                f'  eviction_policy = "{eviction_policy or "Delete"}"\n'
            )
            if max_bid_price is not None:
                spot_block += f"  max_bid_price = {max_bid_price}\n"

        hcl = VM_TEMPLATE.format(
            resource_group=resource_group,
            location=location,
            ssh_username=ssh_username,
            ssh_public_key_path=effective_ssh_path,
            image_publisher=publisher,
            image_offer=offer,
            image_sku=sku,
            image_version=version,
            image_urn=full_urn,
            custom_data_block=cloud_init_block,
            extra_security_rules=render_security_rules(open_ports),
            spot_block=spot_block,
        )
        (workspace / "main.tf").write_text(hcl)

        tfvars = (
            f'vm_name = "{name}"\n'
            f'vm_size = "{vm_size}"\n'
            f"disk_size_gb = {disk_size_gb}\n"
        )
        (workspace / "terraform.tfvars").write_text(tfvars)

    def iter_vm_workspaces(self) -> Iterator[Path]:
        """Yield VM workspace directories in sorted order.

        Hidden entries, non-directories, and directories that hold no
        ``main.tf`` are skipped.
        """
        for item in sorted(self._root.iterdir()):
            if item.name.startswith("."):
                continue
            if not item.is_dir():
                continue
            if not (item / "main.tf").exists():
                continue
            yield item

    def list_vms(self) -> list[VmInfo]:
        vms: list[VmInfo] = []
        for vm_dir_item in self.iter_vm_workspaces():
            result = self._backend.run(
                ["tofu", "output", "-json"], cwd=str(vm_dir_item)
            )
            if result.success and result.stdout:
                try:
                    data = json.loads(result.stdout)
                    vms.append(VmInfo.from_tofu_output(data, vm_dir_item.name))
                except json.JSONDecodeError:
                    continue
        return vms

    def purge(self) -> None:
        """Destroy all VM workspaces (preserving .shared/)."""
        for vm_dir_item in self.iter_vm_workspaces():
            run_command(
                self._backend,
                ["tofu", "destroy", "-auto-approve"],
                cwd=str(vm_dir_item),
            )


def _try_import_resource_group(
    backend: CommandBackend,
    shared: Path,
    resource_group: str,
) -> None:
    """Try to import an existing Azure resource group into Tofu state.

    If the resource group already exists in Azure but is not tracked in
    the local Tofu state (e.g. fresh workspace, or state was deleted),
    ``tofu apply`` would fail with "already exists". Importing it first
    makes apply a no-op.  When the import fails the resource group
    likely does not exist yet — apply will create it.
    """
    # Resolve the subscription id via Azure CLI (must be authenticated).
    result = backend.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"],
    )
    if not result.success:
        return  # can't query; let apply try anyway
    sub_id = (result.stdout or "").strip()
    if not sub_id:
        return

    resource_id = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
    # Intentionally ignore result — import is best-effort.
    backend.run(
        ["tofu", "import", "azurerm_resource_group.main", resource_id],
        cwd=str(shared),
    )
