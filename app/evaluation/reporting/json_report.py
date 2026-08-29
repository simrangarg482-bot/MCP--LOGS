"""Machine-readable JSON rendering of an `EvaluationReport`.

`EvaluationReport` is already a Pydantic model, so this is a thin wrapper
over `model_dump_json` -- kept as its own function (rather than callers
using `report.model_dump_json()` directly) so the on-disk shape has one
named, documented entry point that can absorb future formatting concerns
(e.g. a schema-version envelope) without every caller needing to change.
"""

from __future__ import annotations

from pathlib import Path

from app.evaluation.schemas import EvaluationReport


def render_json_report(report: EvaluationReport) -> str:
    return report.model_dump_json(indent=2)


def write_json_report(report: EvaluationReport, path: str | Path) -> None:
    Path(path).write_text(render_json_report(report), encoding="utf-8")
