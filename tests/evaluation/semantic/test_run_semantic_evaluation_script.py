"""Tests for `scripts/run_semantic_evaluation.py` -- section 15's "missing
credentials fails loudly" / "live mode can't silently fall back" and
section 6's "never silently downgrades" requirements.

The credential-failure behavior is verified two ways, deliberately:
  1. At the real process level (`test_missing_credentials_...`), the same
     way `tests/evaluation/test_ci_gate.py` verifies its own CI gate --
     only a genuine subprocess run proves the actual exit code CI depends
     on; a `main()`-return-value check could pass while `SystemExit` never
     actually fired.
  2. As a plain unit test of `_require_live_model` itself, for the exact
     branch (blank key vs. `Settings` failing to load) and message.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_semantic_evaluation.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "run_semantic_evaluation_under_test", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


# --------------------------------------------------------------------------
# real process: the exit code CI would actually observe
# --------------------------------------------------------------------------


def test_missing_credentials_fails_loudly_with_nonzero_exit_and_no_silent_downgrade(tmp_path):
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = ""

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--report-path",
            str(tmp_path / "report.json"),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode != 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "BENCHMARK EXECUTION FAILED" in result.stdout
    assert "OPENAI_API_KEY" in result.stdout
    # No report should be written when the run never actually executed --
    # a written report here would look like a completed (if empty) benchmark.
    assert not (tmp_path / "report.json").exists()


def test_help_exits_zero_and_documents_repository_derived_flag():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--repository-derived" in result.stdout


# --------------------------------------------------------------------------
# unit-level: the exact branch and message of `_require_live_model`
# --------------------------------------------------------------------------


def test_require_live_model_raises_when_api_key_blank(monkeypatch, script_module):
    class _FakeSettings:
        openai_api_key = ""
        agent_llm_model = "gpt-4o-mini"

    monkeypatch.setattr(script_module, "get_settings", lambda: _FakeSettings())
    with pytest.raises(script_module.CredentialsUnavailableError, match="OPENAI_API_KEY"):
        script_module._require_live_model()


def test_require_live_model_raises_when_settings_fail_to_load(monkeypatch, script_module):
    def _raise():
        raise RuntimeError("no .env configured")

    monkeypatch.setattr(script_module, "get_settings", _raise)
    with pytest.raises(script_module.CredentialsUnavailableError, match="could not load Settings"):
        script_module._require_live_model()


def test_require_live_model_returns_provider_and_model_when_key_present(monkeypatch, script_module):
    class _FakeSettings:
        openai_api_key = "sk-fake-test-key"
        agent_llm_model = "gpt-4o-mini"

    monkeypatch.setattr(script_module, "get_settings", lambda: _FakeSettings())
    provider, model = script_module._require_live_model()
    assert provider == "openai"
    assert model == "gpt-4o-mini"


def test_get_settings_value_reads_the_named_attribute(monkeypatch, script_module):
    class _FakeSettings:
        memory_relevance_threshold = 0.35

    monkeypatch.setattr(script_module, "get_settings", lambda: _FakeSettings())
    assert script_module.get_settings_value("memory_relevance_threshold") == 0.35


def test_git_commit_swallows_failures_and_returns_none(monkeypatch, script_module):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(script_module.subprocess, "run", _raise)
    assert script_module._git_commit() is None


def test_git_commit_returns_a_short_hash_in_this_real_repository(script_module):
    commit = script_module._git_commit()
    assert commit is None or (isinstance(commit, str) and 0 < len(commit) <= 40)
