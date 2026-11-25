#!/usr/bin/env python3
"""Apply Authentik SAML 2.0 SSO config to Fleet Free via inline metadata XML.

Fleet blocks outbound `metadata_url` fetches to private/CGNAT (Tailscale 100.64.0.0/10)
addresses as SSRF protection, so we fetch the live IdP metadata locally and pass it
inline via `sso_settings.metadata` to `fleetctl apply -f -`.

Run on macOS ノート PC (after `fleetctl config set` + `fleetctl login`):
  python3 stacks/fleet/configure_saml.py
"""

import subprocess
import tempfile
import urllib.request

METADATA_URL = "https://auth.home.example.com/application/saml/fleet/metadata/"
ENTITY_ID = "https://fleet.home.example.com"


def main() -> None:
    with urllib.request.urlopen(METADATA_URL, timeout=15) as resp:
        xml_text = resp.read().decode("utf-8").strip()

    indented_xml = "\n".join("      " + line for line in xml_text.splitlines())
    yaml_doc = f"""apiVersion: v1
kind: config
spec:
  sso_settings:
    enable_sso: true
    idp_name: authentik
    entity_id: {ENTITY_ID}
    metadata_url: ""
    metadata: |
{indented_xml}
    enable_sso_idp_login: false
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", encoding="utf-8") as tmp:
        tmp.write(yaml_doc)
        tmp.flush()
        subprocess.run(["fleetctl", "apply", "-f", tmp.name], check=True)
    print("OK: Applied Authentik SAML SSO config (inline metadata) to Fleet.")


if __name__ == "__main__":
    main()
