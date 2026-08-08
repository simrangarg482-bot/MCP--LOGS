"""A tiny local JSON scratch file so each script in this harness can hand
IDs (organization ids, tokens, invitation ids, role ids, ...) off to the
next one -- the same way a human operator would keep notes while clicking
through a real onboarding flow across several sessions.

Stored at scripts/realworld_onboarding/.state.json. This file holds live
access/refresh tokens once scripts have run -- it is listed in this
directory's own .gitignore and must never be committed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_STATE_PATH = Path(__file__).resolve().parent.parent / ".state.json"


def load_state() -> dict[str, Any]:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.write_text(json.dumps(state, indent=2, default=str, sort_keys=True))


def update_state(**kwargs: Any) -> dict[str, Any]:
    """Merge `kwargs` into the persisted state and write it back. Nested
    dict values (e.g. state["users"]["admin"] = {...}) are merged shallowly
    by the caller before calling this -- this function itself does a plain
    top-level dict.update.
    """
    state = load_state()
    state.update(kwargs)
    save_state(state)
    return state


def clear_state() -> None:
    if _STATE_PATH.exists():
        _STATE_PATH.unlink()


def state_path() -> Path:
    return _STATE_PATH
