"""Human-readable console rendering of an `EvaluationReport`, matching this
package's spec's example layout. Returns the rendered string rather than
printing directly, so callers (the CLI, tests) choose whether/where to print
it -- `scripts/eval_confidence.py`'s own functions print directly since it
has no other consumer; this package's reporting is reused by both the CLI
and its own test suite, so returning a string is the more testable choice.
"""

from __future__ import annotations

from app.evaluation.schemas import EvaluationReport

_RULE = "=" * 60


def render_console_report(report: EvaluationReport) -> str:
    lines: list[str] = []
    lines.append("EKIP Evaluation Report")
    lines.append(_RULE)
    lines.append("")
    lines.append(f"Dataset: {report.dataset_name}")
    lines.append(f"Version: {report.dataset_version}")
    lines.append(f"Mode: {report.mode}")
    if report.git_commit:
        lines.append(f"Commit: {report.git_commit}")
    if report.model_provider or report.model_name:
        lines.append(f"Model: {report.model_provider or '?'}/{report.model_name or '?'}")
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append("")
    lines.append(f"Total cases: {report.total}")
    lines.append(f"Passed: {report.passed_count}")
    lines.append(f"Failed: {report.failed_count}")
    lines.append(
        f"  of which negative controls failing as designed: {report.expected_failure_count}"
    )
    lines.append(f"Regressions (unexpected behavior): {len(report.regressions)}")
    lines.append(f"VERDICT: {'CLEAN' if report.is_clean else 'REGRESSED'}")

    by_category: dict[str, list] = {}
    for result in report.results:
        by_category.setdefault(result.category, []).append(result)

    for category, results in sorted(by_category.items()):
        lines.append("")
        lines.append(category.capitalize())
        lines.append("-" * len(category))
        passed = sum(1 for r in results if r.passed)
        as_expected = sum(1 for r in results if r.matched_expectation)
        lines.append(f"{passed}/{len(results)} passed, {as_expected}/{len(results)} as expected")

    if report.aggregate_metrics:
        lines.append("")
        lines.append("Aggregate metrics")
        lines.append("-" * 17)
        for name, metric in sorted(report.aggregate_metrics.items()):
            value_str = f"{metric.value:.3f}" if metric.value is not None else "n/a"
            lines.append(f"{name}: {value_str}")

    if report.calibration is not None:
        lines.append("")
        lines.append("Confidence calibration")
        lines.append("-" * 23)
        error = report.calibration.overall_calibration_error
        if error is not None:
            lines.append(f"Calibration error: {error:.3f}")
        else:
            lines.append("Calibration error: n/a")
        lines.append(f"Samples: {report.calibration.sample_count}")
        for bucket in report.calibration.buckets:
            if bucket.count == 0:
                continue
            lines.append(
                f"  [{bucket.range_low:.1f}-{bucket.range_high:.1f}) "
                f"n={bucket.count} predicted={bucket.mean_predicted_confidence:.3f} "
                f"actual={bucket.actual_success_rate:.3f} gap={bucket.calibration_gap:.3f}"
            )

    expected_failures = [
        r for r in report.failures if r.expected_outcome == "fail" and r.matched_expectation
    ]
    if expected_failures:
        lines.append("")
        lines.append("Negative controls detected as designed (not build failures)")
        lines.append("-" * 58)
        for result in expected_failures:
            lines.append(f"- {result.case_id} [{result.failure.stage}]")
            lines.append(f"  Reason: {result.failure.reason}")

    if report.regressions:
        lines.append("")
        lines.append("REGRESSIONS (these fail the build)")
        lines.append("-" * 34)
        for result in report.unexpected_failures:
            lines.append(f"- {result.case_id}: UNEXPECTED FAILURE [{result.failure.stage}]")
            lines.append("  Expected this case to pass; it failed.")
            lines.append(f"  Reason: {result.failure.reason}")
        for result in report.unexpected_passes:
            lines.append(f"- {result.case_id}: UNEXPECTED PASS")
            lines.append(
                "  This is a negative control -- it is supposed to be caught failing. "
                "It passed, so the check it guards has stopped detecting anything."
            )
        for result in report.wrong_stage_failures:
            lines.append(f"- {result.case_id}: WRONG FAILURE STAGE")
            lines.append(
                f"  Expected to fail at '{result.expected_failure_stage}', "
                f"actually failed at '{result.failure.stage}'."
            )
            lines.append(f"  Reason: {result.failure.reason}")

    return "\n".join(lines)
