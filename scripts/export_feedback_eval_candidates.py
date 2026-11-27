"""Export evaluation CANDIDATES from one organization's user feedback, and --
only on an explicit second step -- promote the human-APPROVED ones.

    # 1. Export (read-only against the database). Writes/merges a candidate file.
    python scripts/export_feedback_eval_candidates.py <organization_id> --out feedback_candidates.json

    # 2. A person edits the file: for each candidate they trust, they set
    #    status=approved plus reviewer_id, reviewed_at, category,
    #    expected_route, expected_answer, rationale and evidence (verbatim
    #    quotes from the live corpus). Review it in a pull request.

    # 3. Promote approved candidates into the confidence dataset (file edit only).
    python scripts/export_feedback_eval_candidates.py --promote feedback_candidates.json \
        --dataset scripts/eval_confidence_dataset.json

Nothing is ever promoted automatically: `proposed` and `rejected` candidates
are ignored by step 3, and `approved` ones that lack a reviewer or an expected
answer fail validation. Re-running step 1 merges new candidates in and never
overwrites an existing one, so review work survives. See
`app.evaluation.feedback_candidates`.

Step 1 needs a database (`DATABASE_URL`); step 3 does not touch one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.evaluation import feedback_candidates as fc


async def _export(organization_id: uuid.UUID, out_path: Path, window_days: int) -> int:
    from app.agents.feedback import patterns, repository
    from app.core.exceptions import OrganizationSuspendedError
    from app.core.tenancy import service as tenancy_service
    from app.database.session import session_scope, set_tenant_context
    from app.retrieval import embedding
    from app.shared.config.settings import get_settings

    async with session_scope() as session:
        try:
            await tenancy_service.assert_organization_processing_allowed(session, organization_id)
        except OrganizationSuspendedError:
            print(f"Organization {organization_id} is suspended; nothing exported.")
            return 2
        await set_tenant_context(session, organization_id)
        since = datetime.now(UTC) - timedelta(days=window_days)
        rows = await repository.list_feedback_with_executions(
            session, organization_id=organization_id, since=since, limit=500
        )
        found = await patterns.identify_patterns(
            rows,
            embed=embedding.embed_texts,
            similarity_threshold=get_settings().knowledge_gap_similarity_threshold,
        )

    fresh = fc.candidates_from_patterns(found)
    existing = fc.load_candidate_file(out_path).candidates if out_path.exists() else []
    merged = fc.merge_candidates(existing, fresh)
    fc.save_candidate_file(out_path, fc.new_candidate_file(merged))
    print(
        f"{len(found)} pattern(s) found; {len(merged) - len(existing)} new candidate(s) added; "
        f"{len(merged)} total in {out_path}. All new candidates are 'proposed' -- a person must review them."
    )
    return 0


def _promote(candidates_path: Path, dataset_path: Path) -> int:
    candidate_file = fc.load_candidate_file(candidates_path)  # validates every candidate
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset, added = fc.promote_into_dataset(dataset, candidate_file)
    if added:
        dataset_path.write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")
    print(f"{added} approved candidate(s) added to {dataset_path}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("organization_id", nargs="?", help="organization to export from")
    parser.add_argument("--out", default="feedback_candidates.json", type=Path)
    parser.add_argument("--window-days", default=30, type=int)
    parser.add_argument("--promote", type=Path, help="candidate file whose APPROVED entries to promote")
    parser.add_argument("--dataset", type=Path, default=Path("scripts/eval_confidence_dataset.json"))
    args = parser.parse_args()

    if args.promote:
        return _promote(args.promote, args.dataset)
    if not args.organization_id:
        parser.error("organization_id is required unless --promote is given")
    return asyncio.run(_export(uuid.UUID(args.organization_id), args.out, args.window_days))


if __name__ == "__main__":
    sys.exit(main())
