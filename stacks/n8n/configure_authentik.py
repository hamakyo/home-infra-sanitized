"""Idempotently configure Authentik Proxy Provider (Forward Auth, single app)
for n8n (https://n8n.home.example.com) and bind it to the Embedded Outpost.

Run inside authentik-server:
  docker exec -i authentik-server ak shell < stacks/n8n/configure_authentik.py
"""

from authentik.core.models import Application
from authentik.flows.models import Flow
from authentik.outposts.models import Outpost
from authentik.providers.proxy.models import ProxyMode, ProxyProvider

authz_flow = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
inval_flow = Flow.objects.get(slug="default-provider-invalidation-flow")

provider, created = ProxyProvider.objects.update_or_create(
    name="n8n",
    defaults={
        "authorization_flow": authz_flow,
        "invalidation_flow": inval_flow,
        "external_host": "https://n8n.home.example.com",
        "mode": ProxyMode.FORWARD_SINGLE,
        "access_token_validity": "hours=24",
    },
)
provider.set_oauth_defaults()
provider.save()

app, _ = Application.objects.update_or_create(
    slug="n8n",
    defaults={
        "name": "n8n",
        "provider": provider,
        "meta_launch_url": "https://n8n.home.example.com",
    },
)

outpost = Outpost.objects.get(managed="goauthentik.io/outposts/embedded")
outpost.providers.add(provider)
outpost.save()

print(f"Configured Authentik ProxyProvider n8n (pk={provider.pk}, mode={provider.mode}, created={created}) on Embedded Outpost.")
