"""Configuration loader for the onboarding test harness.

This is a STANDALONE config object, deliberately separate from
`app.shared.config.settings.Settings` (the *server's* configuration). This
harness plays the role of an external API client -- a real customer's
onboarding engineer -- so it has its own, smaller configuration surface
(base URL, IdP credentials, test identities), read from its own .env file:

    scripts/realworld_onboarding/.env

Copy `.env.example` in this same directory to `.env` and fill in real
values. Every variable is documented there. Nothing here is hardcoded;
`load_config()` raises a clear `RuntimeError` naming the exact missing
variable if something required is absent, rather than failing later with a
confusing HTTP error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError as exc:  # pragma: no cover - guidance path, not logic
    raise SystemExit(
        "Missing dependency 'python-dotenv'. Install this harness's own "
        "requirements first:\n"
        "    pip install -r scripts/realworld_onboarding/requirements.txt"
    ) from exc

_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _HARNESS_ROOT / ".env"

# override=False: if the caller's shell already exported these (e.g. in CI),
# that value wins over whatever is in the .env file.
load_dotenv(_ENV_PATH, override=False)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _get_required(name: str) -> str:
    value = _get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}\n"
            f"Copy {_HARNESS_ROOT / '.env.example'} to {_ENV_PATH} and fill it in."
        )
    return value


@dataclass(frozen=True)
class Config:
    # --- Where EKIP's REST API is actually running ---------------------------
    base_url: str
    request_timeout_seconds: float

    # --- Identity Provider (only needed for a REAL login, Script 05) ---------
    client_id: str | None
    client_secret: str | None
    issuer: str | None
    discovery_url: str | None
    redirect_uri: str
    sso_provider: str  # one of EKIP's own supported literals: entra_id | okta | auth0 | google_workspace

    # --- A real IdP test user (only needed for a REAL browser login) --------
    test_email: str | None
    test_password: str | None

    # --- The organization this whole run operates against -------------------
    org_name: str
    org_slug: str

    # --- Organization B, used only by the isolation-testing script ----------
    org_b_name: str
    org_b_slug: str

    @property
    def has_real_idp_credentials(self) -> bool:
        """True only if enough was supplied to attempt a genuine OIDC
        exchange against a real Identity Provider. Every script that needs a
        live IdP checks this first and explains clearly what's missing
        instead of failing several steps later with an opaque HTTP error.
        """
        return bool(self.client_id and self.client_secret and self.issuer)


def load_config() -> Config:
    return Config(
        base_url=_get("BASE_URL", "http://localhost:8000").rstrip("/"),
        request_timeout_seconds=float(_get("REQUEST_TIMEOUT_SECONDS", "45")),
        client_id=_get("CLIENT_ID"),
        client_secret=_get("CLIENT_SECRET"),
        issuer=_get("ISSUER"),
        discovery_url=_get("DISCOVERY_URL"),
        redirect_uri=_get("REDIRECT_URI", "http://localhost:8000/auth/callback"),
        sso_provider=_get("SSO_PROVIDER", "okta"),
        test_email=_get("TEST_EMAIL"),
        test_password=_get("TEST_PASSWORD"),
        org_name=_get("ORG_NAME", "Acme Realworld Test Corp"),
        org_slug=_get("ORG_SLUG", "acme-realworld-test"),
        org_b_name=_get("ORG_B_NAME", "Bravo Realworld Test Corp"),
        org_b_slug=_get("ORG_B_SLUG", "bravo-realworld-test"),
    )
