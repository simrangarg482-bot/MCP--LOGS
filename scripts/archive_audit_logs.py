"""Export old audit history to compressed, verified files. NEVER deletes an audit row.

    python scripts/archive_audit_logs.py                       # dry run: lists the months that would be exported
    python scripts/archive_audit_logs.py --apply               # export + verify + record in audit_archive_batches
    python scripts/archive_audit_logs.py --apply --organization <uuid>
    python scripts/archive_audit_logs.py --verify              # re-check every recorded archive against its file

Needs `AUDIT_ARCHIVE_DIRECTORY` (durable storage) and `AUDIT_ARCHIVE_MIN_AGE_DAYS` (default 365: only months that
ended at least that long ago). Operator-run; nothing schedules it. See docs/operations/data-retention.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.core.audit import archive
from app.database.session import session_scope
from app.shared.config.settings import get_settings


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.audit_archive_directory is None:
        print("AUDIT_ARCHIVE_DIRECTORY is not configured; archiving needs durable storage.", file=sys.stderr)
        return 2
    organizations = [uuid.UUID(args.organization)] if args.organization else None
    if args.verify:
        from sqlalchemy import text

        async with session_scope() as session:
            ids = organizations or list((await session.execute(text("SELECT id FROM organizations"))).scalars().all())
        bad = 0
        for organization_id in ids:
            for month, ok, reason in await archive.verify_archives(session_scope, organization_id):
                print(f"{organization_id} {month:%Y-%m} {'OK' if ok else 'FAILED: ' + str(reason)}")
                bad += 0 if ok else 1
        return 1 if bad else 0
    report = await archive.run_archive(session_scope, organization_ids=organizations, settings=settings, apply=args.apply)
    for outcome in report.outcomes:
        print(f"{outcome.organization_id} {outcome.period_start:%Y-%m} {outcome.status} rows={outcome.row_count}"
              + (f" error={outcome.error}" if outcome.error else ""))
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to export.")
    return 1 if report.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="export (default is a dry run)")
    parser.add_argument("--verify", action="store_true", help="re-check recorded archives against their files")
    parser.add_argument("--organization", help="limit to one organization id")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
