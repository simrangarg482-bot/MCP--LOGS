"""99 -- Master end-to-end orchestrator.

PURPOSE
    Run the complete real-world onboarding simulation in one command:

        Organization Registration (01)
              |
        Create Organization Administrator (02)
              |
        Configure SSO (03)
              |
        Create Test Users (04)
              |
        Real SSO Login, if configured -- otherwise a documented skip (05)
              |
        Verify Access Token (06)
              |
        Permission Tests (07)
              |
        Multi-Tenant Isolation Tests (08)
              |
        Negative Tests (09)
              |
        Logout Tests (10)
              |
        Cleanup (best-effort)

WHY THIS DOESN'T JUST SHELL OUT TO `python 01_register_org.py` ELEVEN TIMES
    Each numbered script is importable on its own (guarded by
    `if __name__ == "__main__":`) but its filename starts with a digit,
    which Python's `import` statement can't reference directly -- this
    orchestrator loads each one by file path via `importlib.util` instead,
    and calls its `main()` function directly, so every script's own
    request/response logging and this orchestrator's combined final
    summary land in one continuous, chronological console transcript.

STOP-ON-FAILURE
    By default, this orchestrator stops at the first stage whose `main()`
    returns False (matching the task's "The script should stop on
    failures" requirement). Pass --continue-on-failure to run every stage
    regardless and see the complete picture at once (useful when
    diagnosing several independent issues in one pass).

HOW TO EXECUTE
    python scripts/realworld_onboarding/99_master_e2e.py
    python scripts/realworld_onboarding/99_master_e2e.py --continue-on-failure
    python scripts/realworld_onboarding/99_master_e2e.py --skip-cleanup

EXPECTED SUCCESS OUTPUT
    Every stage's own PASS/FAIL lines, followed by one final combined
    summary table (see common/logger.py) covering every check across
    every stage that ran.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HARNESS_DIR))

from common.logger import print_summary, reset_results  # noqa: E402

_STAGES = [
    ("01_register_org.py", "Organization Registration"),
    ("02_bootstrap_org_admin.py", "Create Organization Administrator"),
    ("03_configure_sso.py", "Configure SSO"),
    ("04_create_test_users.py", "Create Test Users"),
    ("05_login_flow.py", "Real SSO Login (or documented skip)"),
    ("06_verify_token.py", "Verify Access Token"),
    ("07_permission_tests.py", "Permission Tests"),
    ("08_isolation_tests.py", "Multi-Tenant Isolation Tests"),
    ("09_negative_tests.py", "Negative Tests"),
    ("10_logout_tests.py", "Logout Tests"),
]


def _load_main(filename: str):
    module_name = filename.replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, _HARNESS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.main


def main() -> bool:
    continue_on_failure = "--continue-on-failure" in sys.argv
    skip_cleanup = "--skip-cleanup" in sys.argv

    reset_results()
    overall_ok = True

    for filename, label in _STAGES:
        print("\n" + "#" * 72)
        print(f"# STAGE: {label}  ({filename})")
        print("#" * 72)
        try:
            stage_main = _load_main(filename)
            stage_ok = stage_main()
        except Exception as exc:  # noqa: BLE001 - a stage crashing is itself a result to report
            print(f"\n!!! Stage {label} raised an unhandled exception: {type(exc).__name__}: {exc}")
            stage_ok = False

        overall_ok = overall_ok and stage_ok
        if not stage_ok and not continue_on_failure:
            print(f"\nStopping: stage '{label}' failed and --continue-on-failure was not passed.")
            break

    if not skip_cleanup:
        print("\n" + "#" * 72)
        print("# CLEANUP (best-effort -- see cleanup.py's own docstring for what it can/can't do)")
        print("#" * 72)
        try:
            cleanup_main = _load_main("cleanup.py")
            cleanup_main()
        except Exception as exc:  # noqa: BLE001
            print(f"Cleanup raised an unhandled exception (non-fatal to the overall result): {exc}")

    all_passed = print_summary(title="EKIP REAL-WORLD ONBOARDING -- FULL END-TO-END SUMMARY")

    # Best-effort only: dispose of the shared DB engine/event loop this run
    # accumulated across stages, so the process exits quietly instead of
    # printing spurious asyncpg/SQLAlchemy shutdown warnings after the real
    # results above are already final. See common/bootstrap.py's
    # dispose_shared_loop() docstring for why this exists.
    try:
        from common.bootstrap import dispose_shared_loop

        dispose_shared_loop()
    except Exception as exc:  # noqa: BLE001 - cosmetic only, never affects the result above
        print(f"(non-fatal) cleanup of shared DB event loop raised: {exc}")

    return overall_ok and all_passed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
