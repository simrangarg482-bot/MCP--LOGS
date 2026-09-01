"""Small real-stack answer-quality gate for a controlled connector corpus.

This is deliberately not an LLM-as-judge benchmark. It checks reproducible
facts: expected routing/outcome, required answer terms, citation count, and
required terms in citation excerpts. A human still reviews the stored
Playwright evidence for semantic quality.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _outcome(response: dict[str, Any]) -> str:
    if response.get("route_taken") == "investigation":
        return "investigation"
    if response.get("answer_mode") == "no_answer":
        return "no_answer"
    return "answer"


def _contains_all(text: str, terms: list[str]) -> tuple[bool, list[str]]:
    lowered = text.casefold()
    missing = [term for term in terms if term.casefold() not in lowered]
    return not missing, missing


def _run_case(client: httpx.Client, token: str, case: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": case["question"]},
    )
    response.raise_for_status()
    body = response.json()
    actual_outcome = _outcome(body)
    citations = body.get("citations") or []
    answer = body.get("answer") or ""
    citation_text = "\n".join(str(citation.get("excerpt") or "") for citation in citations)

    assertions: list[dict[str, Any]] = []

    acceptable = case["acceptable_outcomes"]
    assertions.append(
        {
            "name": "outcome",
            "passed": actual_outcome in acceptable,
            "expected": acceptable,
            "actual": actual_outcome,
        }
    )

    minimum_citations = int(case.get("minimum_citations", 0))
    assertions.append(
        {
            "name": "minimum_citations",
            "passed": len(citations) >= minimum_citations,
            "expected": minimum_citations,
            "actual": len(citations),
        }
    )

    answer_ok, missing_answer_terms = _contains_all(answer, case.get("required_answer_terms", []))
    assertions.append(
        {
            "name": "required_answer_terms",
            "passed": answer_ok,
            "missing": missing_answer_terms,
        }
    )

    citations_ok, missing_citation_terms = _contains_all(
        citation_text, case.get("required_citation_terms", [])
    )
    assertions.append(
        {
            "name": "required_citation_terms",
            "passed": citations_ok,
            "missing": missing_citation_terms,
        }
    )

    return {
        "id": case["id"],
        "category": case["category"],
        "passed": all(assertion["passed"] for assertion in assertions),
        "actual_outcome": actual_outcome,
        "confidence": body.get("confidence"),
        "citation_count": len(citations),
        "assertions": assertions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--email", default="e2e-admin-orga@example.com")
    parser.add_argument("--password-env", default="EKIP_E2E_PASSWORD")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"{args.password_env} is not set; refusing to put a password on the command line.")
        return 2

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        print("Dataset must contain a non-empty 'cases' list.")
        return 2

    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.api_base.rstrip("/"), timeout=args.timeout) as client:
        login = client.post("/auth/login", json={"email": args.email, "password": password})
        login.raise_for_status()
        token = login.json()["access_token"]

        for case in cases:
            try:
                result = _run_case(client, token, case)
            except Exception as exc:  # report every case instead of losing the run
                result = {
                    "id": case.get("id", "unknown"),
                    "category": case.get("category", "unknown"),
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            print(f"{status}: {result['id']}")

    passed = sum(1 for result in results if result["passed"])
    report = {
        "dataset_version": dataset.get("version"),
        "api_base": args.api_base,
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Summary: {passed}/{len(results)} passed. Report: {args.report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
