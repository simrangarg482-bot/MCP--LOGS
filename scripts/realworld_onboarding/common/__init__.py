"""Shared utilities for the real-world onboarding/SSO test harness.

Nothing in this package modifies, imports-for-mutation, or monkeypatches
anything under `app/`. Where a helper needs the real EKIP application code
(e.g. `common.bootstrap` minting a token with the project's own signing
logic, or `common.jwt_tools` verifying a token with the project's own
verifier), it imports the relevant function *read-only*, exactly the way
`scripts/seed_test_organization.py` already does. No file under `app/` is
ever written to by anything in this package.
"""
