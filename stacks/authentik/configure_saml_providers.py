"""Idempotently configure Authentik SAML 2.0 providers & apps for Zabbix and Fleet.

Run inside authentik-server:
  docker exec -i authentik-server ak shell < stacks/authentik/configure_saml_providers.py
"""

from authentik.core.models import Application
from authentik.crypto.models import CertificateKeyPair
from authentik.flows.models import Flow
from authentik.providers.saml.models import (
    SAMLBindings,
    SAMLLogoutMethods,
    SAMLPropertyMapping,
    SAMLProvider,
)

authz_flow = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
inval_flow = Flow.objects.get(slug="default-provider-invalidation-flow")
signing_kp = CertificateKeyPair.objects.get(name="authentik Self-signed Certificate")

default_managed = [
    "goauthentik.io/providers/saml/upn",
    "goauthentik.io/providers/saml/name",
    "goauthentik.io/providers/saml/email",
    "goauthentik.io/providers/saml/username",
    "goauthentik.io/providers/saml/uid",
    "goauthentik.io/providers/saml/groups",
]
default_mappings = list(SAMLPropertyMapping.objects.filter(managed__in=default_managed))
email_mapping = SAMLPropertyMapping.objects.get(managed="goauthentik.io/providers/saml/email")

# 1. Zabbix 8.0 LTS (SAML 2.0)
zabbix_provider, _ = SAMLProvider.objects.update_or_create(
    name="Zabbix",
    defaults={
        "authorization_flow": authz_flow,
        "invalidation_flow": inval_flow,
        "acs_url": "https://zabbix.home.example.com/index_sso.php?acs",
        "sp_binding": SAMLBindings.POST,
        "audience": "https://zabbix.home.example.com",
        "sls_url": "https://zabbix.home.example.com/index_sso.php?sls",
        "sls_binding": SAMLBindings.REDIRECT,
        "logout_method": SAMLLogoutMethods.FRONTCHANNEL_IFRAME,
        "signing_kp": signing_kp,
        "sign_assertion": True,
        "sign_response": True,
        "name_id_mapping": None,
    },
)
zabbix_provider.property_mappings.set(default_mappings)

Application.objects.update_or_create(
    slug="zabbix",
    defaults={
        "name": "Zabbix",
        "meta_launch_url": "https://zabbix.home.example.com",
        "provider": zabbix_provider,
    },
)

# 2. Fleet Free (SAML 2.0 — NameID must carry user email)
fleet_provider, _ = SAMLProvider.objects.update_or_create(
    name="Fleet",
    defaults={
        "authorization_flow": authz_flow,
        "invalidation_flow": inval_flow,
        "acs_url": "https://fleet.home.example.com/api/v1/fleet/sso/callback",
        "sp_binding": SAMLBindings.POST,
        "audience": "https://fleet.home.example.com",
        "signing_kp": signing_kp,
        "sign_assertion": True,
        "sign_response": True,
        "name_id_mapping": email_mapping,
    },
)
fleet_provider.property_mappings.set(default_mappings)

Application.objects.update_or_create(
    slug="fleet",
    defaults={
        "name": "Fleet",
        "meta_launch_url": "https://fleet.home.example.com",
        "provider": fleet_provider,
    },
)

print("OK: Configured Authentik SAML providers & applications:")
print("  - zabbix: https://auth.home.example.com/application/saml/zabbix/metadata/")
print("  - fleet:  https://auth.home.example.com/application/saml/fleet/metadata/")
