"""Idempotently configure Authentik OIDC provider for Infra BI (Grafana)
and a read-only service account (data-ro) for Infra DWH ingestion.

Run inside authentik-server:
  docker exec -i authentik-server ak shell < stacks/data-platform/configure_authentik.py
"""

import json
from authentik.core.models import Application, Group, Token, TokenIntents, User, UserTypes
from authentik.crypto.models import CertificateKeyPair
from authentik.flows.models import Flow
from authentik.providers.oauth2.models import (
    OAuth2Provider,
    RedirectURI,
    RedirectURIMatchingMode,
    RedirectURIType,
    ScopeMapping,
)

authz_flow = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
inval_flow = Flow.objects.get(slug="default-provider-invalidation-flow")
signing_kp = CertificateKeyPair.objects.get(name="authentik Self-signed Certificate")

scope_managed = [
    "goauthentik.io/providers/oauth2/scope-openid",
    "goauthentik.io/providers/oauth2/scope-email",
    "goauthentik.io/providers/oauth2/scope-profile",
]
scopes = list(ScopeMapping.objects.filter(managed__in=scope_managed))

# 1. OIDC Provider & Application for Grafana (Infra BI)
provider, _ = OAuth2Provider.objects.update_or_create(
    name="Infra BI",
    defaults={
        "authorization_flow": authz_flow,
        "invalidation_flow": inval_flow,
        "client_type": "confidential",
        "grant_types": ["authorization_code", "refresh_token"],
        "signing_key": signing_kp,
        "redirect_uris": [
            RedirectURI(
                matching_mode=RedirectURIMatchingMode.STRICT,
                url="https://bi.home.example.com/login/generic_oauth",
                redirect_uri_type=RedirectURIType.AUTHORIZATION,
            )
        ],
    },
)
provider.property_mappings.set(scopes)
provider.save()

Application.objects.update_or_create(
    slug="infra-bi",
    defaults={
        "name": "Infra BI",
        "provider": provider,
        "meta_launch_url": "https://bi.home.example.com",
    },
)

# 2. Read-only service account & non-expiring API token for Infra DWH
ro_group = Group.objects.get(name="authentik Read-only")
ro_user, _ = User.objects.update_or_create(
    username="data-ro",
    defaults={
        "name": "Infra DWH Read-Only",
        "type": UserTypes.SERVICE_ACCOUNT,
        "is_active": True,
    },
)
ro_user.ak_groups.add(ro_group)
ro_user.save()

token, _ = Token.objects.update_or_create(
    identifier="infra-dwh-data-ro",
    defaults={
        "user": ro_user,
        "intent": TokenIntents.INTENT_API,
        "expiring": False,
        "description": "Read-only API token for Infra DWH ingestion",
    },
)

print(
    "INFRA_AUTHENTIK_JSON="
    + json.dumps(
        {
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "api_token": token.key,
        }
    )
)
