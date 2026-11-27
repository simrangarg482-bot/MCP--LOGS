"""Password reset, change password, session cut-off and organization suspension against REAL
PostgreSQL as the real `ekip_app` role (row-level security enforced, `refresh_tokens` FORCE RLS).

Skipped unless EKIP_TEST_POSTGRES_URL / EKIP_TEST_APP_URL point at a LOCAL database migrated to head
(same contract as the other `*_postgres` tests). Emails go to a recording fake, never over SMTP.
Every row is tagged and removed with targeted DELETEs.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.auth import passwords, password_reset
from app.core.auth import service as auth_service
from app.core.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    OrganizationSelectionRequest,
    OrganizationSelectionRequired,
    RefreshRequest,
    SessionTokens,
    SwitchOrganizationRequest,
)
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.users import service as users_service
from app.shared.config.settings import get_settings
from app.shared.email import EmailMessage
from app.shared.security import hash_opaque_token
from tests.core.users.test_role_management_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _is_local,
    _make_org,
    _role_id,
    env,
)

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

OLD_PW = "old password for the tests"
NEW_PW = "brand new password for tests"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


@pytest_asyncio.fixture
async def pw(env, monkeypatch):  # noqa: F811
    migrated = await env.sup("SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='sessions_valid_after'")
    if not migrated:
        pytest.skip("users.sessions_valid_after missing: run `alembic upgrade head` on this database")
    settings = get_settings().model_copy(update={"frontend_public_base_url": "https://app.example.test"})
    monkeypatch.setattr(password_reset, "get_settings", lambda: settings)
    env.settings = settings
    yield env
    s = f"rm-{env.tag}-%"
    await env.sup("DELETE FROM refresh_tokens WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s) OR user_id IN (SELECT id FROM users WHERE email LIKE :e)", s=s, e=s)
    await env.sup("DELETE FROM password_resets WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)", e=s)


# ------------------------------------------------------------------ helpers


async def _org(env, name):
    return await _make_org(env, name)


async def _user(env, name, *orgs, password=OLD_PW, verified=True, active=True, password_hash=None):
    """A user holding the viewer role in each of `orgs`. `password=None` -> SSO-only (no hash)."""

    async def make(s):
        h = password_hash if password_hash is not None else (passwords.hash_password(password) if password else None)
        return await users_service.create_verified_user(
            s, email=env.email(name), display_name=name, password_hash=h, via="email_link"
        )

    uid = await env.run(None, make)
    for org in orgs:
        rid = await _role_id(env, org, "viewer")
        await env.run(org, lambda s, org=org, rid=rid: users_service.assign_role(s, user_id=uid, organization_id=org, role_id=rid))
    if not verified:
        await env.sup("UPDATE users SET email_verified_at = NULL WHERE id=:u", u=uid)
    if not active:
        await env.sup("UPDATE users SET is_active = false WHERE id=:u", u=uid)
    return uid


async def _request(env, name):
    return await env.run(None, lambda s: password_reset.request_reset(s, email=env.email(name)))


async def _reset(env, token, password=NEW_PW):
    return await env.run(None, lambda s: password_reset.reset_password(s, token=token, new_password=password))


async def _session(env, uid, org):
    return await env.run(
        org, lambda s: auth_service._issue_session(s, user_id=uid, organization_id=org, family_id=uuid.uuid4())
    )


async def _login(env, name, password):
    return await env.run(None, lambda s: auth_service.login_with_password(s, LoginRequest(email=env.email(name), password=password)))


async def _identity(env, tokens, org=None):
    claims = auth_service.verify_access_token(tokens.access_token)
    return await env.run(
        claims.organization_id,
        lambda s: users_service.resolve_identity(
            s,
            claims.user_id,
            claims.organization_id,
            token_issued_at=claims.issued_at,
            token_org_epoch=claims.org_session_epoch,
            token_member_epoch=claims.member_epoch,
        ),
    )


async def _expect(coro, error_type, code):
    with pytest.raises(error_type) as info:
        await coro
    assert info.value.error_code == code, (info.value.error_code, info.value.message)
    return info.value


async def _hash_of(env, uid):
    return (await env.sup("SELECT password_hash FROM users WHERE id=:u", u=uid))[0][0]


async def _audit(env, org, action):
    rows = await env.sup("SELECT metadata FROM audit_logs WHERE organization_id=:o AND action=:a", o=org, a=action)
    return [r[0] for r in rows]


async def _end_cooldown(env, uid):
    await env.sup("UPDATE password_resets SET last_sent_at = now() - interval '2 hours' WHERE user_id=:u AND consumed_at IS NULL", u=uid)


# ============================================================ requesting a reset


@pytest.mark.asyncio
async def test_a_request_stores_only_a_hash_with_a_short_life_and_audits_each_organization(pw) -> None:
    a, b = await _org(pw, "ra"), await _org(pw, "rb")
    uid = await _user(pw, "multi", a, b)

    delivery = await _request(pw, "multi")

    assert delivery is not None and len(delivery.raw_token) >= 40
    row = (await pw.sup("SELECT token_hash, expires_at - created_at, consumed_at FROM password_resets WHERE user_id=:u", u=uid))[0]
    assert row[0] == hash_opaque_token(delivery.raw_token) and delivery.raw_token not in row[0]
    assert timedelta(minutes=29) < row[1] <= timedelta(minutes=31) and row[2] is None
    assert "raw_token" not in repr(delivery) and delivery.raw_token not in repr(delivery)
    for org in (a, b):  # one audit row per organization the person belongs to, never the token
        events = await _audit(pw, org, "user.password_reset_requested")
        assert len(events) == 1 and delivery.raw_token not in str(events)


@pytest.mark.asyncio
async def test_ineligible_accounts_get_nothing_and_nothing_is_stored(pw) -> None:
    org = await _org(pw, "inel")
    await _user(pw, "sso", org, password=None)
    await _user(pw, "dead", org, active=False)
    await _user(pw, "unver", org, verified=False)

    for name in ("sso", "dead", "unver", "nobody-at-all"):
        assert await _request(pw, name) is None
    for bad in ("", "   ", "not-an-email", "a@b", "x" * 400 + "@y.test"):
        assert await pw.run(None, lambda s, bad=bad: password_reset.request_reset(s, email=bad)) is None
    count = (await pw.sup("SELECT count(*) FROM password_resets pr JOIN users u ON u.id=pr.user_id WHERE u.email LIKE :e", e=f"rm-{pw.tag}-%"))[0][0]
    assert count == 0


@pytest.mark.asyncio
async def test_the_address_is_matched_case_insensitively_and_trimmed(pw) -> None:
    org = await _org(pw, "case")
    await _user(pw, "mixed", org)
    delivery = await pw.run(None, lambda s: password_reset.request_reset(s, email=f"  {pw.email('mixed').upper()} "))
    assert delivery is not None


@pytest.mark.asyncio
async def test_resends_are_throttled_in_the_database_and_rotate_the_link(pw) -> None:
    org = await _org(pw, "thr")
    uid = await _user(pw, "thr", org)
    first = await _request(pw, "thr")
    assert first is not None
    assert await _request(pw, "thr") is None  # inside the cooldown
    await _end_cooldown(pw, uid)
    second = await _request(pw, "thr")
    assert second is not None and second.raw_token != first.raw_token
    await _expect(_reset(pw, first.raw_token), ValidationError, "auth.reset_token_invalid")  # the earlier link died
    assert (await pw.sup("SELECT count(*) FROM password_resets WHERE user_id=:u AND consumed_at IS NULL", u=uid))[0][0] == 1  # still one open link

    # daily cap
    await pw.sup("UPDATE password_resets SET send_count = :n, send_window_started_at = now() WHERE user_id=:u AND consumed_at IS NULL", u=uid, n=pw.settings.password_reset_max_sends_per_day)
    await _end_cooldown(pw, uid)
    assert await _request(pw, "thr") is None


# ============================================================ completing a reset


@pytest.mark.asyncio
async def test_reset_sets_an_argon2id_password_consumes_the_link_and_old_password_stops_working(pw) -> None:
    org = await _org(pw, "ok")
    uid = await _user(pw, "ok", org)
    delivery = await _request(pw, "ok")

    notice = await _reset(pw, delivery.raw_token)

    assert notice.how == "reset" and notice.email == pw.email("ok")
    stored = await _hash_of(pw, uid)
    assert stored.startswith("$argon2id$") and passwords.verify_password(NEW_PW, stored)
    await _expect(_login(pw, "ok", OLD_PW), PermissionDeniedError, "auth.invalid_credentials")
    assert isinstance(await _login(pw, "ok", NEW_PW), SessionTokens)
    assert (await pw.sup("SELECT consumed_at IS NOT NULL FROM password_resets WHERE user_id=:u", u=uid))[0][0] is True
    assert (await _audit(pw, org, "user.password_reset_completed"))[0]["refresh_tokens_revoked"] == 0


@pytest.mark.asyncio
async def test_reset_links_are_single_use_expire_and_unknown_tokens_are_refused(pw) -> None:
    org = await _org(pw, "tok")
    uid = await _user(pw, "tok", org)
    delivery = await _request(pw, "tok")
    await _reset(pw, delivery.raw_token)

    await _expect(_reset(pw, delivery.raw_token), ConflictError, "auth.reset_token_used")
    await _expect(_reset(pw, "totally-made-up-token"), ValidationError, "auth.reset_token_invalid")
    await _expect(_reset(pw, "   "), ValidationError, "auth.reset_token_invalid")

    await _end_cooldown(pw, uid)
    again = await _request(pw, "tok")
    await pw.sup("UPDATE password_resets SET expires_at = now() - interval '1 minute' WHERE user_id=:u AND consumed_at IS NULL", u=uid)
    await _expect(_reset(pw, again.raw_token), ValidationError, "auth.reset_token_expired")


@pytest.mark.asyncio
async def test_a_weak_password_does_not_burn_the_link(pw) -> None:
    org = await _org(pw, "weak")
    uid = await _user(pw, "weak", org)
    delivery = await _request(pw, "weak")

    await _expect(_reset(pw, delivery.raw_token, "short"), ValidationError, "auth.password_too_short")
    await _expect(_reset(pw, delivery.raw_token, "x" * 129), ValidationError, "auth.password_too_long")
    await _expect(_reset(pw, delivery.raw_token, pw.email("weak")), ValidationError, "auth.password_same_as_email")  # checked after consume, rolled back

    assert (await pw.sup("SELECT consumed_at IS NULL FROM password_resets WHERE user_id=:u", u=uid))[0][0] is True
    assert passwords.verify_password(OLD_PW, await _hash_of(pw, uid))  # nothing changed
    await _reset(pw, delivery.raw_token)  # the same link still works with a good password


@pytest.mark.asyncio
async def test_concurrent_resets_with_one_token_have_exactly_one_winner(pw) -> None:
    org = await _org(pw, "race")
    uid = await _user(pw, "race", org)
    delivery = await _request(pw, "race")

    results = await asyncio.gather(
        _reset(pw, delivery.raw_token, "winner password number one"),
        _reset(pw, delivery.raw_token, "winner password number two"),
        _reset(pw, delivery.raw_token, "winner password number 333"),
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, password_reset.PendingPasswordNotice)]
    losers = [r for r in results if isinstance(r, (ConflictError, ValidationError))]
    assert len(winners) == 1 and len(losers) == 2
    final = await _hash_of(pw, uid)
    assert sum(passwords.verify_password(p, final) for p in ("winner password number one", "winner password number two", "winner password number 333")) == 1


@pytest.mark.asyncio
async def test_reset_never_gives_a_password_to_sso_only_or_verifies_or_revives_accounts(pw) -> None:
    """Even a link that somehow exists (planted directly) cannot create a credential, verify an
    address, or wake an inactive account -- and the failure leaves everything untouched."""
    org = await _org(pw, "bad")
    sso = await _user(pw, "sso2", org, password=None)
    unver = await _user(pw, "unver2", org, verified=False)
    dead = await _user(pw, "dead2", org, active=False)
    for uid in (sso, unver, dead):
        token = f"planted-{uid}"
        await pw.sup(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (:u, :h, now() + interval '10 minutes')",
            u=uid, h=hash_opaque_token(token),
        )
        await _expect(_reset(pw, token), ValidationError, "auth.reset_token_invalid")
        assert (await pw.sup("SELECT consumed_at IS NULL FROM password_resets WHERE user_id=:u", u=uid))[0][0] is True  # rolled back
    assert (await _hash_of(pw, sso)) is None
    assert (await pw.sup("SELECT email_verified_at IS NULL FROM users WHERE id=:u", u=unver))[0][0] is True
    assert (await pw.sup("SELECT is_active FROM users WHERE id=:u", u=dead))[0][0] is False


# ============================================================ sessions end everywhere


@pytest.mark.asyncio
async def test_reset_ends_every_session_in_every_organization_including_a_former_one(pw) -> None:
    a, b, gone = await _org(pw, "sa"), await _org(pw, "sb"), await _org(pw, "sg")
    uid = await _user(pw, "sess", a, b, gone)
    other = await _user(pw, "bystander", a)
    sessions = {org: await _session(pw, uid, org) for org in (a, b, gone)}
    bystander = await _session(pw, other, a)
    # the person is removed from `gone` but their refresh token there still exists (FORCE RLS hides it from tenant queries)
    await pw.sup("DELETE FROM user_roles WHERE user_id=:u AND organization_id=:o", u=uid, o=gone)

    delivery = await _request(pw, "sess")
    await _reset(pw, delivery.raw_token)

    live = (await pw.sup("SELECT count(*) FROM refresh_tokens WHERE user_id=:u AND revoked_at IS NULL", u=uid))[0][0]
    assert live == 0
    for org, tokens in sessions.items():
        await _expect(
            pw.run(None, lambda s, t=tokens: auth_service.refresh(s, RefreshRequest(refresh_token=t.refresh_token))),
            PermissionDeniedError,
            "auth.refresh_token_reused",
        )
        if org != gone:
            await _expect(_identity(pw, tokens), PermissionDeniedError, "auth.session_revoked")  # old access token dead at once
    # another user is completely unaffected
    assert (await _identity(pw, bystander)).user_id == other
    rotated = await pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=bystander.refresh_token)))
    assert rotated.access_token
    assert (await pw.sup("SELECT sessions_valid_after IS NULL FROM users WHERE id=:u", u=other))[0][0] is True


@pytest.mark.asyncio
async def test_session_cutoff_boundaries_at_microsecond_precision(pw) -> None:
    org = await _org(pw, "prec")
    uid = await _user(pw, "prec", org)
    cutoff = datetime(2026, 11, 26, 12, 0, 0, 700_000, tzinfo=UTC)  # mid-second on purpose
    await pw.sup("UPDATE users SET sessions_valid_after=:c WHERE id=:u", c=cutoff, u=uid)

    async def resolve(issued_at):
        return await pw.run(org, lambda s: users_service.resolve_identity(s, uid, org, token_issued_at=issued_at))

    one_us = timedelta(microseconds=1)
    await _expect(resolve(cutoff - one_us), PermissionDeniedError, "auth.session_revoked")  # 1 us before: refused
    assert (await resolve(cutoff)).user_id == uid  # at the instant: accepted
    assert (await resolve(cutoff + one_us)).user_id == uid
    # a token minted before `iat_us` existed knows only its WHOLE second: it is treated as issued at
    # the START of that second, so one from the cut-off's own second is refused (never accepted).
    await _expect(resolve(datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)), PermissionDeniedError, "auth.session_revoked")
    assert (await resolve(datetime(2026, 11, 26, 12, 0, 1, tzinfo=UTC))).user_id == uid
    assert (await resolve(None)).user_id == uid  # callers that carry no token are unaffected
    # a user who never reset is never refused
    other = await _user(pw, "never", org)
    assert (await pw.run(org, lambda s: users_service.resolve_identity(s, other, org, token_issued_at=datetime(2000, 1, 1, tzinfo=UTC)))).user_id == other


@pytest.mark.asyncio
async def test_tokens_issued_right_around_a_reset_land_on_the_correct_side(pw) -> None:
    """Real tokens minted back-to-back with the reset (often within the same wall-clock second):
    the one issued before is refused, the one issued after is accepted -- repeatedly."""
    org = await _org(pw, "edge")
    uid = await _user(pw, "edge", org)
    for round_ in range(4):
        before = await _session(pw, uid, org)
        await pw.sup("DELETE FROM password_resets WHERE user_id=:u", u=uid)
        delivery = await _request(pw, "edge")
        await _reset(pw, delivery.raw_token, f"edge case password number {round_}")
        after = await _session(pw, uid, org)
        await _expect(_identity(pw, before), PermissionDeniedError, "auth.session_revoked")
        assert (await _identity(pw, after)).user_id == uid
        claims = auth_service.verify_access_token(after.access_token)
        assert claims.issued_at.microsecond is not None  # carries the precise claim


# ============================================================ change password


@pytest.mark.asyncio
async def test_change_password_keeps_the_caller_signed_in_and_ends_everything_else(pw) -> None:
    a, b = await _org(pw, "ca"), await _org(pw, "cb")
    uid = await _user(pw, "chg", a, b)
    current = await _session(pw, uid, a)
    elsewhere = await _session(pw, uid, b)
    laptop = await _session(pw, uid, a)

    tokens, notice = await pw.run(
        a,
        lambda s: auth_service.change_password(
            s, user_id=uid, organization_id=a, data=ChangePasswordRequest(current_password=OLD_PW, new_password=NEW_PW)
        ),
    )

    assert notice.how == "change"
    assert (await _identity(pw, tokens)).organization_id == a  # the returned session works
    for stale in (current, elsewhere, laptop):
        await _expect(_identity(pw, stale), PermissionDeniedError, "auth.session_revoked")
        await _expect(
            pw.run(None, lambda s, t=stale: auth_service.refresh(s, RefreshRequest(refresh_token=t.refresh_token))),
            PermissionDeniedError,
            "auth.refresh_token_reused",
        )
    assert (await pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=tokens.refresh_token)))).access_token
    assert passwords.verify_password(NEW_PW, await _hash_of(pw, uid))
    await _expect(_login(pw, "chg", OLD_PW), PermissionDeniedError, "auth.invalid_credentials")
    assert len(await _audit(pw, a, "user.password_changed")) == 1 and len(await _audit(pw, b, "user.password_changed")) == 1


@pytest.mark.asyncio
async def test_change_password_checks_the_current_password_policy_and_account_type(pw) -> None:
    org = await _org(pw, "chk")
    uid = await _user(pw, "chk", org)
    sso = await _user(pw, "chksso", org, password=None)

    def change(user, current, new):
        return pw.run(org, lambda s: auth_service.change_password(s, user_id=user, organization_id=org, data=ChangePasswordRequest(current_password=current, new_password=new)))

    await _expect(change(uid, "wrong current password", NEW_PW), PermissionDeniedError, "auth.current_password_incorrect")
    await _expect(change(uid, OLD_PW, "short"), ValidationError, "auth.password_too_short")
    await _expect(change(uid, OLD_PW, OLD_PW), ValidationError, "auth.password_unchanged")
    await _expect(change(uid, OLD_PW, pw.email("chk")), ValidationError, "auth.password_same_as_email")
    await _expect(change(sso, "anything at all here", NEW_PW), ValidationError, "auth.no_password")
    assert passwords.verify_password(OLD_PW, await _hash_of(pw, uid))  # nothing changed
    assert (await pw.sup("SELECT sessions_valid_after IS NULL FROM users WHERE id=:u", u=uid))[0][0] is True
    assert (await _hash_of(pw, sso)) is None


@pytest.mark.asyncio
async def test_a_failed_change_attempt_is_audited_but_does_not_end_any_session(pw) -> None:
    """The failure audit row is written in the request's transaction, which the error then rolls back
    -- so the failed attempt is visible in the application log, and (importantly) it never ends a session."""
    org = await _org(pw, "fail")
    uid = await _user(pw, "fail", org)
    session = await _session(pw, uid, org)
    with pytest.raises(PermissionDeniedError):
        await pw.run(org, lambda s: auth_service.change_password(s, user_id=uid, organization_id=org, data=ChangePasswordRequest(current_password="nope nope nope nope", new_password=NEW_PW)))
    assert (await _identity(pw, session)).user_id == uid


# ============================================================ legacy hashes


@pytest.mark.asyncio
async def test_a_bcrypt_account_still_logs_in_and_is_upgraded_without_ending_sessions(pw) -> None:
    org = await _org(pw, "bc")
    legacy = bcrypt.hashpw(OLD_PW.encode(), bcrypt.gensalt(rounds=4)).decode()
    uid = await _user(pw, "bc", org, password_hash=legacy)
    before = await _session(pw, uid, org)

    first = await _login(pw, "bc", OLD_PW)

    assert isinstance(first, SessionTokens)
    stored = await _hash_of(pw, uid)
    assert stored.startswith("$argon2id$") and passwords.verify_password(OLD_PW, stored)
    assert (await pw.sup("SELECT sessions_valid_after IS NULL FROM users WHERE id=:u", u=uid))[0][0] is True
    assert (await _identity(pw, before)).user_id == uid  # the upgrade did not log anyone out
    assert isinstance(await _login(pw, "bc", OLD_PW), SessionTokens)  # and it keeps working
    await _expect(_login(pw, "bc", "the wrong password!!"), PermissionDeniedError, "auth.invalid_credentials")


@pytest.mark.asyncio
async def test_a_stale_upgrade_never_overwrites_a_newer_password(pw) -> None:
    org = await _org(pw, "stale")
    legacy = bcrypt.hashpw(OLD_PW.encode(), bcrypt.gensalt(rounds=4)).decode()
    uid = await _user(pw, "stale", org, password_hash=legacy)
    delivery = await _request(pw, "stale")
    await _reset(pw, delivery.raw_token)  # a reset lands first
    reset_hash = await _hash_of(pw, uid)

    applied = await pw.run(None, lambda s: users_service.upgrade_password_hash(s, user_id=uid, old_hash=legacy, new_hash=passwords.hash_password(OLD_PW)))

    assert applied is False and await _hash_of(pw, uid) == reset_hash


# ============================================================ suspended organizations


@pytest.mark.asyncio
async def test_suspension_blocks_only_the_suspended_organization(pw) -> None:
    live, dead = await _org(pw, "live"), await _org(pw, "dead")
    uid = await _user(pw, "susp", live, dead)
    earlier_dead = await _session(pw, uid, dead)
    earlier_live = await _session(pw, uid, live)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=dead)

    # password login goes straight into the active organization (no picker offering the dead one)
    result = await _login(pw, "susp", OLD_PW)
    assert isinstance(result, SessionTokens)
    assert (await _identity(pw, result)).organization_id == live

    # every other way into the suspended organization is refused ...
    await _expect(
        pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=earlier_dead.refresh_token))),
        PermissionDeniedError,
        "auth.organization_suspended",
    )
    await _expect(
        pw.run(None, lambda s: auth_service.switch_organization(s, user_id=uid, data=SwitchOrganizationRequest(organization_id=dead))),
        PermissionDeniedError,
        "auth.organization_suspended",
    )
    # ... while the same person's session in the other organization keeps working
    assert (await pw.run(None, lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=earlier_live.refresh_token)))).access_token
    listed = await pw.run(None, lambda s: auth_service.list_available_organizations(s, uid))
    assert {o.id for o in listed.organizations} == {live}

    # every organization suspended: a specific error, no session
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=live)
    await _expect(_login(pw, "susp", OLD_PW), PermissionDeniedError, "auth.organization_suspended")
    # lifting the suspension restores access
    await pw.sup("UPDATE organizations SET status='active' WHERE id IN (:a, :b)", a=live, b=dead)
    assert isinstance(await _login(pw, "susp", OLD_PW), OrganizationSelectionRequired)


@pytest.mark.asyncio
async def test_selecting_a_suspended_organization_from_a_stale_picker_is_refused(pw) -> None:
    a, b = await _org(pw, "pa"), await _org(pw, "pb")
    uid = await _user(pw, "pick", a, b)
    picker = await _login(pw, "pick", OLD_PW)
    assert isinstance(picker, OrganizationSelectionRequired)
    await pw.sup("UPDATE organizations SET status='suspended' WHERE id=:o", o=b)

    await _expect(
        pw.run(None, lambda s: auth_service.select_organization(s, OrganizationSelectionRequest(selection_token=picker.selection_token, organization_id=b))),
        PermissionDeniedError,
        "auth.organization_suspended",
    )
    ok = await pw.run(None, lambda s: auth_service.select_organization(s, OrganizationSelectionRequest(selection_token=picker.selection_token, organization_id=a)))
    assert (await _identity(pw, ok)).organization_id == a


# ============================================================ delivery, tenancy


@pytest.mark.asyncio
async def test_the_reset_email_has_an_https_fragment_link_and_never_leaks_the_token_elsewhere(pw) -> None:
    org = await _org(pw, "mail")
    await _user(pw, "mail", org)
    delivery = await _request(pw, "mail")
    sender = FakeSender()

    assert await password_reset.deliver_reset_email(delivery, sender=sender) is True

    (message,) = sender.sent
    assert message.to == pw.email("mail")
    match = re.search(r"https://app\.example\.test/reset-password#token=([\w\-]+)", message.text)
    assert match and match.group(1) == delivery.raw_token
    assert "?token=" not in message.text and "#token=" in message.html
    assert "30 minutes" in message.text
    events = await _audit(pw, org, "user.password_reset_requested")
    assert len(events) == 1 and delivery.raw_token not in str(events)  # the audit trail never holds the token


@pytest.mark.asyncio
async def test_audit_rows_are_visible_only_to_their_own_organization(pw) -> None:
    a, b, c = await _org(pw, "ta"), await _org(pw, "tb"), await _org(pw, "tc")
    uid = await _user(pw, "tenant", a, b)
    await _user(pw, "other", c)
    await _request(pw, "tenant")

    async def count(s):
        return (await s.execute(text("SELECT count(*) FROM audit_logs WHERE action='user.password_reset_requested' AND resource_id=:u"), {"u": uid})).scalar()

    assert await pw.run(a, count) == 1 and await pw.run(b, count) == 1 and await pw.run(c, count) == 0
