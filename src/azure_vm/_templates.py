"""HCL templates and helper functions for OpenTofu workspace generation."""

from __future__ import annotations

from pathlib import Path

import yaml

SHARED_TEMPLATE = """\
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
  resource_provider_registrations = "none"
}}

resource "azurerm_resource_group" "main" {{
  name     = "{resource_group}"
  location = "{location}"
}}
"""

VM_TEMPLATE = """\
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
  resource_provider_registrations = "none"
}}

data "azurerm_resource_group" "main" {{
  name = "{resource_group}"
}}

resource "azurerm_virtual_network" "vm" {{
  name                = "${{var.vm_name}}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
}}

resource "azurerm_subnet" "vm" {{
  name                 = "${{var.vm_name}}-subnet"
  resource_group_name  = data.azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.vm.name
  address_prefixes     = ["10.0.1.0/24"]
}}

resource "azurerm_network_security_group" "vm" {{
  name                = "${{var.vm_name}}-nsg"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name

  security_rule {{
    name                       = "SSH"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }}
{extra_security_rules}}}

resource "azurerm_subnet_network_security_group_association" "vm" {{
  subnet_id                 = azurerm_subnet.vm.id
  network_security_group_id = azurerm_network_security_group.vm.id
}}

resource "azurerm_public_ip" "vm" {{
  name                = "${{var.vm_name}}-pip"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  allocation_method   = "Static"
}}

resource "azurerm_network_interface" "vm" {{
  name                = "${{var.vm_name}}-nic"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name

  ip_configuration {{
    name                          = "internal"
    subnet_id                     = azurerm_subnet.vm.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.vm.id
  }}
}}

resource "azurerm_linux_virtual_machine" "vm" {{
  name                  = var.vm_name
  location              = data.azurerm_resource_group.main.location
  resource_group_name   = data.azurerm_resource_group.main.name
  network_interface_ids = [azurerm_network_interface.vm.id]
  size                  = var.vm_size
  computer_name         = var.vm_name
  admin_username        = "{ssh_username}"
  disable_password_authentication = true

  admin_ssh_key {{
    username   = "{ssh_username}"
    public_key = file("{ssh_public_key_path}")
  }}

  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = var.disk_size_gb
  }}

  source_image_reference {{
    publisher = "{image_publisher}"
    offer     = "{image_offer}"
    sku       = "{image_sku}"
    version   = "{image_version}"
  }}
{custom_data_block}
}}

variable "vm_name" {{
  type = string
}}

variable "vm_size" {{
  type    = string
  default = "Standard_B1s"
}}

variable "disk_size_gb" {{
  type    = number
  default = 30
}}

variable "desired_state" {{
  type    = string
  default = "running"
}}

output "vm_ip" {{
  value = azurerm_public_ip.vm.ip_address
}}

output "vm_state" {{
  value = var.desired_state
}}

output "location" {{
  value = data.azurerm_resource_group.main.location
}}

output "vm_size" {{
  value = var.vm_size
}}

output "image_urn" {{
  value = "{image_urn}"
}}

output "resource_group" {{
  value = "{resource_group}"
}}
"""


def parse_image_urn(image_urn: str | None) -> tuple[str, str, str, str]:
    """Parse an Azure image URN into (publisher, offer, sku, version)."""
    if not image_urn:
        return ("Canonical", "ubuntu-24_04-lts", "server-gen1", "latest")
    parts = image_urn.split(":")
    return (
        parts[0] if len(parts) > 0 and parts[0] else "Canonical",
        parts[1] if len(parts) > 1 and parts[1] else "ubuntu-24_04-lts",
        parts[2] if len(parts) > 2 and parts[2] else "server-gen1",
        parts[3] if len(parts) > 3 and parts[3] else "latest",
    )


def resolve_ssh_path(
    ssh_key_path: str | None,
    default_path: str | None = None,
) -> str:
    """Resolve an SSH key path, expanding ~ and applying fallback chain."""
    return str(Path(ssh_key_path or default_path or "~/.ssh/id_rsa.pub").expanduser())


def write_cloud_init(workspace: Path, config: dict | str | None) -> str:
    """Write cloud-init file to *workspace*. Returns the HCL custom_data block."""
    if config is None:
        return ""
    if isinstance(config, dict):
        content = "#cloud-config\n" + yaml.dump(config, default_flow_style=False)
    else:
        content = config
    (workspace / "cloud-init.yaml").write_text(content)
    return '  custom_data = filebase64("cloud-init.yaml")\n'


def render_security_rules(open_ports) -> str:
    """Extra inbound TCP allow-rules for the per-VM NSG.

    Port 22 is skipped (the SSH rule is always rendered). Source is "*",
    matching the SSH rule's posture — these are short-lived e2e VMs; source
    restriction is a possible future enhancement.
    """
    rules: list[str] = []
    for index, port in enumerate(sorted({int(p) for p in (open_ports or [])})):
        if port == 22:
            continue
        rules.append(
            "\n  security_rule {\n"
            f'    name                       = "Port{port}"\n'
            f"    priority                   = {1010 + index}\n"
            '    direction                  = "Inbound"\n'
            '    access                     = "Allow"\n'
            '    protocol                   = "Tcp"\n'
            '    source_port_range          = "*"\n'
            f'    destination_port_range     = "{port}"\n'
            '    source_address_prefix      = "*"\n'
            '    destination_address_prefix = "*"\n'
            "  }\n"
        )
    return "".join(rules)
