"""Reset and "password changed" email rendering (pure functions)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.shared.email.templates import (
    password_reset_link,
    render_password_changed_email,
    render_password_reset_email,
)


def test_link_uses_the_fragment_and_normalizes_the_base_url() -> None:
    assert password_reset_link("https://app.example.com/", "abc-DEF_123") == "https://app.example.com/reset-password#token=abc-DEF_123"
    assert "?" not in password_reset_link("https://app.example.com", "t")  # never a query string


def test_reset_email_contents() -> None:
    link = password_reset_link("https://app.example.com", "tok")
    message = render_password_reset_email(to="p@corp.test", link=link, ttl_minutes=30)

    assert message.to == "p@corp.test" and message.subject == "Reset your EKIP password"
    assert link in message.text and link in message.html
    assert "30 minutes" in message.text and "single use" in message.text
    assert "signs you out of every device" in message.text
    assert "ignore this email" in message.text


def test_reset_email_html_escapes_the_link() -> None:
    message = render_password_reset_email(to="p@corp.test", link='https://x.test/reset-password#token="><script>alert(1)</script>', ttl_minutes=30)
    assert "<script>" not in message.html


def test_changed_notice_has_no_link_and_says_when() -> None:
    when = datetime(2026, 11, 26, 14, 5, tzinfo=UTC)
    reset = render_password_changed_email(to="p@corp.test", how="reset", when=when)
    change = render_password_changed_email(to="p@corp.test", how="change", when=when)

    for message in (reset, change):
        assert "http" not in message.text and "http" not in message.html  # nothing to click or phish with
        assert "26 November 2026, 14:05 UTC" in message.text
        assert message.subject == "Your EKIP password was changed"
    assert "was reset" in reset.text and "was changed" in change.text
