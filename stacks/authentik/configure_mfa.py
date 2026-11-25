"""Idempotently configure Authentik MFA validation & enrollment stages (Issue #2).

Run inside authentik-server:
  docker exec -i authentik-server ak shell < stacks/authentik/configure_mfa.py

Policy:
- Allowed MFA classes: webauthn (Passkey), totp, static (recovery codes)
- not_configured_action: configure (enforces MFA enrollment on login without lockout)
- Enrollment choices offered: Passkey (WebAuthn) or TOTP
- Static recovery codes: 10 tokens (12 chars each)
"""

from authentik.stages.authenticator_static.models import AuthenticatorStaticStage
from authentik.stages.authenticator_totp.models import AuthenticatorTOTPStage
from authentik.stages.authenticator_validate.models import (
    AuthenticatorValidateStage,
    DeviceClasses,
    NotConfiguredAction,
)
from authentik.stages.authenticator_webauthn.models import AuthenticatorWebAuthnStage

totp_setup = AuthenticatorTOTPStage.objects.get(name="default-authenticator-totp-setup")
webauthn_setup = AuthenticatorWebAuthnStage.objects.get(name="default-authenticator-webauthn-setup")
static_setup = AuthenticatorStaticStage.objects.get(name="default-authenticator-static-setup")

# Expand static recovery codes to 10 tokens
static_setup.token_count = 10
static_setup.token_length = 12
static_setup.save()

# Enforce MFA on default-authentication-flow with guided enrollment (no silent skip)
validate = AuthenticatorValidateStage.objects.get(name="default-authentication-mfa-validation")
validate.device_classes = [
    DeviceClasses.WEBAUTHN,
    DeviceClasses.TOTP,
    DeviceClasses.STATIC,
]
validate.not_configured_action = NotConfiguredAction.CONFIGURE
validate.last_auth_threshold = "seconds=0"
validate.save()
validate.configuration_stages.set([webauthn_setup, totp_setup])
validate.save()

print(
    "MFA_CONFIG_OK:",
    {
        "device_classes": validate.device_classes,
        "not_configured_action": validate.not_configured_action,
        "configuration_stages": [s.name for s in validate.configuration_stages.all()],
        "static_token_count": static_setup.token_count,
    },
)
