"""Email verification against REAL PostgreSQL, as the real `ekip_app` role.

These are the security tests that matter most for this feature, and they are
deliberately not mocked: single-use consumption is a database statement, the
tenant boundary is row-level security, and the unauthenticated invitation path
only works through a `SECURITY DEFINER` function. None of that exists in a fake.

Skipped unless BOTH are set (and point at a LOCAL, disposable database that has
had `alembic upgrade head` applied, with `EKIP_APP_ROLE_PASSWORD` set for the
`b8f3d6a1c4e7` role migration):

    EKIP_TEST_POSTGRES_URL   superuser/owner URL, used only to seed legacy rows,
                             to inspect rows past RLS, and to clean up
    EKIP_TEST_APP_URL        the `ekip_app` URL (NOSUPERUSER NOBYPASSRLS) -- every
                             service call under test runs through THIS role

e.g. (throwaway server from `pgserver`, see the saved test-env notes)::

    EKIP_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:@127.0.0.1:55329/postgres?sslmode=disable
    EKIP_TEST_APP_URL=postgresql+asyncpg://ekip_app:<pw>@127.0.0.1:55329/postgres?sslmode=disable

Every row a test creates is tagged with a unique token and deleted at teardown
with targeted DELETEs; nothing is truncated or dropped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.auth import email_verification
from app.core.auth import service as auth_service
from app.core.auth.schemas import LoginRequest, SignupRequest
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.tenancy.schemas import InvitationAcceptRequest
from app.core.users import service as users_service
from app.database.session import set_tenant_context
from app.shared.security import hash_opaque_token

_SUPER_URL = os.environ.get("EKIP_TEST_POSTGRES_URL", "")
_APP_URL = os.environ.get("EKIP_TEST_APP_URL", "")


def _is_local(url: str) -> bool:
    return (urlsplit(url.replace("+asyncpg", "")).hostname or "") in {"localhost", "127.0.0.1", "::1"}


pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)

_STRONG_PASSWORD = "a-strong-test-password-123"


def _engine(url: str):
    clean = url.replace("?sslmode=disable", "")
    return create_async_engine(clean, poolclass=NullPool, connect_args={"ssl": False})


@pytest_asyncio.fixture
async def env():
    super_engine, app_engine = _engine(_SUPER_URL), _engine(_APP_URL)
    tag = uuid.uuid4().hex[:10]
    app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)

    async with super_engine.connect() as conn:
        present = (await conn.execute(text("SELECT to_regclass('public.email_verifications')"))).scalar()
    if present is None:
        pytest.skip("email_verifications table missing: run `alembic upgrade head` on this database")

    class Env:
        pass

    e = Env()
    e.tag = tag
    e.super_engine = super_engine
    e.app_engine = app_engine

    async def run(fn):
        """One request-shaped unit of work as `ekip_app`: commit on success,
        roll back on any exception (exactly `get_db_session`)."""
        async with app_sessions() as session:
            try:
                result = await fn(session)
                await session.commit()
                return result
            except BaseException:
                await session.rollback()
                raise

    async def sup(sql, **params):
        async with super_engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return res.fetchall() if res.returns_rows else None

    e.run, e.sup = run, sup
    e.email = lambda name: f"ev-{tag}-{name}@example.invalid"
    e.slug = lambda name: f"ev-{tag}-{name}"
    yield e

    # --- targeted cleanup of this test's own rows (never TRUNCATE/DROP) ---
    like_email, like_slug = f"ev-{tag}-%", f"ev-{tag}-%"
    for sql in (
        "DELETE FROM refresh_tokens WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)",
        "DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE email LIKE :e)",
        "DELETE FROM email_verifications WHERE email LIKE :e",
        "DELETE FROM invitations WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM external_identity_mappings WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM projects WHERE organization_id IN (SELECT id FROM organizations WHERE slug LIKE :s)",
        "DELETE FROM organizations WHERE slug LIKE :s",
        "DELETE FROM users WHERE email LIKE :e",
    ):
        await sup(sql, e=like_email, s=like_slug)
    await super_engine.dispose()
    await app_engine.dispose()


# ----------------------------------------------------------------- helpers


async def _signup(env, name="founder", *, now=None):
    async def go(session):
        return await email_verification.start_registration(
            session,
            email=env.email(name),
            display_name="Founder",
            password_hash=auth_service._hash_password(_STRONG_PASSWORD),
            organization_name=f"Org {name}",
            organization_slug=env.slug(name),
            now=now,
        )

    return await env.run(go)


async def _verify(env, raw_token):
    return await env.run(lambda s: email_verification.verify_token(s, raw_token))


async def _counts(env, name="founder"):
    row = (
        await env.sup(
            "SELECT (SELECT count(*) FROM organizations WHERE slug = :s), "
            "(SELECT count(*) FROM users WHERE email = :e), "
            "(SELECT count(*) FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE email = :e)), "
            "(SELECT count(*) FROM refresh_tokens WHERE user_id IN (SELECT id FROM users WHERE email = :e))",
            s=env.slug(name),
            e=env.email(name),
        )
    )[0]
    return tuple(row)


# =========================================================== password signup


@pytest.mark.asyncio
async def test_the_connection_under_test_really_is_the_rls_enforced_app_role(env) -> None:
    row = (
        await env.run(
            lambda s: s.execute(
                text("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        )
    ).one()
    assert row[0] == "ekip_app" and row[1] is False and row[2] is False


@pytest.mark.asyncio
async def test_signup_creates_only_a_pending_row_no_org_user_role_or_session(env) -> None:
    delivery = await _signup(env)

    assert delivery is not None and delivery.kind == "registration"
    # nothing that confers access exists yet: no org, user, role, refresh token
    assert await _counts(env) == (0, 0, 0, 0)
    (row,) = await env.sup(
        "SELECT token_hash, password_hash, consumed_at, expires_at, organization_slug "
        "FROM email_verifications WHERE email = :e",
        e=env.email("founder"),
    )
    assert row.token_hash == hash_opaque_token(delivery.raw_token) and len(row.token_hash) == 64
    assert row.consumed_at is None and row.expires_at > datetime.now(timezone.utc)
    assert row.password_hash.startswith("$argon2id$")  # Argon2id hash, never the plaintext password


@pytest.mark.asyncio
async def test_raw_token_and_plaintext_password_are_never_persisted_anywhere(env) -> None:
    delivery = await _signup(env)
    dump = await env.sup(
        "SELECT row_to_json(v)::text FROM email_verifications v WHERE email = :e", e=env.email("founder")
    )
    blob = dump[0][0]
    assert delivery.raw_token not in blob
    assert _STRONG_PASSWORD not in blob
    assert delivery.raw_token not in repr(delivery)  # PendingDelivery.repr hides the token


@pytest.mark.asyncio
async def test_verify_creates_org_verified_admin_under_rls_and_no_session(env) -> None:
    delivery = await _signup(env)

    result = await _verify(env, delivery.raw_token)

    assert result.kind == "registration" and result.status == "verified"
    orgs, users, roles, tokens = await _counts(env)
    assert (orgs, users, roles) == (1, 1, 1)
    assert tokens == 0  # verification never authenticates: no refresh token was issued
    (user,) = await env.sup(
        "SELECT id, email_verified_at, email_verified_via, password_hash FROM users WHERE email = :e",
        e=env.email("founder"),
    )
    assert user.email_verified_at is not None and user.email_verified_via == "email_link"
    assert user.password_hash.startswith("$argon2id$")
    (org,) = await env.sup("SELECT id FROM organizations WHERE slug = :s", s=env.slug("founder"))

    # RLS: the admin assignment is visible under ITS organization's context only
    async def roles_under(ctx_org):
        async def go(session):
            await set_tenant_context(session, ctx_org)
            return (
                await session.execute(
                    text("SELECT count(*) FROM user_roles WHERE user_id = :u"), {"u": user.id}
                )
            ).scalar_one()

        return await env.run(go)

    assert await roles_under(org.id) == 1
    assert await roles_under(uuid.uuid4()) == 0  # another tenant's context sees nothing


@pytest.mark.asyncio
async def test_verified_user_can_log_in_and_unverified_cannot_get_a_session(env) -> None:
    delivery = await _signup(env)
    login = LoginRequest(email=env.email("founder"), password=_STRONG_PASSWORD)

    # Before verification the user does not exist at all: login is the generic failure.
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(lambda s: auth_service.login_with_password(s, login))
    assert exc.value.error_code == "auth.invalid_credentials"

    await _verify(env, delivery.raw_token)
    tokens = await env.run(lambda s: auth_service.login_with_password(s, login))
    assert tokens.status == "complete" and tokens.access_token and tokens.refresh_token


@pytest.mark.asyncio
async def test_reused_token_is_rejected_and_creates_nothing_more(env) -> None:
    delivery = await _signup(env)
    await _verify(env, delivery.raw_token)
    before = await _counts(env)

    with pytest.raises(ConflictError) as exc:
        await _verify(env, delivery.raw_token)

    assert exc.value.error_code == "email_verification.already_used"
    assert await _counts(env) == before


@pytest.mark.asyncio
async def test_unknown_token_is_invalid(env) -> None:
    with pytest.raises(ValidationError) as exc:
        await _verify(env, "not-a-real-token-" + uuid.uuid4().hex)
    assert exc.value.error_code == "email_verification.invalid"


@pytest.mark.asyncio
async def test_expired_token_is_rejected_and_creates_nothing(env) -> None:
    delivery = await _signup(env)
    await env.sup(
        "UPDATE email_verifications SET expires_at = now() - interval '1 minute' WHERE email = :e",
        e=env.email("founder"),
    )

    with pytest.raises(ValidationError) as exc:
        await _verify(env, delivery.raw_token)

    assert exc.value.error_code == "email_verification.expired"
    assert await _counts(env) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_failed_activation_rolls_the_consumption_back_so_the_link_is_not_burned(env) -> None:
    """If activation fails (here: someone took the slug between signup and click)
    the single transaction rolls back INCLUDING `consumed_at`, so the user can
    still use the same link once the cause is gone."""
    delivery = await _signup(env)
    await env.sup(
        "INSERT INTO organizations (name, slug, status) VALUES ('squatter', :s, 'onboarding')",
        s=env.slug("founder"),
    )

    with pytest.raises(ConflictError) as exc:
        await _verify(env, delivery.raw_token)
    assert exc.value.error_code == "organization.slug_taken"
    (row,) = await env.sup(
        "SELECT consumed_at FROM email_verifications WHERE email = :e", e=env.email("founder")
    )
    assert row.consumed_at is None  # not burned

    await env.sup("DELETE FROM organizations WHERE slug = :s", s=env.slug("founder"))
    assert (await _verify(env, delivery.raw_token)).status == "verified"


@pytest.mark.asyncio
async def test_concurrent_verification_of_one_token_activates_exactly_once(env) -> None:
    delivery = await _signup(env)

    results = await asyncio.gather(
        *[_verify(env, delivery.raw_token) for _ in range(6)], return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1, results
    assert all(isinstance(f, ConflictError) and f.error_code == "email_verification.already_used" for f in failures)
    assert await _counts(env) == (1, 1, 1, 0)  # one org, one user, one role -- never duplicated


@pytest.mark.asyncio
async def test_duplicate_signup_for_a_registered_address_creates_no_second_identity(env) -> None:
    delivery = await _signup(env)
    await _verify(env, delivery.raw_token)

    # same email, different slug: nothing observable differs, nothing is created or sent
    async def again(session):
        return await email_verification.start_registration(
            session,
            email=env.email("founder").upper(),  # case-insensitive match
            display_name="Impostor",
            password_hash=auth_service._hash_password("attacker-password-123"),
            organization_name="Other",
            organization_slug=env.slug("other"),
        )

    assert await env.run(again) is None
    (n,) = (await env.sup("SELECT count(*) FROM users WHERE lower(email) = :e", e=env.email("founder")))[0]
    assert n == 1
    (m,) = (await env.sup("SELECT count(*) FROM email_verifications WHERE email = :e", e=env.email("founder")))[0]
    assert m == 1  # only the original, now-consumed row


@pytest.mark.asyncio
async def test_signup_for_a_taken_slug_is_reported_before_and_independent_of_email_existence(env) -> None:
    """Order matters for enumeration: a taken slug must produce the same answer
    whether or not the email has an account."""
    await env.sup(
        "INSERT INTO organizations (name, slug, status) VALUES ('taken', :s, 'onboarding')", s=env.slug("taken")
    )
    existing = await _signup(env, "exists")
    await _verify(env, existing.raw_token)

    async def attempt(email):
        async def go(session):
            return await email_verification.start_registration(
                session,
                email=email,
                display_name="X",
                password_hash=auth_service._hash_password(_STRONG_PASSWORD),
                organization_name="X",
                organization_slug=env.slug("taken"),
            )

        return await env.run(go)

    for email in (env.email("exists"), env.email("brand-new")):  # existing account / unknown address
        with pytest.raises(ConflictError) as exc:
            await attempt(email)
        assert exc.value.error_code == "organization.slug_taken"


@pytest.mark.asyncio
async def test_concurrent_signups_for_one_address_leave_one_open_challenge(env) -> None:
    results = await asyncio.gather(*[_signup(env) for _ in range(6)], return_exceptions=True)

    assert not [r for r in results if isinstance(r, BaseException)], results
    (n,) = (
        await env.sup(
            "SELECT count(*) FROM email_verifications WHERE email = :e AND consumed_at IS NULL",
            e=env.email("founder"),
        )
    )[0]
    assert n == 1  # the partial unique index holds under concurrency
    assert sum(1 for r in results if r is not None) >= 1  # at least one send was issued


@pytest.mark.asyncio
async def test_last_signup_wins_so_a_squatter_cannot_block_the_real_owner(env) -> None:
    attacker = await _signup(env)  # attacker pre-registers the victim's address
    await env.sup(
        "UPDATE email_verifications SET last_sent_at = now() - interval '2 minutes', "
        "send_window_started_at = now() - interval '2 minutes' WHERE email = :e",
        e=env.email("founder"),
    )
    owner = await _signup(env)  # the real owner signs up later (cooldown elapsed)

    assert owner is not None
    with pytest.raises(ValidationError):  # the attacker's earlier link is dead
        await _verify(env, attacker.raw_token)
    assert (await _verify(env, owner.raw_token)).status == "verified"


# ============================================================ resend / limits


@pytest.mark.asyncio
async def test_resend_rotates_the_token_old_link_dies_new_link_works(env) -> None:
    first = await _signup(env)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)

    second = await env.run(
        lambda s: email_verification.resend_verification(s, email=env.email("founder"), now=later)
    )

    assert second is not None and second.raw_token != first.raw_token
    with pytest.raises(ValidationError) as exc:
        await _verify(env, first.raw_token)
    assert exc.value.error_code == "email_verification.invalid"  # replaced, not merely unused
    assert (await _verify(env, second.raw_token)).status == "verified"


@pytest.mark.asyncio
async def test_resend_inside_the_cooldown_is_silently_suppressed_and_keeps_the_original_valid(env) -> None:
    first = await _signup(env)

    suppressed = await env.run(
        lambda s: email_verification.resend_verification(s, email=env.email("founder"))
    )

    assert suppressed is None
    assert (await _verify(env, first.raw_token)).status == "verified"  # original untouched


@pytest.mark.asyncio
async def test_resend_is_capped_per_day_in_the_database(env) -> None:
    """The Redis limiter fails open; this cap lives in the row, so a Redis outage
    cannot turn resend into a mail bomber."""
    from app.shared.config.settings import get_settings

    cap = get_settings().email_verification_max_sends_per_day
    await _signup(env)  # send #1
    base = datetime.now(timezone.utc)
    sent = 1
    for i in range(1, cap + 3):
        when = base + timedelta(minutes=2 * i)  # always past the cooldown, always inside 24h
        delivery = await env.run(
            lambda s, when=when: email_verification.resend_verification(
                s, email=env.email("founder"), now=when
            )
        )
        if delivery is not None:
            sent += 1
    assert sent == cap


@pytest.mark.asyncio
async def test_concurrent_resends_rotate_at_most_once_within_the_cooldown(env) -> None:
    await _signup(env)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)

    results = await asyncio.gather(
        *[
            env.run(lambda s: email_verification.resend_verification(s, email=env.email("founder"), now=later))
            for _ in range(6)
        ]
    )

    assert sum(1 for r in results if r is not None) == 1  # FOR UPDATE + cooldown


@pytest.mark.asyncio
async def test_resend_reveals_nothing_unknown_and_verified_addresses_get_nothing(env) -> None:
    done = await _signup(env)
    await _verify(env, done.raw_token)

    unknown = await env.run(lambda s: email_verification.resend_verification(s, email=env.email("nobody")))
    verified = await env.run(lambda s: email_verification.resend_verification(s, email=env.email("founder")))
    garbage = await env.run(lambda s: email_verification.resend_verification(s, email="not an email"))

    assert unknown is None and verified is None and garbage is None


# ======================================================= legacy accounts


async def _seed_legacy_user(env, name, *, password=_STRONG_PASSWORD, org=True):
    pw = auth_service._hash_password(password)
    (u,) = await env.sup(
        "INSERT INTO users (email, display_name, password_hash, is_active) "
        "VALUES (:e, 'Legacy', :p, true) RETURNING id",
        e=env.email(name),
        p=pw,
    )
    org_id = role_id = None
    if org:
        role_id = await env.run(users_service.ensure_admin_role)
        (o,) = await env.sup(
            "INSERT INTO organizations (name, slug, status) VALUES (:n, :s, 'onboarding') RETURNING id",
            n=f"Legacy {name}",
            s=env.slug(name),
        )
        org_id = o.id
        await env.sup(
            "INSERT INTO user_roles (user_id, organization_id, role_id) VALUES (:u, :o, :r)",
            u=u.id,
            o=org_id,
            r=role_id,
        )
    return u.id, org_id


@pytest.mark.asyncio
async def test_legacy_unverified_user_is_blocked_everywhere_until_verified(env) -> None:
    user_id, org_id = await _seed_legacy_user(env, "legacy")
    login = LoginRequest(email=env.email("legacy"), password=_STRONG_PASSWORD)

    # password login: correct password, still no session
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(lambda s: auth_service.login_with_password(s, login))
    assert exc.value.error_code == "auth.email_not_verified"

    # an access token minted BEFORE this feature existed is cut off at resolve_identity
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(lambda s: users_service.resolve_identity(s, user_id, org_id))
    assert exc.value.error_code == "auth.email_not_verified"

    # a still-valid refresh token from before cannot mint new tokens, and is not rotated
    raw_refresh = "legacy-refresh-" + uuid.uuid4().hex
    await env.sup(
        "INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at) "
        "VALUES (:u, :o, gen_random_uuid(), :h, now() + interval '10 days')",
        u=user_id,
        o=org_id,
        h=auth_service._hash_token(raw_refresh),
    )
    from app.core.auth.schemas import RefreshRequest

    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(lambda s: auth_service.refresh(s, RefreshRequest(refresh_token=raw_refresh)))
    assert exc.value.error_code == "auth.email_not_verified"
    (tok,) = await env.sup(
        "SELECT revoked_at FROM refresh_tokens WHERE token_hash = :h", h=auth_service._hash_token(raw_refresh)
    )
    assert tok.revoked_at is None  # a refused refresh did not rotate/revoke it


@pytest.mark.asyncio
async def test_legacy_user_reverifies_by_link_and_old_sessions_are_revoked(env) -> None:
    user_id, org_id = await _seed_legacy_user(env, "legacy")
    raw_refresh = "legacy-refresh-" + uuid.uuid4().hex
    await env.sup(
        "INSERT INTO refresh_tokens (user_id, organization_id, family_id, token_hash, expires_at) "
        "VALUES (:u, :o, gen_random_uuid(), :h, now() + interval '10 days')",
        u=user_id,
        o=org_id,
        h=auth_service._hash_token(raw_refresh),
    )

    delivery = await env.run(
        lambda s: email_verification.resend_verification(s, email=env.email("legacy"))
    )
    assert delivery is not None and delivery.kind == "existing_user"
    assert (await _verify(env, delivery.raw_token)).kind == "existing_user"

    (u,) = await env.sup(
        "SELECT email_verified_at, email_verified_via FROM users WHERE id = :u", u=user_id
    )
    assert u.email_verified_at is not None and u.email_verified_via == "email_link"
    (tok,) = await env.sup(
        "SELECT revoked_at FROM refresh_tokens WHERE token_hash = :h", h=auth_service._hash_token(raw_refresh)
    )
    assert tok.revoked_at is not None  # whatever sessions existed while ownership was unproven are dead
    # and a fresh normal login now works
    tokens = await env.run(
        lambda s: auth_service.login_with_password(
            s, LoginRequest(email=env.email("legacy"), password=_STRONG_PASSWORD)
        )
    )
    assert tokens.status == "complete"


@pytest.mark.asyncio
async def test_deactivated_legacy_user_gets_no_verification_mail(env) -> None:
    user_id, _ = await _seed_legacy_user(env, "legacy", org=False)
    await env.sup("UPDATE users SET is_active = false WHERE id = :u", u=user_id)

    delivery = await env.run(
        lambda s: email_verification.resend_verification(s, email=env.email("legacy"))
    )

    assert delivery is None  # verification can never be used to reactivate a deactivated account


# ======================================================= invitations + RLS


async def _seed_org_with_invitation(env, *, invited_email, raw_token="the-invite-token", org_name="inv"):
    admin_role = await env.run(users_service.ensure_admin_role)
    (inviter,) = await env.sup(
        "INSERT INTO users (email, display_name, is_active, email_verified_at) "
        "VALUES (:e, 'Inviter', true, now()) RETURNING id",
        e=env.email(f"{org_name}-inviter"),
    )
    (org,) = await env.sup(
        "INSERT INTO organizations (name, slug, status) VALUES (:n, :s, 'onboarding') RETURNING id",
        n=f"Org {org_name}",
        s=env.slug(org_name),
    )
    (inv,) = await env.sup(
        "INSERT INTO invitations (organization_id, email, status, token_hash, grants_role_id, invited_by, expires_at) "
        "VALUES (:o, :e, 'pending', :h, :r, :i, now() + interval '3 days') RETURNING id",
        o=org.id,
        e=invited_email,
        h=hash_opaque_token(raw_token),
        r=admin_role,
        i=inviter.id,
    )
    return org.id, inv.id


@pytest.mark.asyncio
async def test_invitation_accept_works_unauthenticated_under_rls_but_creates_nothing_until_verified(env) -> None:
    org_id, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("invitee"))

    delivery = await env.run(
        lambda s: auth_service.accept_invitation_with_password(
            s, inv_id, InvitationAcceptRequest(token="the-invite-token", password=_STRONG_PASSWORD)
        )
    )

    assert delivery is not None and delivery.kind == "invitation"
    assert (await _counts(env, "invitee"))[1] == 0  # no user yet
    (inv,) = await env.sup("SELECT status FROM invitations WHERE id = :i", i=inv_id)
    assert inv.status == "pending"  # not consumed by an unproven mailbox

    result = await _verify(env, delivery.raw_token)

    assert result.kind == "invitation"
    (inv,) = await env.sup("SELECT status, accepted_at FROM invitations WHERE id = :i", i=inv_id)
    assert inv.status == "accepted" and inv.accepted_at is not None
    (u,) = await env.sup(
        "SELECT id, email_verified_at FROM users WHERE email = :e", e=env.email("invitee")
    )
    assert u.email_verified_at is not None
    (role,) = await env.sup(
        "SELECT organization_id FROM user_roles WHERE user_id = :u", u=u.id
    )
    assert role.organization_id == org_id  # scoped to the inviting organization only


@pytest.mark.asyncio
async def test_invitation_with_wrong_token_is_refused_and_records_nothing(env) -> None:
    _org, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("invitee"))

    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(
            lambda s: auth_service.accept_invitation_with_password(
                s, inv_id, InvitationAcceptRequest(token="guessed", password=_STRONG_PASSWORD)
            )
        )

    assert exc.value.error_code == "invitation.invalid_token"
    (n,) = (await env.sup("SELECT count(*) FROM email_verifications WHERE email = :e", e=env.email("invitee")))[0]
    assert n == 0


@pytest.mark.asyncio
async def test_invitation_cannot_take_over_an_existing_account(env) -> None:
    """The confirmed pre-fix account takeover: an org admin holds the invitation
    token and names a victim's email. The victim's password and sessions must be
    untouched and nothing is issued."""
    victim_id, _ = await _seed_legacy_user(env, "victim", org=False)
    await env.sup("UPDATE users SET email_verified_at = now() WHERE id = :u", u=victim_id)
    (before,) = await env.sup("SELECT password_hash FROM users WHERE id = :u", u=victim_id)
    _org, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("victim"))

    with pytest.raises(ConflictError) as exc:
        await env.run(
            lambda s: auth_service.accept_invitation_with_password(
                s,
                inv_id,
                InvitationAcceptRequest(token="the-invite-token", password="attacker-chosen-password"),
            )
        )

    assert exc.value.error_code == "invitation.account_exists"
    (after,) = await env.sup("SELECT password_hash FROM users WHERE id = :u", u=victim_id)
    assert after.password_hash == before.password_hash  # password NOT overwritten
    assert (await _counts(env, "victim"))[2:] == (0, 0)  # no role granted, no session issued
    (inv,) = await env.sup("SELECT status FROM invitations WHERE id = :i", i=inv_id)
    assert inv.status == "pending"


@pytest.mark.asyncio
async def test_invitation_revoked_between_accept_and_verify_cannot_be_activated(env) -> None:
    _org, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("invitee"))
    delivery = await env.run(
        lambda s: auth_service.accept_invitation_with_password(
            s, inv_id, InvitationAcceptRequest(token="the-invite-token", password=_STRONG_PASSWORD)
        )
    )
    await env.sup("UPDATE invitations SET status = 'revoked' WHERE id = :i", i=inv_id)

    with pytest.raises(ConflictError) as exc:
        await _verify(env, delivery.raw_token)

    assert exc.value.error_code == "invitation.not_pending"
    assert (await _counts(env, "invitee"))[1] == 0


@pytest.mark.asyncio
async def test_invitation_email_mismatch_cannot_be_activated(env) -> None:
    """The challenge is bound to the invited address; a row naming a different
    email than the invitation must not activate."""
    _org, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("invitee"))
    delivery = await env.run(
        lambda s: auth_service.accept_invitation_with_password(
            s, inv_id, InvitationAcceptRequest(token="the-invite-token", password=_STRONG_PASSWORD)
        )
    )
    await env.sup(
        "UPDATE invitations SET email = :e WHERE id = :i", e=env.email("someone-else"), i=inv_id
    )

    with pytest.raises(ConflictError):
        await _verify(env, delivery.raw_token)


# ======================================================= SSO linking policy


@pytest.mark.asyncio
async def test_sso_provisioning_creates_verified_user_links_only_verified_accounts(env) -> None:
    # brand-new address -> a new user, created already-verified via oidc, no password
    new_id = await env.run(
        lambda s: users_service.resolve_user_for_sso_provisioning(
            s, email=env.email("sso-new").upper(), display_name="New"
        )
    )
    (u,) = await env.sup(
        "SELECT email, email_verified_via, password_hash FROM users WHERE id = :i", i=new_id
    )
    assert u.email == env.email("sso-new") and u.email_verified_via == "oidc" and u.password_hash is None

    # existing + verified account -> linked to that account (we independently know its owner)
    verified_id, _ = await _seed_legacy_user(env, "sso-verified", org=False)
    await env.sup("UPDATE users SET email_verified_at = now() WHERE id = :u", u=verified_id)
    got = await env.run(
        lambda s: users_service.resolve_user_for_sso_provisioning(
            s, email=env.email("sso-verified"), display_name="x"
        )
    )
    assert got == verified_id

    # existing + UNVERIFIED account -> refused: never linked by email string alone
    squat_id, _ = await _seed_legacy_user(env, "sso-squat", org=False)
    with pytest.raises(PermissionDeniedError) as exc:
        await env.run(
            lambda s: users_service.resolve_user_for_sso_provisioning(
                s, email=env.email("sso-squat"), display_name="x"
            )
        )
    assert exc.value.error_code == "auth.account_email_unverified"
    (still,) = await env.sup("SELECT email_verified_at FROM users WHERE id = :u", u=squat_id)
    assert still.email_verified_at is None  # SSO did not silently verify it either


# ================================================= RLS / grants regression


@pytest.mark.asyncio
async def test_rls_policies_and_grants_are_unchanged_by_the_migration(env) -> None:
    # tenant-isolation policy still ENABLED + FORCED on the sensitive tables
    rows = await env.sup(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('user_roles','invitations','refresh_tokens','external_identity_mappings')"
    )
    assert len(rows) == 4 and all(r.relrowsecurity and r.relforcerowsecurity for r in rows)
    pol = await env.sup(
        "SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation' AND tablename IN "
        "('user_roles','invitations','refresh_tokens','external_identity_mappings')"
    )
    assert len(pol) == 4
    # the new table is deliberately global: no policy, and ekip_app has plain DML only
    assert not await env.sup("SELECT 1 FROM pg_policies WHERE tablename = 'email_verifications'")
    (priv,) = await env.sup(
        "SELECT has_table_privilege('ekip_app','email_verifications','SELECT') AS can_select, "
        "has_table_privilege('ekip_app','email_verifications','TRUNCATE') AS can_truncate, "
        "has_table_privilege('ekip_app','email_verifications','REFERENCES') AS can_reference, "
        "has_table_privilege('ekip_app','email_verifications','TRIGGER') AS can_trigger"
    )
    assert priv.can_select is True
    assert (priv.can_truncate, priv.can_reference, priv.can_trigger) == (False, False, False)
    # the app role was not widened
    (role,) = await env.sup(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'ekip_app'"
    )
    assert (role.rolsuper, role.rolbypassrls, role.rolcreatedb, role.rolcreaterole) == (False,) * 4


@pytest.mark.asyncio
async def test_invitation_resolver_leaks_only_an_organization_id_and_rls_still_hides_the_row(env) -> None:
    org_id, inv_id = await _seed_org_with_invitation(env, invited_email=env.email("invitee"))

    async def probe(session):
        await set_tenant_context(session, uuid.uuid4())  # some OTHER tenant's context
        direct = (
            await session.execute(text("SELECT count(*) FROM invitations WHERE id = :i"), {"i": inv_id})
        ).scalar_one()
        resolved = (
            await session.execute(
                text("SELECT resolve_invitation_organization(:i)"), {"i": str(inv_id)}
            )
        ).scalar_one()
        unknown = (
            await session.execute(
                text("SELECT resolve_invitation_organization(:i)"), {"i": str(uuid.uuid4())}
            )
        ).scalar_one()
        return direct, resolved, unknown

    direct, resolved, unknown = await env.run(probe)

    assert direct == 0  # RLS: another tenant cannot read the invitation row
    assert resolved == org_id  # the bypass function returns ONLY the owning org id
    assert unknown is None


@pytest.mark.asyncio
async def test_cross_tenant_isolation_of_new_accounts(env) -> None:
    a = await _signup(env, "tenant-a")
    b = await _signup(env, "tenant-b")
    await _verify(env, a.raw_token)
    await _verify(env, b.raw_token)
    (org_a,) = await env.sup("SELECT id FROM organizations WHERE slug = :s", s=env.slug("tenant-a"))
    (org_b,) = await env.sup("SELECT id FROM organizations WHERE slug = :s", s=env.slug("tenant-b"))

    async def visible_member_emails(ctx):
        async def go(session):
            await set_tenant_context(session, ctx)
            rows = (
                await session.execute(
                    text(
                        "SELECT u.email FROM users u JOIN user_roles r ON r.user_id = u.id "
                        "WHERE u.email LIKE :p"
                    ),
                    {"p": f"ev-{env.tag}-%"},
                )
            ).scalars().all()
            return set(rows)

        return await env.run(go)

    assert await visible_member_emails(org_a.id) == {env.email("tenant-a")}
    assert await visible_member_emails(org_b.id) == {env.email("tenant-b")}


@pytest.mark.asyncio
async def test_signup_service_call_never_issues_tokens_or_creates_rows_for_an_existing_address(env) -> None:
    done = await _signup(env)
    await _verify(env, done.raw_token)
    before = await _counts(env)

    async def via_service(session):
        return await auth_service.signup(
            session,
            SignupRequest(
                email=env.email("founder"),
                password="another-password-123",
                display_name="Dup",
                organization_name="Dup",
                organization_slug=env.slug("dup"),
            ),
        )

    assert await env.run(via_service) is None
    assert await _counts(env) == before
    (n,) = (await env.sup("SELECT count(*) FROM organizations WHERE slug = :s", s=env.slug("dup")))[0]
    assert n == 0
