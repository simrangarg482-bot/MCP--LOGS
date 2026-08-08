"""JWT inspection helpers.

Two genuinely different operations, kept in two separate functions on
purpose:

1. `decode_unverified` -- pure base64 decoding of the JWT header/payload
   segments. No signature check, no dependency on any secret. Works on any
   token, including a deliberately-tampered one (used directly by the
   "tampered token" negative test) or one this environment knows nothing
   about. This answers "what does this token *claim*", never "is this
   token real".

2. `verify_with_project` -- imports and calls the project's own,
   completely unmodified `app.core.auth.service.verify_access_token`
   in-process. This is deliberately NOT a reimplementation of JWT
   verification: EKIP signs its own session tokens symmetrically (HS256,
   `Settings.jwt_secret_key`) rather than with a public/private keypair, so
   there is no separate JWKS endpoint publishing a *public* key for these
   tokens the way there is for an upstream IdP's ID tokens (that JWKS
   lookup happens server-side, inside `core.auth.service.
   _exchange_code_for_claims`, against a completely different token that
   is never returned to the client). Given that, calling the project's own
   verifier is the most faithful "verify the signature the way the project
   verifies it" available to an external script -- short of embedding a
   second, independent JWT implementation that would have no better claim
   to correctness than the original.

   This requires running from an environment where `app` is importable
   (this repo's root is put on sys.path below) AND where this process's
   environment resolves to the *same* JWT_SECRET_KEY / JWT_ALGORITHM the
   running API server itself is using. If they differ, every real token
   will fail verification here even though the live server accepts it
   fine -- that is a test-harness environment mismatch, not a product bug;
   see this harness's README troubleshooting section.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


class TokenVerificationUnavailable(RuntimeError):
    """Raised when the project's own verifier can't even be imported (e.g.
    this harness's requirements were installed into a different virtualenv
    than the project's own dependencies). Distinguished from "the token is
    invalid" (a normal, expected negative-test outcome) so callers don't
    conflate a broken environment with a passing security check.
    """


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_unverified(token: str) -> dict[str, Any]:
    """Split `token` into its header/payload dicts without checking the
    signature. Raises ValueError on a malformed token.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Not a JWT: expected 3 dot-separated segments, got {len(parts)}.")
    header = json.loads(_b64url_decode(parts[0]))
    payload = json.loads(_b64url_decode(parts[1]))
    return {"header": header, "payload": payload}


def verify_with_project(token: str) -> dict[str, Any]:
    """Verify `token` using the project's real, unmodified
    `core.auth.service.verify_access_token`.

    Returns the verified claims as a plain dict on success
    (`user_id`, `organization_id`, `issued_at`, `expires_at`).

    Raises the project's own `app.core.exceptions.PermissionDeniedError`
    on an invalid/expired/malformed token -- callers in this harness catch
    that specific exception, never a bare `Exception`, so an import failure
    (see `TokenVerificationUnavailable` above) is never mistaken for "the
    token was rejected".
    """
    try:
        from app.core.auth.service import verify_access_token
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any import-time
        # failure here means "this harness's environment can't load the
        # project", not "the token is invalid" -- surfaced distinctly.
        raise TokenVerificationUnavailable(
            "Could not import app.core.auth.service.verify_access_token. "
            "Run this harness from the project root with the project's own "
            "virtualenv active (pip install -r requirements.txt at the repo "
            f"root), not just this harness's own requirements.txt. Original "
            f"error: {type(exc).__name__}: {exc}"
        ) from exc

    claims = verify_access_token(token)
    return claims.model_dump()


def craft_token_with_project_secret(
    user_id: str, organization_id: str, *, issued_at, expires_at
) -> str:
    """Build an access token using the project's OWN signing secret and
    algorithm (`Settings.jwt_secret_key`/`jwt_algorithm`), with caller-
    chosen `issued_at`/`expires_at` -- used only by
    `09_negative_tests.py` to construct fixtures like "an already-expired
    token" or "a token asserting an organization_id its subject doesn't
    belong to", which cannot be obtained through any legitimate API call.

    This is read-only use of the project's already-public signing logic
    (the same `python-jose` call `core.auth.service._issue_access_token`
    itself makes) against its own configuration -- nothing under `app/`
    is modified, and this function produces test fixtures only, never
    used to authenticate a real action against production data.

    Requires the same environment as `verify_with_project` -- this
    process's settings must resolve to the same JWT_SECRET_KEY the target
    server is using, since the whole point is producing a token that
    server will recognize as validly signed (but rejected for a different,
    specific reason: expiry, or a permission mismatch).
    """
    try:
        from jose import jwt as jose_jwt

        from app.shared.config.settings import get_settings
    except Exception as exc:  # noqa: BLE001
        raise TokenVerificationUnavailable(
            f"Could not import project settings/jose: {type(exc).__name__}: {exc}"
        ) from exc

    settings = get_settings()
    claims = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
