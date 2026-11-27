"""A local, SIMULATED OpenID Connect provider for tests.

It speaks real HTTP on 127.0.0.1 (discovery, authorize, token, JWKS), signs ID
tokens with a real RSA key, and enforces what a real provider enforces that
EKIP depends on: the authorization code is single-use, the redirect URI must
match, the client secret must match, and **PKCE is verified**
(`sha256(code_verifier) == code_challenge`). It echoes the request's `nonce`
into the ID token.

It is NOT a real Entra ID / Okta / Auth0 / Google tenant. `flavor` only
reproduces the documented *differences that affect EKIP* (Auth0's trailing-slash
issuer; Entra's `xms_edov` instead of `email_verified`; Google's `hd` claim), so
a passing test shows EKIP handles those shapes -- not that a real tenant
interoperates. Real-provider verification is a separate, manual step.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_int(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse-DNS lookup (getfqdn): seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


_KEYS: list = []


def _shared_keys():
    """Two throw-away RSA keys, generated once per test process (keygen is slow)."""
    if not _KEYS:
        _KEYS.extend(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))
    return _KEYS


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


class MockOidcProvider:
    def __init__(self, flavor: str = "okta", client_id: str = "ekip-client", client_secret: str = "s3cret") -> None:
        self.flavor = flavor
        self.client_id = client_id
        self.client_secret = client_secret
        self.kid = "mock-key-1"
        self._key, other = _shared_keys()
        self._pem = _pem(self._key)
        self._other_pem = _pem(other)
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []
        self.authorize_requests: list[dict] = []
        self.requests_seen: list[str] = []  # "METHOD path" for every request
        # behaviour switches a test can flip
        self.cancel_next = False
        self.nonce_override: str | None = None
        self.drop_nonce = False
        self.sign_with_other_key = False
        self.raw_id_token: str | None = None
        self.iss_override: str | None = None
        self.aud_override: object = None
        self.expires_in = 300
        self.token_http_status: int | None = None
        self.discovery_issuer_override: str | None = None
        self.discovery_status = 200
        self.extra_claims: dict = {}
        self.drop_claims: set[str] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self, port: int = 0) -> "MockOidcProvider":
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence
                return

            def _send(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
                payload = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                provider.requests_seen.append(f"GET {urlsplit(self.path).path}")
                path = urlsplit(self.path).path
                if path.endswith("/.well-known/openid-configuration"):
                    if provider.discovery_status != 200:
                        return self._send(provider.discovery_status, {"error": "boom"})
                    return self._send(200, provider.discovery_document())
                if path == provider.base_path + "/keys":
                    return self._send(200, provider.jwks())
                if path == provider.base_path + "/authorize":
                    return provider._handle_authorize(self)
                self._send(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                provider.requests_seen.append(f"POST {urlsplit(self.path).path}")
                if urlsplit(self.path).path == provider.base_path + "/token":
                    length = int(self.headers.get("Content-Length", "0"))
                    form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                    return provider._handle_token(self, form)
                self._send(404, {"error": "not_found"})

        self._server = _Server(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # ------------------------------------------------------------------ addresses
    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_path(self) -> str:
        return "/tenant-id/v2.0" if self.flavor == "entra_id" else ""

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def configured_issuer(self) -> str:
        """What an administrator would type into EKIP (no trailing slash)."""
        return self.origin + self.base_path

    @property
    def token_issuer(self) -> str:
        """The `iss` the provider actually emits -- Auth0 adds a trailing slash."""
        return self.configured_issuer + ("/" if self.flavor == "auth0" else "")

    # ------------------------------------------------------------------ documents
    def discovery_document(self) -> dict:
        return {
            "issuer": self.discovery_issuer_override or self.token_issuer,
            "authorization_endpoint": f"{self.configured_issuer}/authorize",
            "token_endpoint": f"{self.configured_issuer}/token",
            "jwks_uri": f"{self.configured_issuer}/keys",
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def jwks(self) -> dict:
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url_int(numbers.n),
                    "e": _b64url_int(numbers.e),
                }
            ]
        }

    # ------------------------------------------------------------------ the "browser"
    def browser_login(
        self,
        authorization_url: str,
        *,
        email: str = "person@corp.test",
        sub: str | None = None,
        groups: list[str] | None = None,
        name: str = "Test Person",
    ) -> dict[str, str]:
        """What the person's browser does at the provider: open the authorization
        URL, sign in, get redirected back. Returns the redirect's query params."""
        self._next_user = {"email": email, "sub": sub or f"sub-{email}", "groups": groups, "name": name}
        response = httpx.get(authorization_url, follow_redirects=False, timeout=5)
        assert response.status_code == 302, response.text
        return {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}

    def _handle_authorize(self, handler) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(handler.path).query).items()}
        self.authorize_requests.append(query)
        redirect_uri = query.get("redirect_uri", "")
        if query.get("client_id") != self.client_id or query.get("response_type") != "code":
            return handler._send(400, {"error": "invalid_request"})
        if self.cancel_next:
            self.cancel_next = False
            location = redirect_uri + "?" + urlencode(
                {"error": "access_denied", "error_description": "The user cancelled", "state": query["state"]}
            )
            return handler._send(302, None, {"Location": location})
        code = uuid.uuid4().hex
        self.codes[code] = {
            "nonce": query.get("nonce"),
            "challenge": query.get("code_challenge"),
            "method": query.get("code_challenge_method"),
            "redirect_uri": redirect_uri,
            "user": dict(self._next_user),
        }
        location = redirect_uri + "?" + urlencode({"code": code, "state": query["state"]})
        handler._send(302, None, {"Location": location})

    # ------------------------------------------------------------------ token endpoint
    def _handle_token(self, handler, form: dict) -> None:
        self.token_requests.append(form)
        if self.token_http_status is not None:
            return handler._send(self.token_http_status, {"error": "server_error"})
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return handler._send(401, {"error": "invalid_client"})
        record = self.codes.pop(form.get("code", ""), None)  # single use
        if record is None or record["redirect_uri"] != form.get("redirect_uri"):
            return handler._send(400, {"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if record["method"] != "S256" or expected != record["challenge"]:
            return handler._send(400, {"error": "invalid_grant", "error_description": "PKCE failed"})
        handler._send(200, {"access_token": "opaque-access", "token_type": "Bearer", "id_token": self._id_token(record)})

    def _id_token(self, record: dict) -> str:
        if self.raw_id_token is not None:
            return self.raw_id_token
        user = record["user"]
        now = int(time.time())
        claims: dict = {
            "iss": self.iss_override or self.token_issuer,
            "aud": self.aud_override if self.aud_override is not None else self.client_id,
            "sub": user["sub"],
            "iat": now,
            "exp": now + self.expires_in,
            "email": user["email"],
            "name": user["name"],
        }
        if not self.drop_nonce:
            claims["nonce"] = self.nonce_override or record["nonce"]
        if self.flavor == "entra_id":
            claims["xms_edov"] = True  # Entra: no `email_verified` by default
        else:
            claims["email_verified"] = True
        if self.flavor == "google_workspace":
            claims["hd"] = user["email"].split("@")[1]
        if user["groups"] is not None:
            claims["groups"] = user["groups"]
        claims.update(self.extra_claims)
        for name in self.drop_claims:
            claims.pop(name, None)
        key = self._other_pem if self.sign_with_other_key else self._pem
        return jose_jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})

    # ------------------------------------------------------------------ forging helpers
    def forged_unsigned_token(self, claims: dict) -> str:
        header = _b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": self.kid}).encode())
        return f"{header}.{_b64url(json.dumps(claims).encode())}."

    def hs256_token(self, claims: dict) -> str:
        return jose_jwt.encode(claims, "shared-secret", algorithm="HS256", headers={"kid": self.kid})
