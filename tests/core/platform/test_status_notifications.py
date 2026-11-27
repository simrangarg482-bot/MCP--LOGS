"""Suspension / reactivation emails: privacy and failure handling -- no database."""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.core.platform import notifications
from app.core.platform.notifications import PendingStatusNotice, deliver_status_notices
from app.shared.email import EmailDeliveryError, EmailNotConfiguredError
from app.shared.email.templates import (
    render_organization_reactivated_email,
    render_organization_suspended_email,
)

ORG = uuid.uuid4()


class Sender:
    def __init__(self, fail_times=0, exc=EmailDeliveryError):
        self.sent, self.calls, self.fail_times, self.exc = [], 0, fail_times, exc

    async def send(self, message):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc("down")
        self.sent.append(message)


async def _no_sleep(_s):
    return None


def _notice(kind="suspended", message="Contact billing"):
    return PendingStatusNotice(to="admin@example.invalid", kind=kind, organization_name="Acme", customer_message=message, organization_id=ORG)


def test_the_renderers_cannot_receive_an_operator_reason_or_note() -> None:
    for fn in (render_organization_suspended_email, render_organization_reactivated_email):
        names = set(inspect.signature(fn).parameters)
        assert not names & {"reason", "internal_note", "note", "operator", "operator_email"}
    assert set(inspect.signature(notifications.prepare_status_notices).parameters) >= {"customer_message"}
    assert "reason" not in inspect.signature(notifications.prepare_status_notices).parameters
    assert "internal_note" not in inspect.signature(notifications.prepare_status_notices).parameters
    assert not {"reason", "internal_note"} & set(PendingStatusNotice.__dataclass_fields__)


def test_the_suspension_mail_carries_the_message_escaped_and_no_link() -> None:
    m = render_organization_suspended_email(to="a@b.c", organization_name="Acme\r\nBcc: x@y.z", customer_message="<script>alert(1)</script> pay up")
    assert "\r" not in m.subject and "\n" not in m.subject
    assert "<script>" not in (m.html or "") and "&lt;script&gt;" in m.html
    assert "http" not in m.text and "http" not in m.html
    assert "pay up" in m.text


def test_the_mail_without_a_message_is_still_complete() -> None:
    m = render_organization_suspended_email(to="a@b.c", organization_name="Acme", customer_message=None)
    assert "suspended" in m.subject and "Message from EKIP" not in m.text


def test_the_reactivation_mail_asks_everyone_to_sign_in_again() -> None:
    m = render_organization_reactivated_email(to="a@b.c", organization_name="Acme", sign_in_url="https://app.example/login")
    assert "restored" in m.subject and "sign in again" in m.text.lower() and "https://app.example/login" in m.text


@pytest.mark.asyncio
async def test_delivery_retries_then_succeeds() -> None:
    sender = Sender(fail_times=1)
    assert await deliver_status_notices([_notice()], sender=sender, sleep=_no_sleep) == 1
    assert sender.calls == 2


@pytest.mark.asyncio
async def test_delivery_gives_up_quietly_and_continues_with_the_next_recipient(monkeypatch) -> None:
    monkeypatch.setattr(notifications, "_DELIVERY_ATTEMPTS", 2)
    sender = Sender(fail_times=2)  # the first recipient fails both attempts, the second succeeds
    sent = await deliver_status_notices([_notice(), _notice()], sender=sender, sleep=_no_sleep)
    assert sent == 1 and sender.calls == 3


@pytest.mark.asyncio
async def test_no_provider_configured_is_not_an_error() -> None:
    assert await deliver_status_notices([_notice()], sender=Sender(fail_times=9, exc=EmailNotConfiguredError), sleep=_no_sleep) == 0


@pytest.mark.asyncio
async def test_an_unexpected_error_never_escapes(monkeypatch) -> None:
    def explode(_n):
        raise RuntimeError("template bug")

    monkeypatch.setattr(notifications, "_render", explode)
    assert await deliver_status_notices([_notice()], sender=Sender(), sleep=_no_sleep) == 0


def test_the_notice_repr_hides_the_address() -> None:
    assert "admin@example.invalid" not in repr(_notice())
