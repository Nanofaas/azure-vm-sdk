"""Enumerations and dataclasses shared by the client and the VM handles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class VmArchitecture(Enum):
    """CPU architecture a VM size is built on."""

    ARM64 = "arm64"
    X86_64 = "x86_64"


class VmState(Enum):
    """Lifecycle state a VM last reported.

    Any value Azure returns that is not listed here resolves to ``UNKNOWN``
    instead of raising ``ValueError``.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> VmState:
        return cls.UNKNOWN


@dataclass
class VmInfo:
    """Snapshot of a VM's state and identifying details."""

    name: str
    state: VmState
    ipv4: list[str]
    location: str
    vm_size: str
    image_urn: str
    resource_group: str

    @classmethod
    def from_tofu_output(cls, data: dict, name: str) -> VmInfo:
        """Build a snapshot from parsed ``tofu output -json`` data.

        Each field falls back to an empty string when its output is absent;
        ``ipv4`` is a one-element list when ``vm_ip`` is set and empty
        otherwise. ``name`` is supplied by the caller because the Tofu output
        does not carry it.
        """
        ip = data.get("vm_ip", {}).get("value", "")
        state_raw = data.get("vm_state", {}).get("value", "unknown")
        return cls(
            name=name,
            state=VmState(state_raw),
            ipv4=[ip] if ip else [],
            location=data.get("location", {}).get("value", ""),
            vm_size=data.get("vm_size", {}).get("value", ""),
            image_urn=data.get("image_urn", {}).get("value", ""),
            resource_group=data.get("resource_group", {}).get("value", ""),
        )


_ARM64_FAMILIES = frozenset(
    {
        "Dps_v5",
        "Dpds_v5",
        "Dpls_v5",
        "Dplds_v5",
        "Eps_v5",
        "Epds_v5",
        "Epls_v5",
        "Eplds_v5",
        "Dps_v6",
        "Dpds_v6",
        "Dpls_v6",
        "Dplds_v6",
        "Eps_v6",
        "Epds_v6",
        "Epls_v6",
        "Eplds_v6",
        "Bps_v2",
        "Bpls_v2",
    }
)


def _get_architecture(size_name: str) -> VmArchitecture:
    name = size_name.removeprefix("Standard_")
    family = re.sub(r"(?<=[A-Z])\d+", "", name)
    return VmArchitecture.ARM64 if family in _ARM64_FAMILIES else VmArchitecture.X86_64


@dataclass
class VmSize:
    """A VM size offered in an Azure region."""

    name: str
    number_of_cores: int
    memory_in_mb: int
    os_disk_size_in_mb: int
    resource_disk_size_in_mb: int
    max_data_disk_count: int
    architecture: VmArchitecture

    @classmethod
    def from_az_vm_size_list(cls, data: list[dict]) -> list[VmSize]:
        """Convert ``az vm list-sizes`` entries into :class:`VmSize` objects.

        Numeric fields default to zero when Azure omits them, and the
        architecture is inferred from each size name.
        """
        return [
            cls(
                name=s["name"],
                number_of_cores=s.get("numberOfCores", 0),
                memory_in_mb=s.get("memoryInMb", 0),
                os_disk_size_in_mb=s.get("osDiskSizeInMb", 0),
                resource_disk_size_in_mb=s.get("resourceDiskSizeInMb", 0),
                max_data_disk_count=s.get("maxDataDiskCount", 0),
                architecture=_get_architecture(s["name"]),
            )
            for s in data
        ]


@dataclass
class VmConfig:
    """Configuration for one VM requested from ``AzureClient.launch``."""

    name: str | None = None
    vm_size: str = "Standard_B1s"
    disk_size_gb: int = 30
    image_urn: str | None = None
    cloud_init_config: dict | str | None = None
    ssh_key_path: str | None = None
    open_ports: tuple[int, ...] | None = None


@dataclass
class ImageInfo:
    """A VM image offered by an Azure Marketplace publisher."""

    publisher: str
    offer: str
    sku: str
    version: str

    @classmethod
    def from_az_image_list(cls, data: list[dict]) -> list[ImageInfo]:
        """Convert ``az vm image list`` entries into :class:`ImageInfo` objects.

        Fields missing from the Azure response become empty strings so a
        partial result still yields usable entries.
        """
        return [
            cls(
                publisher=img.get("publisher", ""),
                offer=img.get("offer", ""),
                sku=img.get("sku", ""),
                version=img.get("version", ""),
            )
            for img in data
        ]
