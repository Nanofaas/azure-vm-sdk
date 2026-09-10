from azure_vm.models import ImageInfo, VmInfo, VmState

TOFU_OUTPUT = {
    "vm_ip": {"value": "1.2.3.4"},
    "vm_state": {"value": "running"},
    "location": {"value": "westeurope"},
    "vm_size": {"value": "Standard_B1s"},
    "image_urn": {"value": "Canonical:ubuntu-24_04-lts:server-gen1:latest"},
    "resource_group": {"value": "my-rg"},
}


def test_vmstate_known_values():
    assert VmState.RUNNING.value == "running"
    assert VmState.STOPPED.value == "stopped"
    assert VmState.STARTING.value == "starting"
    assert VmState.STOPPING.value == "stopping"
    assert VmState.UNKNOWN.value == "unknown"


def test_vmstate_missing_falls_back_to_unknown():
    assert VmState("nonexistent") == VmState.UNKNOWN


def test_vminfo_from_tofu_output():
    info = VmInfo.from_tofu_output(TOFU_OUTPUT, "my-vm")
    assert info.name == "my-vm"
    assert info.state == VmState.RUNNING
    assert info.ipv4 == ["1.2.3.4"]
    assert info.location == "westeurope"
    assert info.vm_size == "Standard_B1s"
    assert info.image_urn == "Canonical:ubuntu-24_04-lts:server-gen1:latest"
    assert info.resource_group == "my-rg"


def test_vminfo_from_tofu_output_empty_ip():
    data = {**TOFU_OUTPUT, "vm_ip": {"value": ""}}
    info = VmInfo.from_tofu_output(data, "vm-no-ip")
    assert info.ipv4 == []


def test_vminfo_from_tofu_output_missing_ip():
    data = {k: v for k, v in TOFU_OUTPUT.items() if k != "vm_ip"}
    info = VmInfo.from_tofu_output(data, "vm-no-ip")
    assert info.ipv4 == []


def test_vminfo_from_tofu_output_unknown_state():
    data = {**TOFU_OUTPUT, "vm_state": {"value": "transitioning"}}
    info = VmInfo.from_tofu_output(data, "vm")
    assert info.state == VmState.UNKNOWN


AZ_IMAGE_LIST = [
    {
        "publisher": "Canonical",
        "offer": "ubuntu-24_04-lts",
        "sku": "server-gen1",
        "version": "latest",
    },
    {
        "publisher": "Canonical",
        "offer": "0001-com-ubuntu-server-jammy",
        "sku": "22_04-lts",
        "version": "latest",
    },
]


def test_imageinfo_from_az_image_list():
    images = ImageInfo.from_az_image_list(AZ_IMAGE_LIST)
    assert len(images) == 2
    assert images[0].publisher == "Canonical"
    assert images[0].offer == "ubuntu-24_04-lts"
    assert images[0].sku == "server-gen1"
    assert images[1].sku == "22_04-lts"
