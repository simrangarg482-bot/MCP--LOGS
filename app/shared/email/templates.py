"""Message rendering, separate from delivery. Pure functions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from html import escape

from app.shared.email.messages import EmailMessage


def verification_link(frontend_base_url: str, raw_token: str) -> str:
    """`<base>/verify-email#token=...` -- the token rides in the URL
    *fragment*, which browsers never send to servers or in `Referer`, so it
    stays out of access logs and proxies. The SPA reads it from
    `location.hash`, removes it from the address bar, and POSTs it.
    """
    return f"{frontend_base_url.rstrip('/')}/verify-email#token={raw_token}"


def render_verification_email(
    *, to: str, link: str, ttl_minutes: int, kind: str
) -> EmailMessage:
    hours = max(1, round(ttl_minutes / 60))
    what = {
        "registration": "create your EKIP account",
        "invitation": "accept your EKIP organization invitation",
        "existing_user": "confirm the email address on your EKIP account",
    }.get(kind, "confirm your email address")
    subject = "Confirm your email address for EKIP"
    text = (
        f"Someone (hopefully you) asked to {what} using this email address.\n\n"
        f"Confirm it by opening this link (valid for about {hours} hour(s), single use):\n\n"
        f"{link}\n\n"
        "If you did not request this, ignore this email - nothing will happen and "
        "no account is created without this confirmation.\n"
    )
    safe_link = escape(link, quote=True)
    html = (
        f"<p>Someone (hopefully you) asked to {escape(what)} using this email address.</p>"
        f"<p><a href=\"{safe_link}\">Confirm your email address</a></p>"
        f"<p>This link is valid for about {hours} hour(s) and can be used once.</p>"
        "<p>If you did not request this, ignore this email &mdash; nothing will happen and "
        "no account is created without this confirmation.</p>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


# --- invitation email --------------------------------------------------------------------


def invitation_link(frontend_base_url: str, invitation_id: object, raw_token: str) -> str:
    """`<base>/invitations/<id>/accept#token=...` -- the token rides in the URL
    *fragment* (never sent to servers, logs or `Referer`); the accept page reads
    it from `location.hash` and removes it from the address bar. The older
    `?token=` query form is still understood by that page, so links issued
    before this change keep working."""
    return f"{frontend_base_url.rstrip('/')}/invitations/{invitation_id}/accept#token={raw_token}"


_CONTROL_CHARS = {c: " " for c in range(0, 32)} | {0x7F: " "}


def clean_inline(value: str, limit: int = 120) -> str:
    """User-controlled text (organization / person names) as one safe line:
    control characters (incl. CR/LF -- header injection) become spaces,
    whitespace is collapsed and the length is bounded."""
    text = " ".join(str(value).translate(_CONTROL_CHARS).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_invitation_email(
    *,
    to: str,
    link: str,
    inviter_name: str,
    organization_name: str,
    role_name: str,
    role_description: str | None,
    permissions: Sequence[tuple[str, str]],
    expires_at: datetime,
) -> EmailMessage:
    """The "you have been invited" message.

    `permissions` is the role's complete list as `(label, description)` pairs,
    so the recipient sees exactly what access they are accepting. Everything
    user-controlled is cleaned for text and HTML-escaped for HTML; the link is
    only ever placed in an escaped `href` and as plain text.
    """
    inviter = clean_inline(inviter_name, 80) or "A colleague"
    org = clean_inline(organization_name, 80) or "an organization"
    role = clean_inline(role_name, 64)
    desc = clean_inline(role_description, 300) if role_description else ""
    when = expires_at.astimezone(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    recipient = clean_inline(to, 254)
    subject = f"{inviter} invited you to join {org} on EKIP"

    perm_lines = [(clean_inline(label, 80), clean_inline(d, 300)) for label, d in permissions]
    text_perms = (
        "\n".join(f"  - {label}: {d}" if d else f"  - {label}" for label, d in perm_lines)
        or "  - (no permissions)"
    )
    text = (
        "EKIP - Enterprise Knowledge Incident Intelligence Platform\n"
        "==========================================================\n\n"
        f"{inviter} invited you ({recipient}) to join the organization \"{org}\" on EKIP.\n\n"
        f"Role you will receive: {role}\n"
        + (f"{desc}\n" if desc else "")
        + f"\nWith this role you will be able to:\n{text_perms}\n\n"
        f"Accept the invitation by opening this link (valid until {when}, single use):\n\n"
        f"{link}\n\n"
        "What happens next: you will choose a password and then confirm this email address "
        "through a second email before your account is created.\n\n"
        "If you were not expecting this invitation, ignore this email - nothing happens and "
        "no account is created unless you accept it. EKIP will never ask for your password "
        "by email.\n"
    )

    safe_link = escape(link, quote=True)
    perm_items = "".join(
        f"<li style=\"margin:0 0 6px 0\"><strong>{escape(label)}</strong>"
        + (f"<br><span style=\"color:#555\">{escape(d)}</span>" if d else "")
        + "</li>"
        for label, d in perm_lines
    ) or "<li>(no permissions)</li>"
    html = (
        "<div style=\"font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;"
        "color:#1a1a1a;line-height:1.5\">"
        "<div style=\"background:#4f46e5;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0\">"
        "<span style=\"font-size:20px;font-weight:bold;letter-spacing:1px\">EKIP</span><br>"
        "<span style=\"font-size:12px;opacity:.85\">Enterprise Knowledge Incident Intelligence "
        "Platform</span></div>"
        "<div style=\"border:1px solid #e5e7eb;border-top:0;padding:20px;border-radius:0 0 8px 8px\">"
        f"<p><strong>{escape(inviter)}</strong> invited you (<span>{escape(recipient)}</span>) to join "
        f"the organization <strong>{escape(org)}</strong> on EKIP.</p>"
        f"<p style=\"margin-bottom:4px\">Role you will receive: <strong>{escape(role)}</strong></p>"
        + (f"<p style=\"margin-top:0;color:#555\">{escape(desc)}</p>" if desc else "")
        + "<p style=\"margin-bottom:6px\">With this role you will be able to:</p>"
        f"<ul style=\"padding-left:20px;margin-top:0\">{perm_items}</ul>"
        f"<p style=\"text-align:center;margin:24px 0\"><a href=\"{safe_link}\" "
        "style=\"background:#4f46e5;color:#fff;text-decoration:none;padding:12px 24px;"
        "border-radius:6px;font-weight:bold;display:inline-block\">Accept Invitation</a></p>"
        f"<p style=\"font-size:13px;color:#555\">This invitation is valid until <strong>{escape(when)}"
        "</strong> and can be used once. After you choose a password, you will confirm this email "
        "address through a second email before your account is created.</p>"
        "<p style=\"font-size:12px;color:#777\">If the button does not work, copy this link into your "
        f"browser:<br><span style=\"word-break:break-all\">{escape(link)}</span></p>"
        "<p style=\"font-size:12px;color:#777;border-top:1px solid #e5e7eb;padding-top:12px\">"
        "If you were not expecting this invitation, ignore this email &mdash; nothing happens and no "
        "account is created unless you accept it. EKIP will never ask for your password by email.</p>"
        "</div></div>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


# --- password reset / password changed ----------------------------------------------------


def password_reset_link(frontend_base_url: str, raw_token: str) -> str:
    """`<base>/reset-password#token=...` -- the token rides in the URL *fragment*
    (never sent to servers, logs or `Referer`); the SPA reads it, removes it from the
    address bar and POSTs it. Never put the token in a query string."""
    return f"{frontend_base_url.rstrip('/')}/reset-password#token={raw_token}"


def render_password_reset_email(*, to: str, link: str, ttl_minutes: int) -> EmailMessage:
    subject = "Reset your EKIP password"
    text = (
        "We received a request to reset the password for your EKIP account.\n\n"
        f"Choose a new password by opening this link (valid for {ttl_minutes} minutes, "
        "single use):\n\n"
        f"{link}\n\n"
        "Resetting your password signs you out of every device. If you did not ask for "
        "this, you can ignore this email: your password stays the same.\n"
    )
    safe_link = escape(link, quote=True)
    html = (
        "<p>We received a request to reset the password for your EKIP account.</p>"
        f"<p><a href=\"{safe_link}\">Choose a new password</a></p>"
        f"<p>This link is valid for {ttl_minutes} minutes and can be used once.</p>"
        "<p>Resetting your password signs you out of every device. If you did not ask for "
        "this, you can ignore this email &mdash; your password stays the same.</p>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def render_password_changed_email(*, to: str, how: str, when: datetime) -> EmailMessage:
    """Security notice sent AFTER a password was reset (`how="reset"`) or changed
    (`how="change"`). Contains no link, so it cannot be abused as a phishing template."""
    stamp = when.astimezone(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    what = "reset" if how == "reset" else "changed"
    subject = "Your EKIP password was changed"
    text = (
        f"The password for your EKIP account was {what} on {stamp}, and you were signed "
        "out of your other sessions.\n\n"
        "If this was you, no action is needed. If it was not, reset your password "
        "immediately from the sign-in page and contact your administrator.\n"
    )
    html = (
        f"<p>The password for your EKIP account was {what} on {escape(stamp)}, and you were "
        "signed out of your other sessions.</p>"
        "<p>If this was you, no action is needed. If it was not, reset your password "
        "immediately from the sign-in page and contact your administrator.</p>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def render_organization_suspended_email(
    *, to: str, organization_name: str, customer_message: str | None
) -> EmailMessage:
    """Told to an organization's administrators when its access is suspended. Contains ONLY the customer-
    facing message the operator chose to share -- never the reason or the private operator note (the
    renderer is not even given them) -- and no link."""
    org = clean_inline(organization_name)
    note = clean_inline(customer_message, 1000) if customer_message else ""
    subject = f"Access to {org} on EKIP has been suspended"
    text = (
        f"Access to the organization \"{org}\" on EKIP has been suspended. Members cannot sign in or use "
        "the organization until access is restored, and everyone has been signed out.\n\n"
        + (f"Message from EKIP:\n{note}\n\n" if note else "")
        + "Your data has not been deleted. If you have questions, contact EKIP support.\n"
    )
    html = (
        f"<p>Access to the organization <strong>{escape(org)}</strong> on EKIP has been suspended. Members "
        "cannot sign in or use the organization until access is restored, and everyone has been signed out.</p>"
        + (f"<p><strong>Message from EKIP:</strong><br>{escape(note)}</p>" if note else "")
        + "<p>Your data has not been deleted. If you have questions, contact EKIP support.</p>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)


def render_organization_reactivated_email(
    *, to: str, organization_name: str, sign_in_url: str
) -> EmailMessage:
    """Told to an organization's administrators when access is restored. Sessions did not survive the
    suspension, so everyone signs in again. No reason, no operator note."""
    org = clean_inline(organization_name)
    subject = f"Access to {org} on EKIP has been restored"
    text = (
        f"Access to the organization \"{org}\" on EKIP has been restored.\n\n"
        f"Everyone needs to sign in again: {sign_in_url}\n"
    )
    html = (
        f"<p>Access to the organization <strong>{escape(org)}</strong> on EKIP has been restored.</p>"
        f"<p>Everyone needs to <a href=\"{escape(sign_in_url, quote=True)}\">sign in again</a>.</p>"
    )
    return EmailMessage(to=to, subject=subject, text=text, html=html)
