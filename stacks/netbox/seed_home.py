"""Idempotent NetBox seed script (sanitized sample inventory).

Run inside the NetBox container:
    docker compose exec -T netbox /opt/netbox/venv/bin/python \
        /opt/netbox/netbox/manage.py shell < seed_home.py

Source of truth: docs/devices.md and docs/network.md (sanitized placeholders).
Safe to re-run; existing objects are preserved via get_or_create.
"""

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from ipam.models import IPAddress, IPRange, Prefix
from virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface


def ensure_site():
    site, created = Site.objects.get_or_create(
        slug="home",
        defaults={
            "name": "Home",
            "status": "active",
            "time_zone": "Asia/Tokyo",
            "description": "自宅ネットワーク拠点（サンプル）",
        },
    )
    print(f"Site: {site.name} ({'created' if created else 'exists'})")
    return site


def ensure_ipam(site):
    lan_prefix, created = Prefix.objects.get_or_create(
        prefix="192.168.50.0/24",
        defaults={
            "status": "active",
            "description": "Home LAN (Sample)",
        },
    )
    if lan_prefix.scope != site:
        lan_prefix.scope = site
        lan_prefix.save()
    print(f"Prefix: {lan_prefix.prefix} ({'created' if created else 'exists'})")

    ts_prefix, created = Prefix.objects.get_or_create(
        prefix="100.64.0.0/10",
        defaults={
            "status": "container",
            "description": "Tailscale Tailnet (CGNAT)",
        },
    )
    print(f"Prefix: {ts_prefix.prefix} ({'created' if created else 'exists'})")

    dhcp_range, created = IPRange.objects.get_or_create(
        start_address="192.168.50.100/24",
        end_address="192.168.50.199/24",
        defaults={
            "status": "active",
            "description": "Home Router DHCP Pool",
        },
    )
    print(f"IPRange: {dhcp_range} ({'created' if created else 'exists'})")


def ensure_roles():
    role_specs = [
        ("Router", "router", "2196f3", "ネットワークルーター", False),
        ("Server", "server", "4caf50", "常駐サーバー・検証ホスト", True),
        ("Workstation", "workstation", "607d8b", "デスクトップ・ノートPC", True),
        ("Mobile", "mobile", "00bcd4", "スマートフォン・モバイル端末", False),
        ("IoT", "iot", "ff9800", "スマート家電・IoT機器", False),
    ]
    roles = {}
    for name, slug, color, desc, vm_role in role_specs:
        role, created = DeviceRole.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "color": color, "description": desc, "vm_role": vm_role},
        )
        roles[slug] = role
        print(f"Role: {role.name} ({'created' if created else 'exists'})")
    return roles


def ensure_device_types():
    manufacturers = {}
    for name, slug in [
        ("Generic", "generic"),
        ("Apple", "apple"),
        ("Custom", "custom"),
    ]:
        m, created = Manufacturer.objects.get_or_create(slug=slug, defaults={"name": name})
        manufacturers[slug] = m
        print(f"Manufacturer: {m.name} ({'created' if created else 'exists'})")

    type_specs = [
        ("generic", "Home Wi-Fi Router", "home-router"),
        ("generic", "Smart Bulb IoT", "iot-bulb"),
        ("generic", "x86_64 Linux Server", "linux-desktop"),
        ("apple", "macOS Laptop", "macos-laptop"),
        ("apple", "iOS Mobile", "ios-mobile"),
        ("custom", "Windows Desktop", "windows-desktop"),
    ]
    dtypes = {}
    for m_slug, model, slug in type_specs:
        dt, created = DeviceType.objects.get_or_create(
            manufacturer=manufacturers[m_slug],
            slug=slug,
            defaults={"model": model, "u_height": 0},
        )
        dtypes[slug] = dt
        print(f"DeviceType: {dt} ({'created' if created else 'exists'})")
    return dtypes


def assign_ip(iface, address, description=""):
    ip, created = IPAddress.objects.get_or_create(
        address=address,
        defaults={
            "status": "active",
            "description": description,
        },
    )
    if ip.assigned_object != iface:
        ip.assigned_object = iface
        if description and not ip.description:
            ip.description = description
        ip.save()
    return ip, created


def ensure_devices(site, roles, dtypes):
    device_specs = [
        {
            "name": "home-router",
            "role": "router",
            "dtype": "home-router",
            "description": "既設ホームルーター（二重 NAT、LAN GW 192.168.50.1）",
            "interfaces": [
                ("lan", "1000base-t", "192.168.50.1/24", "Home LAN Gateway", True),
            ],
        },
        {
            "name": "ubuntu-desktop",
            "role": "server",
            "dtype": "linux-desktop",
            "description": "Phase 1 Docker ホスト（x86_64 / 12GB / 240GB SSD / Ubuntu 24.04 LTS）",
            "interfaces": [
                ("eno1", "1000base-t", "192.168.50.4/24", "有線 LAN 固定 IP", False),
                ("tailscale0", "virtual", "100.64.0.10/32", "Tailscale", True),
            ],
        },
        {
            "name": "workstation-01",
            "role": "workstation",
            "dtype": "windows-desktop",
            "description": "Windows デスクトップ（有線 LAN、DHCP 予約 .50.2）",
            "interfaces": [
                ("eth0", "1000base-t", "192.168.50.2/24", "有線 LAN 予約 IP", False),
                ("tailscale0", "virtual", "100.64.0.40/32", "Tailscale", True),
            ],
        },
        {
            "name": "laptop-01",
            "role": "workstation",
            "dtype": "macos-laptop",
            "description": "macOS ノート PC（管理端末）",
            "interfaces": [
                ("en0", "ieee802.11ax", "192.168.50.3/24", "Wi-Fi 予約 IP", False),
                ("tailscale0", "virtual", "100.64.0.30/32", "Tailscale", True),
            ],
        },
        {
            "name": "mobile-01",
            "role": "mobile",
            "dtype": "ios-mobile",
            "description": "iOS 端末（LAN は DHCP 動的、Tailscale 固定）",
            "interfaces": [
                ("tailscale0", "virtual", "100.64.0.50/32", "Tailscale", True),
            ],
        },
        {
            "name": "iot-bulb-1",
            "role": "iot",
            "dtype": "iot-bulb",
            "description": "スマート電球 #1（現在 DHCP 動的 IP）",
            "interfaces": [("wlan0", "ieee802.11ax", None, "", False)],
        },
        {
            "name": "iot-bulb-2",
            "role": "iot",
            "dtype": "iot-bulb",
            "description": "スマート電球 #2（現在 DHCP 動的 IP）",
            "interfaces": [("wlan0", "ieee802.11ax", None, "", False)],
        },
        {
            "name": "iot-bulb-3",
            "role": "iot",
            "dtype": "iot-bulb",
            "description": "スマート電球 #3（現在 DHCP 動的 IP）",
            "interfaces": [("wlan0", "ieee802.11ax", None, "", False)],
        },
    ]

    for spec in device_specs:
        dev, created = Device.objects.get_or_create(
            name=spec["name"],
            defaults={
                "site": site,
                "role": roles[spec["role"]],
                "device_type": dtypes[spec["dtype"]],
                "status": "active",
                "description": spec["description"],
            },
        )
        print(f"Device: {dev.name} ({'created' if created else 'exists'})")

        primary_ip = None
        for if_name, if_type, ip_cidr, ip_desc, is_primary in spec["interfaces"]:
            iface, _ = Interface.objects.get_or_create(
                device=dev,
                name=if_name,
                defaults={"type": if_type},
            )
            if ip_cidr:
                ip, _ = assign_ip(iface, ip_cidr, f"{dev.name} {ip_desc}".strip())
                if is_primary:
                    primary_ip = ip

        if primary_ip and dev.primary_ip4 != primary_ip:
            dev.primary_ip4 = primary_ip
            dev.save()


def ensure_external_vm(roles):
    ctype, _ = ClusterType.objects.get_or_create(
        slug="external-cloud",
        defaults={"name": "External Cloud"},
    )
    cluster, _ = Cluster.objects.get_or_create(
        name="External",
        defaults={"type": ctype, "status": "active", "description": "自宅外の外部 VM 環境"},
    )
    vm, created = VirtualMachine.objects.get_or_create(
        name="remote-vm-01",
        defaults={
            "cluster": cluster,
            "role": roles["server"],
            "status": "active",
            "description": "外部 VM（自宅外環境、別リポジトリでバックアップ管理）",
        },
    )
    print(f"VM: {vm.name} ({'created' if created else 'exists'})")

    vm_iface, _ = VMInterface.objects.get_or_create(
        virtual_machine=vm,
        name="tailscale0",
    )
    ip, _ = assign_ip(vm_iface, "100.64.0.20/32", "remote-vm-01 Tailscale")
    if vm.primary_ip4 != ip:
        vm.primary_ip4 = ip
        vm.save()


def main():
    site = ensure_site()
    ensure_ipam(site)
    roles = ensure_roles()
    dtypes = ensure_device_types()
    ensure_devices(site, roles, dtypes)
    ensure_external_vm(roles)
    print("=== NetBox home inventory seed completed ===")


main()
