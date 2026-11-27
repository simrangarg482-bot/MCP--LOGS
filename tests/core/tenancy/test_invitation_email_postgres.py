"""Invitation email delivery against REAL PostgreSQL, as the real `ekip_app` role.

Covers what a mock cannot: the background task's own tenant-scoped session
(row-level security), conditional recording of the delivery outcome, token
rotation on resend, the database-counted invitation caps and their atomicity
under concurrency, and organization isolation of the new columns.

Skipped unless EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL point at a LOCAL,
disposable database on which `alembic upgrade head` has been run (same contract
as `test_role_management_postgres.py`, whose fixtures and helpers it reuses).
No real email is ever sent: a recording fake stands in for the SMTP sender.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import email_verification
from app.core.auth import service as auth_service
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
)
from app.core.tenancy import invitation_email
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationAcceptRequest, InvitationCreate
from app.core.users import role_service
from app.core.users.role_schemas import RoleCreate
from app.shared.config.settings import get_settings
from app.shared.email import EmailDeliveryError, EmailMessage, EmailNotConfiguredError
from app.shared.security import hash_opaque_token
from tests.core.users.test_role_management_postgres import (  # noqa: F401  (fixtures + helpers)
    _APP_URL,
    _SUPER_URL,
    _actor,
    _engine,
    _is_local,
    _make_org,
    _make_user,
    _role_id,
    _setup_org,
    env,
)

pytestmark = pytest.mark.skipif(
    not (_SUPER_URL and _APP_URL and _is_local(_SUPER_URL) and _is_local(_APP_URL)),
    reason="needs EKIP_TEST_POSTGRES_URL and EKIP_TEST_APP_URL pointing at a LOCAL migrated database",
)


# ----------------------------------------------------------------------- helpers


class FakeSender:
    """Records what the SMTP provider 'accepted'; can be told to fail."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.failures = list(failures or [])
        self.calls = 0

    async def send(self, message: EmailMessage) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)


@pytest_asyncio.fixture
async def inv(env, monkeypatch):  # noqa: F811
    """`env` plus: email 'configured', a session factory for the background task
    that does NOT pre-set any tenant context, and cleanup of verification rows."""
    app_engine = _engine(_APP_URL)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)

    @asynccontextmanager
    async def background_session():
        async with sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    env.background_session = background_session
    env.configure = lambda configured=True, **overrides: _configure(monkeypatch, configured, **overrides)
    env.configure(True)
    yield env
    await env.sup("DELETE FROM email_verifications WHERE email LIKE :e", e=f"rm-{env.tag}-%")
    await app_engine.dispose()


def _configure(monkeypatch, configured: bool, **overrides) -> None:
    settings = get_settings().model_copy(
        update={"frontend_public_base_url": "https://app.example.test", **overrides}
    )
    monkeypatch.setattr(tenancy_service, "email_delivery_configured", lambda: configured)
    monkeypatch.setattr(tenancy_service, "get_settings", lambda: settings)
    monkeypatch.setattr(invitation_email, "get_settings", lambda: settings)


def _token_from(message: EmailMessage) -> tuple[str, str]:
    match = re.search(r"/invitations/([0-9a-f-]{36})/accept#token=([A-Za-z0-9_\-]+)", message.text)
    assert match, message.text
    return match.group(1), match.group(2)


async def _invite(env, org_id, actor, email, role="viewer"):
    return await env.run(
        org_id,
        lambda s: tenancy_service.issue_invitation(
            s, actor, org_id, InvitationCreate(email=email, grants_role=role)
        ),
    )


async def _deliver(env, issued, sender):
    return await invitation_email.deliver_invitation_email(
        issued.pending_email,
        sender=sender,
        sleep=_no_sleep,
        session_factory=env.background_session,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


async def _row(env, invitation_id):
    return (
        await env.sup(
            "SELECT status, email_status, email_attempts, email_sent_at, email_last_attempt_at, "
            "resend_count, token_hash, expires_at FROM invitations WHERE id=:i",
            i=invitation_id,
        )
    )[0]


async def _end_cooldown(env, invitation_id):
    await env.sup(
        "UPDATE invitations SET email_last_attempt_at = now() - interval '5 minutes' WHERE id=:i",
        i=invitation_id,
    )


async def _admin_and_actor(env, name="a"):
    org_id, admin_role, admin_id = await _setup_org(env, name)
    return org_id, admin_role, admin_id, await _actor(env, org_id, admin_id)


# ============================================================ issuing + email content


@pytest.mark.asyncio
async def test_issuing_queues_an_email_and_never_returns_the_raw_token(inv) -> None:
    org_id, _r, admin_id, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("newbie"), role="engineer")

    assert issued.invitation.token is None  # the token only travels inside the email
    assert issued.invitation.email_status == "queued"
    assert issued.pending_email is not None
    row = await _row(inv, issued.invitation.id)
    assert row.email_status == "queued" and row.email_attempts == 0 and row.email_sent_at is None

    # only the hash is stored, and it is the hash of the token inside the link
    _id, token = _token_from(issued.pending_email.message)
    assert str(_id) == str(issued.invitation.id)
    assert row.token_hash == hash_opaque_token(token)
    dump = await inv.sup("SELECT row_to_json(i)::text FROM invitations i WHERE id=:i", i=issued.invitation.id)
    assert token not in dump[0][0]
    del admin_id


@pytest.mark.asyncio
async def test_the_email_states_role_permissions_inviter_organization_and_expiry(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("n2"), role="engineer")
    message = issued.pending_email.message
    org_name = f"Org a"

    assert message.to == inv.email("n2")
    assert message.text.count("EKIP") >= 1 and "Accept" in message.text
    for body in (message.text, message.html):
        assert "a-admin" in body  # the inviter's display name
        assert org_name in body
        assert "engineer" in body
        assert inv.email("n2") in body
        assert "https://app.example.test/invitations/" in body
    expires = issued.invitation.expires_at.astimezone(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    assert expires in message.text and expires in message.html
    assert "Accept Invitation" in message.html
    assert "ignore this email" in message.text and "ignore this email" in message.html

    # permission accuracy: exactly the role's permissions as stored, none extra
    role = await inv.run(
        org_id,
        lambda s: role_service.get_role(s, actor, org_id, uuid.UUID(str(issued.invitation.grants_role_id))),
    )
    listed = {
        label
        for label, _d in [
            (p.label, p.description)
            for p in role_service.list_permission_catalog(actor, org_id)
            if p.code in role.permissions
        ]
    }
    everything = {p.label for p in role_service.list_permission_catalog(actor, org_id)}
    assert listed and listed < everything
    for label in listed:
        assert label in message.text and label in message.html
    for label in everything - listed:
        assert label not in message.text and label not in message.html


@pytest.mark.asyncio
async def test_the_builtin_admin_role_lists_every_permission(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("adm"), role="admin")
    for permission in role_service.list_permission_catalog(actor, org_id):
        assert permission.label in issued.pending_email.message.text


@pytest.mark.asyncio
async def test_user_controlled_names_cannot_inject_html_or_headers(inv) -> None:
    org_id = await _make_org(inv, "evil")
    await inv.sup("UPDATE organizations SET name=:n WHERE id=:o", n='<script>alert(1)</script>\r\nBcc: x@evil.test', o=org_id)
    from tests.core.users.test_role_management_postgres import _admin_role_id

    admin_id = await _make_user(inv, org_id, "evil-admin", [await _admin_role_id(inv, org_id)])
    await inv.sup("UPDATE users SET display_name=:n WHERE id=:u", n='"><img src=x onerror=alert(2)>', u=admin_id)
    actor = await _actor(inv, org_id, admin_id)
    issued = await _invite(inv, org_id, actor, inv.email("victim"), role="viewer")
    message = issued.pending_email.message

    assert "<script>" not in message.html and "<img" not in message.html
    assert "&lt;script&gt;" in message.html
    assert "\r" not in message.subject and "\n" not in message.subject
    assert "Bcc:" not in message.subject.split("invited you")[0] or "\n" not in message.subject


# ======================================================== background delivery


@pytest.mark.asyncio
async def test_sent_is_recorded_only_after_the_provider_accepts(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("ok"))
    sender = FakeSender()

    status = await _deliver(inv, issued, sender)

    assert status == "sent" and len(sender.sent) == 1 and sender.sent[0].to == inv.email("ok")
    row = await _row(inv, issued.invitation.id)
    assert row.email_status == "sent" and row.email_attempts == 1 and row.email_sent_at is not None
    audit = await inv.sup(
        "SELECT action FROM audit_logs WHERE organization_id=:o AND resource_id=:i", o=org_id, i=issued.invitation.id
    )
    assert {r[0] for r in audit} >= {"invitation.create", "invitation.email_sent"}


@pytest.mark.asyncio
async def test_the_background_task_sets_its_own_tenant_context(inv) -> None:
    """Its session starts with NO tenant context; had the task not set the
    organization, RLS would match no row and the status would stay `queued`."""
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("ctx"))
    await _deliver(inv, issued, FakeSender())
    assert (await _row(inv, issued.invitation.id)).email_status == "sent"


@pytest.mark.asyncio
async def test_exhausted_retries_record_failed_never_sent(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("fail"))
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    sender = FakeSender([EmailDeliveryError("SMTP delivery failed (SMTPServerDisconnected).")] * 3)
    status = await invitation_email.deliver_invitation_email(
        issued.pending_email, sender=sender, sleep=record_sleep, session_factory=inv.background_session
    )

    assert status == "failed" and sender.calls == 3 and sender.sent == []
    assert delays == [2, 4]  # bounded exponential backoff, as for verification mail
    row = await _row(inv, issued.invitation.id)
    assert row.email_status == "failed" and row.email_attempts == 3 and row.email_sent_at is None


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_then_recorded_as_sent(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("flaky"))
    sender = FakeSender([EmailDeliveryError("SMTP delivery failed (TimeoutError).")])
    assert await _deliver(inv, issued, sender) == "sent"
    row = await _row(inv, issued.invitation.id)
    assert row.email_status == "sent" and row.email_attempts == 2


@pytest.mark.asyncio
async def test_a_provider_that_vanished_is_recorded_as_not_configured_without_retrying(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("gone"))
    sender = FakeSender([EmailNotConfiguredError("no provider")])
    assert await _deliver(inv, issued, sender) == "not_configured" and sender.calls == 1
    assert (await _row(inv, issued.invitation.id)).email_status == "not_configured"


@pytest.mark.asyncio
async def test_delivery_logs_never_contain_the_token_the_link_or_the_recipient(inv, capsys) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("quiet"))
    _id, token = _token_from(issued.pending_email.message)
    sender = FakeSender([EmailDeliveryError("SMTP delivery failed (SMTPServerDisconnected).")])
    await _deliver(inv, issued, sender)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert token not in output and inv.email("quiet") not in output
    assert "/accept#token" not in output
    assert token not in repr(issued.pending_email) and inv.email("quiet") not in repr(issued.pending_email.message)


# ================================================================== not configured


@pytest.mark.asyncio
async def test_without_email_the_admin_gets_the_token_to_copy(inv) -> None:
    inv.configure(False)
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("manual"))

    assert issued.pending_email is None
    assert issued.invitation.email_status == "not_configured"
    assert issued.invitation.token  # the existing copy-link workflow is preserved
    row = await _row(inv, issued.invitation.id)
    assert row.email_status == "not_configured" and row.token_hash == hash_opaque_token(issued.invitation.token)

    # ...and that token is accepted exactly as before (query-string and fragment links carry
    # the same token; the backend never sees which form was used)
    delivery = await inv.run(
        org_id,
        lambda s: auth_service.accept_invitation_with_password(
            s, issued.invitation.id, InvitationAcceptRequest(token=issued.invitation.token, password="a-strong-pass-123")
        ),
    )
    assert delivery is not None


@pytest.mark.asyncio
async def test_resending_by_email_without_a_provider_is_refused_but_a_link_still_works(inv) -> None:
    inv.configure(False)
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("nm"))
    with pytest.raises(ServiceUnavailableError) as exc:
        await inv.run(
            org_id,
            lambda s: tenancy_service.resend_invitation(s, actor, org_id, issued.invitation.id, method="email"),
        )
    assert exc.value.error_code == "email.not_configured"
    link = await inv.run(
        org_id,
        lambda s: tenancy_service.resend_invitation(s, actor, org_id, issued.invitation.id, method="link"),
    )
    assert link.invitation.token and link.pending_email is None


# ===================================================================== resend


@pytest.mark.asyncio
async def test_resend_rotates_the_token_and_the_old_link_stops_working(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    first = await _invite(inv, org_id, actor, inv.email("rot"))
    _i, old_token = _token_from(first.pending_email.message)
    await _deliver(inv, first, FakeSender())
    await _end_cooldown(inv, first.invitation.id)

    again = await inv.run(
        org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, first.invitation.id)
    )
    _i, new_token = _token_from(again.pending_email.message)
    assert new_token != old_token and again.invitation.token is None
    assert again.invitation.email_status == "queued" and again.invitation.resend_count == 1

    async def accept(token):
        return await inv.run(
            org_id,
            lambda s: auth_service.accept_invitation_with_password(
                s, first.invitation.id, InvitationAcceptRequest(token=token, password="a-strong-pass-123")
            ),
        )

    with pytest.raises(PermissionDeniedError) as exc:
        await accept(old_token)
    assert exc.value.error_code == "invitation.invalid_token"
    assert await accept(new_token) is not None  # the new link works


@pytest.mark.asyncio
async def test_a_late_task_for_the_old_link_cannot_overwrite_the_new_delivery(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    first = await _invite(inv, org_id, actor, inv.email("stale"))
    await _end_cooldown(inv, first.invitation.id)
    await inv.run(org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, first.invitation.id))

    status = await _deliver(inv, first, FakeSender())  # the OLD task finishes now

    assert status == "sent"  # it did send its (now dead) link...
    row = await _row(inv, first.invitation.id)
    assert row.email_status == "queued"  # ...but did not claim the new delivery as sent


@pytest.mark.asyncio
async def test_resend_cooldown_and_per_invitation_limit(inv) -> None:
    inv.configure(True, invitation_resend_cooldown_seconds=60, invitation_max_resends=2)
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("cool"))
    iid = issued.invitation.id
    with pytest.raises(RateLimitedError):  # the cooldown already runs from creation
        await inv.run(org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, iid))
    await _end_cooldown(inv, iid)

    async def resend(method="email"):
        return await inv.run(org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, iid, method=method))

    await resend()
    with pytest.raises(RateLimitedError) as exc:
        await resend()  # immediately again
    assert exc.value.error_code == "invitation.resend_cooldown" and exc.value.detail["retry_after_seconds"] > 0

    async def age():
        await inv.sup("UPDATE invitations SET email_last_attempt_at = now() - interval '5 minutes' WHERE id=:i", i=iid)

    await age()
    await resend()  # second allowed resend
    await age()
    with pytest.raises(RateLimitedError) as exc2:
        await resend()  # third exceeds invitation_max_resends=2
    assert exc2.value.error_code == "invitation.resend_limit"
    assert (await _row(inv, iid)).resend_count == 2

    # copying a fresh link is not an email send: no cooldown, no count
    link = await resend("link")
    assert link.invitation.token and (await _row(inv, iid)).email_status == "not_sent"
    assert (await _row(inv, iid)).resend_count == 2


@pytest.mark.asyncio
async def test_resend_extends_a_lapsed_invitation_and_refuses_settled_ones(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    issued = await _invite(inv, org_id, actor, inv.email("lapse"))
    iid = issued.invitation.id
    await inv.sup("UPDATE invitations SET expires_at = now() - interval '1 day' WHERE id=:i", i=iid)
    again = await inv.run(org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, iid, method="link"))
    assert again.invitation.expires_at > datetime.now(timezone.utc) + timedelta(days=13)

    for status in ("revoked", "accepted"):
        await inv.sup("UPDATE invitations SET status=:s WHERE id=:i", s=status, i=iid)
        with pytest.raises(ConflictError) as exc:
            await inv.run(org_id, lambda s: tenancy_service.resend_invitation(s, actor, org_id, iid, method="link"))
        assert exc.value.error_code == "invitation.not_pending"


@pytest.mark.asyncio
async def test_resend_is_authorized_and_organization_scoped(inv) -> None:
    org_a, _ra, _aa, actor_a = await _admin_and_actor(inv, "a")
    org_b, _rb, _ab, actor_b = await _admin_and_actor(inv, "b")
    issued_b = await _invite(inv, org_b, actor_b, inv.email("bs"))
    viewer = await _actor(inv, org_a, await _make_user(inv, org_a, "plain", [await _role_id(inv, org_a, "viewer")]))

    # another organization's invitation is "not found", even for an admin
    with pytest.raises(NotFoundError):
        await inv.run(org_a, lambda s: tenancy_service.resend_invitation(s, actor_a, org_a, issued_b.invitation.id))
    # naming the other organization in the path is refused outright
    with pytest.raises(PermissionDeniedError):
        await inv.run(org_a, lambda s: tenancy_service.resend_invitation(s, actor_a, org_b, issued_b.invitation.id))
    # a plain member cannot resend or create
    issued_a = await _invite(inv, org_a, actor_a, inv.email("as"))
    with pytest.raises(PermissionDeniedError):
        await inv.run(org_a, lambda s: tenancy_service.resend_invitation(s, viewer, org_a, issued_a.invitation.id))
    with pytest.raises(PermissionDeniedError):
        await _invite(inv, org_a, viewer, inv.email("vs"))
    # B's invitation was not touched
    assert (await _row(inv, issued_b.invitation.id)).resend_count == 0


@pytest.mark.asyncio
async def test_rls_hides_and_protects_another_organizations_invitations(inv) -> None:
    org_a, _ra, _aa, _actor_a = await _admin_and_actor(inv, "a")
    org_b, _rb, _ab, actor_b = await _admin_and_actor(inv, "b")
    issued_b = await _invite(inv, org_b, actor_b, inv.email("hidden"))

    seen = await inv.run(org_a, lambda s: s.execute(text("SELECT count(*) FROM invitations WHERE id=:i"), {"i": issued_b.invitation.id}))
    assert seen.scalar_one() == 0

    async def hostile(s):
        return (await s.execute(text("UPDATE invitations SET email_status='sent', token_hash='x' WHERE id=:i"), {"i": issued_b.invitation.id})).rowcount

    assert await inv.run(org_a, hostile) == 0
    assert (await _row(inv, issued_b.invitation.id)).email_status == "queued"


# ======================================================================== caps


@pytest.mark.asyncio
async def test_hourly_and_daily_caps_per_organization(inv) -> None:
    inv.configure(True, invitation_max_per_hour=3, invitation_max_per_day=5)
    org_a, _ra, _aa, actor_a = await _admin_and_actor(inv, "a")
    org_b, _rb, _ab, actor_b = await _admin_and_actor(inv, "b")

    for n in range(3):
        await _invite(inv, org_a, actor_a, inv.email(f"h{n}"))
    with pytest.raises(RateLimitedError) as exc:
        await _invite(inv, org_a, actor_a, inv.email("h3"))
    assert exc.value.error_code == "invitation.hourly_limit" and exc.value.detail == {"limit": 3, "window": "hour"}
    await _invite(inv, org_b, actor_b, inv.email("other-org"))  # another organization is unaffected

    # older invitations leave the hour window but still count for the day
    await inv.sup("UPDATE invitations SET created_at = now() - interval '2 hours' WHERE organization_id=:o", o=org_a)
    await _invite(inv, org_a, actor_a, inv.email("h4"))
    await _invite(inv, org_a, actor_a, inv.email("h5"))
    with pytest.raises(RateLimitedError) as exc2:
        await _invite(inv, org_a, actor_a, inv.email("h6"))
    assert exc2.value.error_code == "invitation.daily_limit"
    # a refused request creates nothing
    assert (await inv.sup("SELECT count(*) FROM invitations WHERE organization_id=:o", o=org_a))[0][0] == 5


@pytest.mark.asyncio
async def test_the_default_limits_are_20_per_hour_and_100_per_day(inv) -> None:
    settings = get_settings()
    assert settings.invitation_max_per_hour == 20 and settings.invitation_max_per_day == 100


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_slip_past_the_cap(inv) -> None:
    inv.configure(True, invitation_max_per_hour=2, invitation_max_per_day=100)
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    await _invite(inv, org_id, actor, inv.email("c0"))  # one slot left

    results = await asyncio.gather(
        *(_invite(inv, org_id, actor, inv.email(f"race{n}")) for n in range(4)), return_exceptions=True
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(isinstance(r, RateLimitedError) for r in results if isinstance(r, BaseException))
    assert (await inv.sup("SELECT count(*) FROM invitations WHERE organization_id=:o", o=org_id))[0][0] == 2


# ================================================ existing vs new users, full flow


@pytest.mark.asyncio
async def test_inviting_an_existing_account_looks_identical_and_reveals_nothing(inv) -> None:
    org_id, _r, admin_id, actor = await _admin_and_actor(inv)
    other_org, _ro, other_admin, _oa = await _admin_and_actor(inv, "other")
    existing_email = inv.email("a-existing")
    await inv.run(other_org, lambda s: _create_user(s, existing_email))

    known = await _invite(inv, org_id, actor, existing_email)
    unknown = await _invite(inv, org_id, actor, inv.email("brand-new"))

    for issued in (known, unknown):
        assert issued.invitation.email_status == "queued" and issued.invitation.token is None
        assert issued.pending_email is not None
    assert known.invitation.model_dump(exclude={"id", "email", "created_at", "updated_at", "expires_at"}) == (
        unknown.invitation.model_dump(exclude={"id", "email", "created_at", "updated_at", "expires_at"})
    )
    sender = FakeSender()
    await _deliver(inv, known, sender)
    assert sender.sent[0].to == existing_email  # the mail goes out like any other

    # the existing behaviour is preserved: such an account cannot be adopted through the link
    _i, token = _token_from(known.pending_email.message)
    with pytest.raises(ConflictError) as exc:
        await inv.run(
            org_id,
            lambda s: auth_service.accept_invitation_with_password(
                s, known.invitation.id, InvitationAcceptRequest(token=token, password="a-strong-pass-123")
            ),
        )
    assert exc.value.error_code == "invitation.account_exists"
    del admin_id, other_admin


async def _create_user(session, email):
    from app.core.users import service as users_service

    return await users_service.create_verified_user(
        session, email=email, display_name="Existing", password_hash=None, via="email_link"
    )


@pytest.mark.asyncio
async def test_a_new_user_completes_the_whole_flow_and_the_second_email_is_still_required(inv) -> None:
    org_id, _r, _a, actor = await _admin_and_actor(inv)
    email = inv.email("a-fresh")
    issued = await _invite(inv, org_id, actor, email, role="engineer")
    await _deliver(inv, issued, FakeSender())
    invitation_id, token = _token_from(issued.pending_email.message)

    # step 1: the invitee opens the emailed link and sets a password -> NO account yet
    delivery = await inv.run(
        org_id,
        lambda s: auth_service.accept_invitation_with_password(
            s, uuid.UUID(invitation_id), InvitationAcceptRequest(token=token, password="a-strong-pass-123", display_name="Fresh")
        ),
    )
    assert delivery is not None and delivery.kind == "invitation"
    assert (await inv.sup("SELECT count(*) FROM users WHERE email=:e", e=email))[0][0] == 0
    assert (await _row(inv, uuid.UUID(invitation_id))).status == "pending"

    # step 2: the SECOND email (mailbox ownership) is what creates the account and grants the role
    await inv.run(None, lambda s: email_verification.verify_token(s, delivery.raw_token))
    engineer = await _role_id(inv, org_id, "engineer")
    held = await inv.sup(
        "SELECT ur.role_id FROM user_roles ur JOIN users u ON u.id=ur.user_id WHERE u.email=:e", e=email
    )
    assert [r[0] for r in held] == [engineer]
    assert (await _row(inv, uuid.UUID(invitation_id))).status == "accepted"
