"""Operator tool: grant, revoke or list EKIP platform operators.

This is the ONLY way a platform operator comes into existence. There is deliberately no API for it, and
holding a tenant permission (`tenancy:manage` included) grants nothing here.

It connects with the OWNER / migration credential (`MIGRATION_DATABASE_URL`), because the application role
has no access to `platform_operators` at all. That is also why it must be run by a person with that
credential, on purpose.

Safe by default
---------------
* DRY RUN unless `--apply` is given: shows exactly what would change, writes nothing.
* `--apply` additionally requires `--yes` and `--reason "<why>"`. The target database (host and name,
  never the password) is printed first, and the reason is stored in the platform audit trail.
* Refuses unless the target user already exists, is active and has a verified email. It never creates
  accounts, never sets passwords, never touches tenant roles.
* Granting an existing operator is refused (use `revoke` first); revoking is a soft delete (`revoked_at`),
  so the history of who held access stays intact.
* Granting a row does NOT switch the platform surface on: that needs `PLATFORM_ADMIN_ENABLED=true`, which
  production refuses to boot with until operator MFA exists.

Usage::

    python scripts/platform_operator.py list
    python scripts/platform_operator.py grant  --email ops@example.com --role admin
    python scripts/platform_operator.py grant  --email ops@example.com --role admin \
        --apply --yes --reason "ticket OPS-123: on-call platform admin"
    python scripts/platform_operator.py revoke --email ops@example.com \
        --apply --yes --reason "left the team"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database.session import _build_engine
from app.shared.config.settings import get_settings

# The platform audit trail records the acting user. A CLI action has no application user, so it is recorded
# under this fixed nil identity, with the operating-system user and the reason in `details`.
CLI_ACTOR = uuid.UUID(int=0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
        p.add_argument("--yes", action="store_true", help="required together with --apply")
        p.add_argument("--reason", help="why (required with --apply; stored in the audit trail)")

    sub.add_parser("list", help="show current and past operators (read-only)")
    grant = sub.add_parser("grant", help="make an existing user a platform operator")
    grant.add_argument("--email", required=True)
    grant.add_argument("--role", choices=["admin", "viewer"], default="viewer")
    common(grant)
    revoke = sub.add_parser("revoke", help="revoke an operator's access")
    revoke.add_argument("--email", required=True)
    common(revoke)

    args = parser.parse_args(argv)
    if args.command != "list" and args.apply and not (args.yes and (args.reason or "").strip()):
        parser.error("--apply requires --yes and a non-empty --reason")
    return args


def target_description(url: str) -> str:
    parsed = make_url(url)
    return f"host={parsed.host} port={parsed.port} name={parsed.database} user={parsed.username}"


def _owner_url() -> str:
    settings = get_settings()
    url = settings.migration_database_url
    if url is None:
        raise SystemExit(
            "MIGRATION_DATABASE_URL (the owner credential) is required: the application role cannot "
            "write platform_operators, by design."
        )
    return str(url)


async def _find_user(conn: AsyncConnection, email: str):
    return (
        await conn.execute(
            text(
                "SELECT id, email, is_active, email_verified_at IS NOT NULL AS verified "
                "FROM users WHERE lower(email) = lower(:e) ORDER BY created_at LIMIT 1"
            ),
            {"e": email.strip()},
        )
    ).first()


async def _audit(conn: AsyncConnection, action: str, target: uuid.UUID, details: dict) -> None:
    import json

    await conn.execute(
        text(
            "INSERT INTO platform_audit_events (actor_user_id, action, details) "
            "VALUES (:a, :action, CAST(:d AS jsonb))"
        ),
        {"a": CLI_ACTOR, "action": action, "d": json.dumps({**details, "target_user_id": str(target)})},
    )


async def run(args: argparse.Namespace, *, url: str | None = None, out=print) -> int:
    url = url or _owner_url()
    out(f"Target database: {target_description(url)}")
    out("Mode: APPLY" if getattr(args, "apply", False) else "Mode: DRY RUN (nothing will be written)")
    engine = _build_engine(url)
    try:
        async with engine.begin() as conn:
            if args.command == "list":
                rows = (
                    await conn.execute(
                        text(
                            "SELECT u.email, o.role, o.granted_at, o.revoked_at FROM platform_operators o "
                            "JOIN users u ON u.id = o.user_id ORDER BY o.granted_at"
                        )
                    )
                ).all()
                out(f"{len(rows)} operator record(s):")
                for email, role, granted, revoked in rows:
                    out(f"  - {email}  role={role}  granted={granted:%Y-%m-%d}  "
                        f"{'REVOKED ' + format(revoked, '%Y-%m-%d') if revoked else 'active'}")
                return 0

            user = await _find_user(conn, args.email)
            if user is None:
                out(f"REFUSED: no user with email {args.email!r}. This tool never creates accounts.")
                return 2
            uid, email, is_active, verified = user
            existing = (
                await conn.execute(
                    text("SELECT role, revoked_at FROM platform_operators WHERE user_id = :u"), {"u": uid}
                )
            ).first()

            if args.command == "grant":
                if not is_active or not verified:
                    out("REFUSED: the account must be active and have a verified email address.")
                    return 2
                if existing is not None:
                    out(
                        "REFUSED: this user already has an operator record "
                        f"(role={existing[0]}, {'revoked' if existing[1] else 'active'}). "
                        "Revoke it first; a revoked grant is never silently re-enabled."
                    )
                    return 2
                out(f"Would grant platform role '{args.role}' to {email} ({uid}).")
                if not args.apply:
                    out("Dry run: re-run with --apply --yes --reason \"...\" to make this change.")
                    return 0
                await conn.execute(
                    text("INSERT INTO platform_operators (user_id, role, granted_by) VALUES (:u, :r, NULL)"),
                    {"u": uid, "r": args.role},
                )
                await _audit(
                    conn, "platform_operator.granted", uid,
                    {"role": args.role, "reason": args.reason.strip(), "cli_user": getpass.getuser()},
                )
                out(f"Granted '{args.role}' to {email}. Reason: {args.reason.strip()}")
                return 0

            # revoke
            if existing is None or existing[1] is not None:
                out("REFUSED: this user is not an active platform operator.")
                return 2
            out(f"Would revoke platform access of {email} ({uid}); their tenant access is unaffected.")
            if not args.apply:
                out("Dry run: re-run with --apply --yes --reason \"...\" to make this change.")
                return 0
            await conn.execute(
                text("UPDATE platform_operators SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"),
                {"u": uid},
            )
            await _audit(
                conn, "platform_operator.revoked", uid,
                {"reason": args.reason.strip(), "cli_user": getpass.getuser()},
            )
            out(f"Revoked platform access of {email}. Reason: {args.reason.strip()}")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
