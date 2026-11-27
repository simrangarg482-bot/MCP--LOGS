"""Best-effort secret redaction for free text that is about to be stored.

Feedback comments and the question text copied into evaluation candidates are
typed by people, and people paste things: a stack trace with a connection
string, a curl command with a bearer token. Those must never be written to a
feedback row or an exported candidate file.

HONEST SCOPE: this is a pattern-based filter for well-known, high-precision
secret formats. It is not general data-loss prevention -- it will not catch a
password that merely looks like a word, and it does not detect personal data.
It errs toward redacting (a false positive costs a `[REDACTED:...]` marker in
a comment; a false negative stores a credential).

The replacement carries the secret's KIND only, never any part of its value.
"""

from __future__ import annotations

import re

# Order matters: more specific shapes first, the generic `key=value` rule last
# so it never eats a token an earlier rule would have labelled precisely.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # scheme://user:password@host -- keep the scheme and host so the text
    # stays readable, drop the credential.
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)"
            r"(\s*[:=]\s*)(?!\[REDACTED)(\"[^\"\r\n]{4,}\"|'[^'\r\n]{4,}'|[^\s\"',;]{6,})"
        ),
    ),
)


def redact_secrets(text: str) -> tuple[str, int]:
    """Return `(redacted_text, number_of_redactions)`."""
    count = 0
    result = text
    for kind, pattern in _RULES:
        if kind == "url_credentials":
            # Group 1 = the scheme (`postgresql://`), kept so the text stays readable.
            result, n = pattern.subn(r"\g<1>[REDACTED:url_credentials]@", result)
        elif kind == "credential_assignment":
            # Groups 1 and 2 = the key name and its `=`/`:` separator, both kept.
            result, n = pattern.subn(r"\g<1>\g<2>[REDACTED:credential_assignment]", result)
        else:
            result, n = pattern.subn(f"[REDACTED:{kind}]", result)
        count += n
    return result, count
