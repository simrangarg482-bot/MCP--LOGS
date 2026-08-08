"""Thin, logging HTTP client used by every script in this harness.

Talks to the real, already-running EKIP REST API over the network, exactly
like any external API client would -- `httpx.Client(...).request(...)`,
nothing else. This deliberately never imports `app.api` or uses FastAPI's
`TestClient`: the entire point of this harness is exercising the deployed
API surface as a real customer's tooling would, not the in-process app
object.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .logger import StepLogger


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"

    def call(
        self,
        log: StepLogger,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue exactly one HTTP call, logging request/response/timing
        through `log`. Never raises on a non-2xx response -- callers decide
        pass/fail themselves, since a 403/404/409 is frequently the
        *expected* result in this harness (permission tests, negative
        tests, idempotency checks).
        """
        url = self._url(path)
        headers = dict(extra_headers or {})
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        log.request(method, url, payload=json_body)
        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(method, url, json=json_body, params=params, headers=headers)
        except httpx.ConnectError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            log.response(0, f"CONNECTION FAILED: {exc}", elapsed_ms)
            raise ConnectionRefused(
                f"Could not reach {self.base_url}. Is the EKIP API server actually "
                f"running there? See this harness's README, 'Running the app locally'."
            ) from exc

        elapsed_ms = (time.monotonic() - start) * 1000
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        log.response(response.status_code, body, elapsed_ms)
        return response


class ConnectionRefused(RuntimeError):
    """Raised when the target BASE_URL refuses the TCP connection entirely
    -- distinguished from a real HTTP error response so callers can print a
    much more actionable message (the server isn't running) instead of a
    confusing stack trace about a missing status code.
    """
