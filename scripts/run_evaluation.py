"""CLI entry point for `app.evaluation` -- runs one or more dataset files
through `EvaluationRunner` and prints/writes a report.

Mirrors `scripts/eval_confidence.py`'s own CLI conventions (argparse,
`--report-path`, printing to stdout as it goes) for consistency with this
project's existing operational scripts, even though this script's default
mode needs no live database or API key at all -- see `app.evaluation`'s
package docstring for how this complements, rather than duplicates,
`eval_confidence.py`.

RUN
    python scripts/run_evaluation.py    # all 6 shipped fixture datasets, deterministic mode
    python scripts/run_evaluation.py --dataset app/evaluation/fixtures/retrieval_core_v1.jsonl
    python scripts/run_evaluation.py --report-path scripts/eval_report.json
    python scripts/run_evaluation.py --calibration-from scripts/eval_confidence_report.json

EXIT CODE -- what CI actually gates on
    Exit 0 only when every case behaved exactly as its dataset predicted;
    non-zero on any *regression*. A regression is one of three things, all
    equally build-breaking (`EvaluationResult.regression_kind`):

      unexpected_failure  -- an `expected_outcome="pass"` case failed.
      unexpected_pass     -- an `expected_outcome="fail"` negative control
                             passed, i.e. a check built to catch a specific
                             defect has stopped catching it.
      wrong_failure_stage -- a negative control failed somewhere other than
                             the stage it was pinned to, so it is no longer
                             testing what it was written to test.

    Crucially NOT "exit non-zero if any case failed": this suite ships
    deliberate negative controls that are *supposed* to fail, and gating on
    raw failure count would make CI permanently red -- with the obvious
    "fix" being to delete exactly the cases that prove the evaluator
    detects anything. A dataset validation error, a runner crash, or an
    unwritable report path also exit non-zero (they mean the gate did not
    actually run, which must never read as success).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.evaluation.adapters.eval_confidence_report import load_calibration_pairs  # noqa: E402
from app.evaluation.datasets.loader import load_datasets  # noqa: E402
from app.evaluation.fixtures.canned_generations import (  # noqa: E402
    CANNED_ANSWERS,
    CANNED_CRITIQUES,
    CANNED_INVESTIGATIONS,
)
from app.evaluation.fixtures.corpus import CORPUS  # noqa: E402
from app.evaluation.fixtures.graph_corpus import ENTITIES_BY_LABEL, GRAPH_EDGES  # noqa: E402
from app.evaluation.fixtures.memory_corpus import MEMORY_CORPUS  # noqa: E402
from app.evaluation.fixtures.proactive_corpus import EVIDENCE, FINDINGS  # noqa: E402
from app.evaluation.metrics.confidence import compute_calibration  # noqa: E402
from app.evaluation.reporting.console import render_console_report  # noqa: E402
from app.evaluation.reporting.json_report import write_json_report  # noqa: E402
from app.evaluation.runner import build_deterministic_runner  # noqa: E402

_FIXTURES_DIR = _REPO_ROOT / "app" / "evaluation" / "fixtures"
_DEFAULT_DATASETS = [
    _FIXTURES_DIR / "retrieval_core_v1.jsonl",
    _FIXTURES_DIR / "grounding_core_v1.jsonl",
    _FIXTURES_DIR / "answer_core_v1.jsonl",
    _FIXTURES_DIR / "investigation_core_v1.jsonl",
    _FIXTURES_DIR / "memory_core_v1.jsonl",
    _FIXTURES_DIR / "graph_core_v1.jsonl",
    _FIXTURES_DIR / "proactive_core_v1.jsonl",
]
_DEFAULT_REPORT_PATH = _REPO_ROOT / "scripts" / "run_evaluation_report.json"


async def _run(args: argparse.Namespace) -> int:
    dataset_paths = [Path(p) for p in args.dataset] if args.dataset else _DEFAULT_DATASETS
    datasets = load_datasets(dataset_paths)

    runner = build_deterministic_runner(
        CORPUS,
        CANNED_ANSWERS,
        CANNED_INVESTIGATIONS,
        memory_corpus=MEMORY_CORPUS,
        graph_entities=ENTITIES_BY_LABEL,
        graph_edges=GRAPH_EDGES,
        proactive_findings=FINDINGS,
        proactive_evidence=EVIDENCE,
        canned_critiques=CANNED_CRITIQUES,
    )
    report = await runner.run_datasets(datasets, combined_name="ekip_core")

    if args.calibration_from:
        pairs = load_calibration_pairs(args.calibration_from)
        if pairs:
            live_calibration = compute_calibration(pairs)
            print(
                f"\n(Calibration from real production data at {args.calibration_from}, "
                f"n={live_calibration.sample_count}, "
                f"error={live_calibration.overall_calibration_error})",
                flush=True,
            )

    print(render_console_report(report), flush=True)
    # Written before the verdict is returned, and outside any `if` -- a
    # regressed run is exactly when the report artifact is most worth
    # having, so it must never be skipped on failure.
    write_json_report(report, args.report_path)
    print(f"\nFull report written to: {args.report_path}", flush=True)

    if report.is_clean:
        print(
            f"\nEVALUATION CLEAN: {report.total} cases, all behaved as predicted "
            f"({report.passed_count} passed, {report.expected_failure_count} negative "
            "controls correctly detected).",
            flush=True,
        )
        return 0

    print(
        f"\nEVALUATION REGRESSED: {len(report.regressions)} case(s) defied expectation "
        f"({len(report.unexpected_failures)} unexpected failure(s), "
        f"{len(report.unexpected_passes)} unexpected pass(es), "
        f"{len(report.wrong_stage_failures)} wrong-stage failure(s)). "
        "See the REGRESSIONS section above.",
        flush=True,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the EKIP evaluation harness (retrieval/grounding/answer/investigation)."
    )
    parser.add_argument(
        "--dataset",
        action="append",
        help="path to a .jsonl dataset file (repeatable). Default: all 6 shipped fixture datasets.",
    )
    parser.add_argument("--report-path", type=Path, default=_DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--calibration-from",
        type=Path,
        default=None,
        help=(
            "optional path to a scripts/eval_confidence.py-produced report.json -- adds a "
            "real-production-data calibration summary alongside this run's fixture-based one"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
