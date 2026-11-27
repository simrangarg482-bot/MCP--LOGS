"""The invitation email template and delivery retry logic, without a database."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.core.tenancy import invitation_email
from app.shared.email import EmailDeliveryError, EmailMessage, EmailNotConfiguredError
from app.shared.email.templates import clean_inline, invitation_link, render_invitation_email
from app.shared.schemas import ActorKind, Identity

EXPIRES = datetime(2026, 12, 9, 14, 30, tzinfo=timezone.utc)
PERMISSIONS = [
    ("View incidents", "See incidents, their timelines and related details."),
    ("Write postmortems", "Draft and edit postmortems for incidents."),
]


def _render(**overrides) -> EmailMessage:
    args = dict(
        to="new.person@example.com",
        link="https://app.example.com/invitations/abc/accept#token=SECRET123",
        inviter_name="Priya Nair",
        organization_name="Acme Corp",
        role_name="engineer",
        role_description="Works incidents.",
        permissions=PERMISSIONS,
        expires_at=EXPIRES,
    )
    args.update(overrides)
    return render_invitation_email(**args)


def test_link_puts_the_token_in_the_fragment_not_the_query() -> None:
    link = invitation_link("https://app.example.com/", "inv-1", "tok")
    assert link == "https://app.example.com/invitations/inv-1/accept#token=tok"
    assert "?" not in link


def test_email_contains_everything_the_recipient_needs() -> None:
    message = _render()
    assert message.subject == "Priya Nair invited you to join Acme Corp on EKIP"
    for body in (message.text, message.html):
        for needle in (
            "EKIP",
            "Priya Nair",
            "Acme Corp",
            "new.person@example.com",
            "engineer",
            "View incidents",
            "Write postmortems",
            "See incidents, their timelines and related details.",
            "09 December 2026, 14:30 UTC",
            "SECRET123",
            "ignore this email",
        ):
            assert needle in body, needle
    assert "Accept Invitation" in message.html
    assert 'href="https://app.example.com/invitations/abc/accept#token=SECRET123"' in message.html
    assert "second email" in message.text  # the mailbox-ownership step is explained up front


def test_every_permission_is_listed_and_an_empty_role_says_so() -> None:
    many = [(f"Permission {n}", f"Does thing {n}.") for n in range(12)]
    message = _render(permissions=many)
    assert all(f"Permission {n}" in message.text and f"Permission {n}" in message.html for n in range(12))
    assert "(no permissions)" in _render(permissions=[]).text


def test_user_controlled_text_is_escaped_and_single_line() -> None:
    message = _render(
        inviter_name='"><img src=x onerror=alert(1)>\r\nBcc: attacker@evil.test',
        organization_name="<script>alert(2)</script> & Co",
        role_name="<b>admin</b>",
        role_description="<i>desc</i>",
        permissions=[("<u>Perm</u>", "<svg onload=alert(3)>")],
    )
    assert "<script>" not in message.html and "<img" not in message.html
    assert "<b>admin" not in message.html and "<svg" not in message.html and "<u>Perm" not in message.html
    assert "&lt;script&gt;" in message.html and "&amp; Co" in message.html
    assert "\r" not in message.subject and "\n" not in message.subject
    assert len(message.subject) < 300


def test_clean_inline_collapses_control_characters_and_bounds_length() -> None:
    assert clean_inline("a\r\nb\tc\x00d") == "a b c d"
    assert len(clean_inline("x" * 500, 50)) == 50


def test_the_message_never_reveals_its_content_in_repr() -> None:
    assert "SECRET123" not in repr(_render()) and "new.person" not in repr(_render())


# --- delivery retry logic, with the outcome recording stubbed out ---------------------------


def _pending() -> invitation_email.PendingInvitationEmail:
    actor = Identity(
        kind=ActorKind.USER, subject="u", organization_id=uuid.uuid4(), user_id=uuid.uuid4(),
        permissions=frozenset({"tenancy:manage"}),
    )
    return invitation_email.PendingInvitationEmail(
        invitation_id=uuid.uuid4(), organization_id=actor.organization_id, actor=actor,
        message=_render(), token_hash="h", origin="create",
    )


class _Sender:
    def __init__(self, failures):
        self.failures, self.calls = list(failures), 0

    async def send(self, message):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


@pytest.fixture()
def recorded(monkeypatch):
    seen = []

    async def fake_record(pending, *, status, attempts, session_factory, now):
        seen.append((status, attempts))

    monkeypatch.setattr(invitation_email, "_record_outcome", fake_record)
    return seen


@pytest.mark.asyncio
async def test_success_is_recorded_after_one_attempt(recorded) -> None:
    sender = _Sender([])
    assert await invitation_email.deliver_invitation_email(_pending(), sender=sender) == "sent"
    assert recorded == [("sent", 1)] and sender.calls == 1


@pytest.mark.asyncio
async def test_failure_is_retried_with_backoff_then_recorded_as_failed(recorded) -> None:
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    sender = _Sender([EmailDeliveryError("x")] * 5)
    status = await invitation_email.deliver_invitation_email(_pending(), sender=sender, sleep=sleep)
    assert status == "failed" and recorded == [("failed", 3)] and delays == [2, 4] and sender.calls == 3


@pytest.mark.asyncio
async def test_missing_provider_is_not_retried(recorded) -> None:
    sender = _Sender([EmailNotConfiguredError("none")])
    assert await invitation_email.deliver_invitation_email(_pending(), sender=sender) == "not_configured"
    assert recorded == [("not_configured", 1)] and sender.calls == 1


def test_default_limits() -> None:
    from app.shared.config.settings import Settings

    fields = Settings.model_fields
    assert fields["invitation_max_per_hour"].default == 20
    assert fields["invitation_max_per_day"].default == 100
    assert fields["invitation_resend_cooldown_seconds"].default == 60
    assert fields["invitation_max_resends"].default == 5
