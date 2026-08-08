"""Console step-logging + the final colored PASS/FAIL summary table.

Every script in this harness creates one `StepLogger` per logical stage and
calls `.step()` / `.request()` / `.response()` / `.info()` along the way,
finishing with exactly one `.passed()` or `.failed()` call. `print_summary()`
(called once, at the very end of `99_master_e2e.py`, or at the end of any
single script run standalone) renders the table the task asked for:

    Organization Creation      PASS
    Configure SSO             PASS
    ...
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

_RESET = "\033[0m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"

_SECRET_KEY_HINTS = ("secret", "password", "token", "credential", "code_verifier")


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if _supports_color() else text


# Process-wide list of (stage_name, passed, detail) -- one master log shared
# by every script imported in the same process (this is what lets
# 99_master_e2e.py print one combined summary across every stage it runs).
_RESULTS: list[tuple[str, bool, str]] = []


def record_result(stage: str, passed: bool, detail: str = "") -> None:
    _RESULTS.append((stage, passed, detail))


def reset_results() -> None:
    _RESULTS.clear()


def print_summary(title: str = "EKIP REAL-WORLD ONBOARDING -- SUMMARY") -> bool:
    """Print the final colored summary table.

    Returns True iff every recorded stage passed -- callers (in particular
    `99_master_e2e.py`) use this as their process exit code so CI can key
    off of it.
    """
    print()
    print(_c("=" * 64, _CYAN))
    print(_c(title, _BOLD))
    print(_c("=" * 64, _CYAN))
    all_passed = True
    for stage, passed, detail in _RESULTS:
        label = _c("PASS", _GREEN) if passed else _c("FAIL", _RED)
        line = f"{stage:<44} {label}"
        if detail and not passed:
            line += f"   ({detail})"
        print(line)
        all_passed = all_passed and passed
    print(_c("=" * 64, _CYAN))
    total = len(_RESULTS)
    passed_count = sum(1 for _, p, _ in _RESULTS if p)
    print(f"{passed_count}/{total} checks passed.")
    print()
    return all_passed


def _redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if isinstance(key, str) and any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(payload, list):
        return [_redact(item) for item in payload]
    return payload


def _truncate(text: str, limit: int = 900) -> str:
    return text if len(text) <= limit else text[:limit] + " ...(truncated)"


@dataclass
class StepLogger:
    """One instance per logical test stage (e.g. "Register Organization").
    Multiple `.step()` calls are fine within one stage (a stage is often
    several HTTP calls); exactly one `.passed()`/`.failed()` call should
    close it out.
    """

    stage_name: str
    _start: float = field(default_factory=time.monotonic, init=False)
    _closed: bool = field(default=False, init=False)

    def step(self, message: str) -> None:
        print(f"\n{_c('>>> STEP', _CYAN)} [{self.stage_name}] {message}")

    def request(self, method: str, url: str, payload: dict | None = None) -> None:
        print(f"    {_c('REQUEST', _YELLOW)} {method} {url}")
        if payload is not None:
            print(f"    {_c('PAYLOAD', _YELLOW)} {json.dumps(_redact(payload), default=str)}")

    def response(self, status_code: int, body: Any, elapsed_ms: float) -> None:
        color = _GREEN if 200 <= status_code < 300 else _YELLOW
        print(f"    {_c('STATUS', color)} {status_code}    {_c('ELAPSED', _CYAN)} {elapsed_ms:.0f}ms")
        body_str = body if isinstance(body, str) else json.dumps(_redact(body), default=str)
        print(f"    {_c('BODY', color)} {_truncate(body_str)}")

    def info(self, message: str) -> None:
        print(f"    {_c('INFO', _CYAN)} {message}")

    def warn(self, message: str) -> None:
        print(f"    {_c('WARN', _YELLOW)} {message}")

    def passed(self, detail: str = "") -> None:
        if self._closed:
            return
        elapsed = time.monotonic() - self._start
        print(f"    {_c('RESULT: PASS', _GREEN)} ({elapsed:.2f}s) {detail}")
        record_result(self.stage_name, True, detail)
        self._closed = True

    def failed(self, reason: str) -> None:
        if self._closed:
            return
        elapsed = time.monotonic() - self._start
        print(f"    {_c('RESULT: FAIL', _RED)} ({elapsed:.2f}s) {reason}")
        record_result(self.stage_name, False, reason)
        self._closed = True

    def skipped(self, reason: str) -> None:
        """Neither pass nor fail -- used for scenarios this harness cannot
        exercise in the current environment (most commonly: no real IdP
        credentials configured). Recorded distinctly in the summary so a
        skip is never silently confused with a pass.
        """
        print(f"    {_c('RESULT: SKIP', _YELLOW)} {reason}")
        record_result(f"{self.stage_name} (SKIPPED)", True, reason)
        self._closed = True
