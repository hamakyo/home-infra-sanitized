#!/usr/bin/env python3
"""Idempotently provision a least-privilege read-only NetBox v4.7 API user & token.

Run on ubuntu-desktop:
  python3 stacks/netbox/configure_readonly_api.py [--env-file stacks/opencode/jobs/.env]

Security properties:
- Creates non-superuser `opencode-ro` with an unusable password.
- Grants an `ObjectPermission` with `actions=["view"]` ONLY on read-only
  inventory models (`dcim`, `ipam`, `virtualization`).
- Creates a NetBox v2 `Token` with `write_enabled=False` (double read-only guard).
- Writes `NETBOX_API_URL` and `NETBOX_API_TOKEN` ONLY into the target chmod-600
  `.env` file (never prints the token to stdout).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

DEFAULT_API_URL = "https://netbox.home.example.com/api"

DJANGO_SCRIPT = r"""
from django.contrib.contenttypes.models import ContentType
from users.models import ObjectPermission, Token, User

USERNAME = "opencode-ro"
PERM_NAME = "OpenCode Read-Only Inventory"
TOKEN_DESC = "opencode-weekly-inventory-ro"
MODELS = [
    ("dcim", "device"),
    ("dcim", "devicerole"),
    ("dcim", "devicetype"),
    ("dcim", "interface"),
    ("dcim", "manufacturer"),
    ("dcim", "platform"),
    ("dcim", "site"),
    ("ipam", "ipaddress"),
    ("ipam", "prefix"),
    ("ipam", "vlan"),
    ("virtualization", "virtualmachine"),
]

user, created = User.objects.get_or_create(
    username=USERNAME,
    defaults={"is_active": True, "is_superuser": False},
)
user.is_active = True
user.is_superuser = False
user.set_unusable_password()
user.save()

perm, _ = ObjectPermission.objects.get_or_create(
    name=PERM_NAME,
    defaults={
        "description": "Read-only view access for OpenCode weekly inventory batch",
        "enabled": True,
        "actions": ["view"],
    },
)
perm.enabled = True
perm.actions = ["view"]
perm.save()

cts = []
for app_label, model in MODELS:
    ct = ContentType.objects.filter(app_label=app_label, model=model).first()
    if ct is not None:
        cts.append(ct)
perm.object_types.set(cts)
perm.users.set([user])

Token.objects.filter(user=user, description=TOKEN_DESC).delete()
tok = Token.objects.create(
    user=user,
    version=2,
    write_enabled=False,
    enabled=True,
    description=TOKEN_DESC,
)
print(f"TOKEN_OUT=nbt_{tok.key}.{tok.token}")
"""


def upsert_env(env_path: Path, updates: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        matched = False
        for k, v in updates.items():
            if line.strip().startswith(f"{k}="):
                out.append(f"{k}={v}")
                seen.add(k)
                matched = True
                break
        if not matched:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision read-only NetBox API user & token.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/infra-admin/dev/home-infra/stacks/opencode/jobs/.env"),
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    args = parser.parse_args()

    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "netbox",
            "/opt/netbox/venv/bin/python",
            "/opt/netbox/netbox/manage.py",
            "shell",
            "-v",
            "0",
        ],
        input=DJANGO_SCRIPT,
        capture_output=True,
        text=True,
        check=True,
    )

    token_val = ""
    for line in proc.stdout.splitlines():
        if line.startswith("TOKEN_OUT="):
            token_val = line.split("=", 1)[1].strip()
    if not token_val:
        raise RuntimeError(f"Failed to extract NetBox token. stderr={proc.stderr[-300:]}")

    upsert_env(
        args.env_file,
        {
            "NETBOX_API_URL": args.api_url.rstrip("/"),
            "NETBOX_API_TOKEN": token_val,
        },
    )
    print(f"OK: netbox opencode-ro v2 read-only token written to {args.env_file} (chmod 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
