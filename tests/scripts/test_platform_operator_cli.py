"""The platform-operator bootstrap CLI (`scripts/platform_operator.py`): safe by default, owner-only,
audited. Runs against a LOCAL migrated database as the owner role; never against anything else."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy import text

from tests.core.auth.test_password_reset_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _user,
    env,
    pw,
)
from tests.core.users.test_role_management_postgres import _engine

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "platform_operator.py"
_spec = importlib.util.spec_from_file_location("platform_operator_cli", _PATH)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)  # type: ignore[union-attr]


@pytest_asyncio.fixture(autouse=True)
async def _purge(pw):  # noqa: F811
    migrated = await pw.sup("SELECT 1 FROM pg_proc WHERE proname='platform_operator_role'")
    if not migrated:
        pytest.skip("run `alembic upgrade head` on this database")
    yield
    engine = _engine(_SUPER_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(
                text("DELETE FROM platform_audit_events WHERE details->>'target_user_id' IN (SELECT id::text FROM users WHERE email LIKE :e)"),
                {"e": f"rm-{pw.tag}-%"},
            )
    finally:
        await engine.dispose()


async def _run(argv, out):
    return await cli.run(cli.parse_args(argv), url=_SUPER_URL, out=out.append)


async def _operators(pw, name):  # noqa: F811
    return await pw.sup(
        "SELECT role, revoked_at IS NOT NULL FROM platform_operators o JOIN users u ON u.id=o.user_id WHERE u.email=:e",
        e=pw.email(name),
    )


# ---------------------------------------------------------------- argument safeguards


def test_apply_needs_yes_and_a_reason() -> None:
    for argv in (
        ["grant", "--email", "a@b.c", "--apply"],
        ["grant", "--email", "a@b.c", "--apply", "--yes"],
        ["grant", "--email", "a@b.c", "--apply", "--reason", "why"],
        ["grant", "--email", "a@b.c", "--apply", "--yes", "--reason", "   "],
        ["revoke", "--email", "a@b.c", "--apply", "--yes"],
    ):
        with pytest.raises(SystemExit):
            cli.parse_args(argv)


def test_dry_run_is_the_default_and_the_role_defaults_to_viewer() -> None:
    args = cli.parse_args(["grant", "--email", "a@b.c"])
    assert args.apply is False and args.role == "viewer"


def test_target_description_never_contains_the_password() -> None:
    text_ = cli.target_description("postgresql+asyncpg://owner:sup3rsecret@db.example:5432/ekip?sslmode=require")
    assert "db.example" in text_ and "ekip" in text_ and "sup3rsecret" not in text_


def test_the_owner_credential_is_required(monkeypatch) -> None:
    fake = type("S", (), {"migration_database_url": None})()
    monkeypatch.setattr(cli, "get_settings", lambda: fake)
    with pytest.raises(SystemExit):
        cli._owner_url()


# ---------------------------------------------------------------- behaviour


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing_and_shows_the_target(pw) -> None:  # noqa: F811
    await _user(pw, "dry")
    out: list[str] = []
    assert await _run(["grant", "--email", pw.email("dry"), "--role", "admin"], out) == 0
    text_ = "\n".join(out)
    assert "Target database: host=" in text_ and "name=" in text_ and "DRY RUN" in text_ and "Would grant" in text_
    assert "ci-only" not in text_ and "password" not in text_.lower().replace("never", "")
    assert await _operators(pw, "dry") == []


@pytest.mark.asyncio
async def test_apply_grants_audits_and_a_second_grant_is_refused(pw) -> None:  # noqa: F811
    await _user(pw, "grant")
    out: list[str] = []
    argv = ["grant", "--email", pw.email("grant"), "--role", "admin", "--apply", "--yes", "--reason", "ticket 1: on-call"]
    assert await _run(argv, out) == 0
    assert await _operators(pw, "grant") == [("admin", False)]
    audit = await pw.sup(
        "SELECT actor_user_id::text, details->>'reason', details->>'role' FROM platform_audit_events WHERE action='platform_operator.granted' AND details->>'target_user_id' = (SELECT id::text FROM users WHERE email=:e)",
        e=pw.email("grant"),
    )
    assert audit == [("00000000-0000-0000-0000-000000000000", "ticket 1: on-call", "admin")]
    again: list[str] = []
    assert await _run(argv, again) == 2 and "already has an operator record" in "\n".join(again)
    assert len(await _operators(pw, "grant")) == 1


@pytest.mark.asyncio
async def test_unknown_inactive_and_unverified_accounts_are_refused(pw) -> None:  # noqa: F811
    await _user(pw, "gone", active=False)
    await _user(pw, "unv", verified=False)
    for email, expected in (
        ("nobody@example.invalid", "no user with email"),
        (pw.email("gone"), "active and have a verified email"),
        (pw.email("unv"), "active and have a verified email"),
    ):
        out: list[str] = []
        assert await _run(["grant", "--email", email, "--apply", "--yes", "--reason", "r"], out) == 2
        assert expected in "\n".join(out)
    assert await _operators(pw, "gone") == [] and await _operators(pw, "unv") == []
    # the tool never creates an account
    assert (await pw.sup("SELECT count(*) FROM users WHERE email='nobody@example.invalid'"))[0][0] == 0


@pytest.mark.asyncio
async def test_revoke_is_a_soft_delete_audited_and_not_reversible_by_granting(pw) -> None:  # noqa: F811
    await _user(pw, "rv")
    await _run(["grant", "--email", pw.email("rv"), "--role", "viewer", "--apply", "--yes", "--reason", "r"], [])
    dry: list[str] = []
    assert await _run(["revoke", "--email", pw.email("rv")], dry) == 0
    assert await _operators(pw, "rv") == [("viewer", False)]  # dry run changed nothing
    assert await _run(["revoke", "--email", pw.email("rv"), "--apply", "--yes", "--reason", "left the team"], []) == 0
    assert await _operators(pw, "rv") == [("viewer", True)]  # row kept, marked revoked
    assert (await pw.sup("SELECT platform_operator_role(id) FROM users WHERE email=:e", e=pw.email("rv")))[0][0] is None
    out: list[str] = []
    assert await _run(["grant", "--email", pw.email("rv"), "--apply", "--yes", "--reason", "r"], out) == 2
    assert "never silently re-enabled" in "\n".join(out)
    assert await _run(["revoke", "--email", pw.email("rv"), "--apply", "--yes", "--reason", "r"], []) == 2  # nothing left to revoke
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events WHERE action='platform_operator.revoked' AND details->>'target_user_id' = (SELECT id::text FROM users WHERE email=:e)", e=pw.email("rv")))[0][0] == 1


@pytest.mark.asyncio
async def test_list_is_read_only(pw) -> None:  # noqa: F811
    await _user(pw, "lst")
    await _run(["grant", "--email", pw.email("lst"), "--apply", "--yes", "--reason", "r"], [])
    before = (await pw.sup("SELECT count(*) FROM platform_audit_events"))[0][0]
    out: list[str] = []
    assert await _run(["list"], out) == 0
    assert pw.email("lst") in "\n".join(out)
    assert (await pw.sup("SELECT count(*) FROM platform_audit_events"))[0][0] == before


@pytest.mark.asyncio
async def test_the_application_role_cannot_do_what_the_cli_does(pw) -> None:  # noqa: F811
    """The CLI needs the owner credential: the runtime role has no way to create an operator."""
    uid = await _user(pw, "noapp")
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await pw.run(None, lambda s: s.execute(text("INSERT INTO platform_operators (user_id, role) VALUES (:u,'admin')"), {"u": uid}))
