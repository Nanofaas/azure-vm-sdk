"""Azure Marketplace image and VM size discovery."""

from __future__ import annotations

import json

from ._backend import CommandBackend, run_command
from .models import ImageInfo, VmSize


def list_images(
    backend: CommandBackend,
    publisher: str = "Canonical",
) -> list[ImageInfo]:
    """List available VM images from Azure Marketplace."""
    result = run_command(
        backend,
        [
            "az",
            "vm",
            "image",
            "list",
            "--publisher",
            publisher,
            "--all",
            "--output",
            "json",
        ],
    )
    return ImageInfo.from_az_image_list(json.loads(result.stdout))


def list_sizes(
    backend: CommandBackend,
    location: str,
) -> list[VmSize]:
    """List available VM sizes in an Azure region."""
    result = run_command(
        backend,
        [
            "az",
            "vm",
            "list-sizes",
            "--location",
            location,
            "--output",
            "json",
        ],
    )
    return VmSize.from_az_vm_size_list(json.loads(result.stdout))
