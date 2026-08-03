"""Tests for `app.agents.investigation.evidence._chunk_to_evidence`'s
kind -> source mapping -- the piece that lets the Investigation Agent tell
a GitHub file chunk apart from a commit/pull-request/issue chunk, now that
`retrieval.search(..., include_metadata=True)` can surface each chunk's
`document_metadata` (see `ingestion.connectors.github`'s module docstring
for the `"kind"` metadata key convention this relies on).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.agents.investigation.evidence import _chunk_to_evidence, _parse_source_timestamp
from app.retrieval.schemas import ScoredChunk


def _chunk(metadata: dict[str, str], content: str = "some content") -> ScoredChunk:
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        collection="code",
        content=content,
        score=0.9,
        source_offset_start=0,
        source_offset_end=len(content),
        title="a title",
        source_url="https://github.com/acme/widgets/commit/abc123",
        metadata=metadata,
    )


def test_file_chunk_with_no_kind_defaults_to_github() -> None:
    chunk = _chunk({"repo": "acme/widgets", "path": "src/app.py", "ref": "main"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "github"
    assert evidence.metadata == chunk.metadata
    assert evidence.source_timestamp is None  # no "timestamp" key on a file chunk


def test_commit_chunk_maps_to_commit_source() -> None:
    chunk = _chunk(
        {
            "repo": "acme/widgets",
            "kind": "commit",
            "sha": "abc123",
            "author": "Ada Lovelace",
            "timestamp": "2026-07-01T10:00:00Z",
            "changed_files": "src/checkout.py",
        }
    )

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "commit"
    assert evidence.metadata["sha"] == "abc123"
    assert evidence.source_timestamp == datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_pull_request_chunk_maps_to_pull_request_source() -> None:
    chunk = _chunk({"repo": "acme/widgets", "kind": "pull_request", "number": "42"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "pull_request"


def test_issue_chunk_maps_to_issue_source() -> None:
    chunk = _chunk({"repo": "acme/widgets", "kind": "issue", "number": "7", "labels": "bug"})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.source == "issue"
    assert evidence.metadata["labels"] == "bug"


def test_explicit_source_override_wins_over_kind_metadata() -> None:
    """`_gather_slack_evidence` always passes `source="slack"` explicitly --
    confirms that override takes priority even if `metadata["kind"]` were
    somehow present (it never is for real Slack chunks).
    """
    chunk = _chunk({"kind": "commit"})

    evidence = _chunk_to_evidence(chunk, source="slack")

    assert evidence.source == "slack"


def test_reference_falls_back_to_chunk_id_without_source_url() -> None:
    chunk = _chunk({})
    chunk = chunk.model_copy(update={"source_url": None})

    evidence = _chunk_to_evidence(chunk)

    assert evidence.reference == f"chunk:{chunk.chunk_id}"


def test_summary_is_truncated_and_marked_with_ellipsis() -> None:
    long_content = "x" * 500
    chunk = _chunk({}, content=long_content)

    evidence = _chunk_to_evidence(chunk)

    assert len(evidence.summary) == 303  # 300 chars + "..."
    assert evidence.summary.endswith("...")


def test_parse_source_timestamp_handles_missing_and_malformed_values() -> None:
    assert _parse_source_timestamp(None) is None
    assert _parse_source_timestamp("") is None
    assert _parse_source_timestamp("not-a-timestamp") is None
    assert _parse_source_timestamp("2026-07-01T10:00:00Z") == datetime(
        2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc
    )
