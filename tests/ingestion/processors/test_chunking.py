"""Tests for `app.ingestion.processors.chunking.classify_content_type`,
specifically confirming the empirical claim behind the GitHub connector
extension: commit/pull-request/issue-shaped `RawDocument`s already
classify as `"document"` (and therefore route to the "documentation"
collection) with zero changes to this module -- `classify_content_type`
only ever branches on `source` (for chat) or a file-extension check on
`metadata["path"]`/`title`/`external_id`, and none of the three new GitHub
shapes populate a `"path"` metadata key or have a title/external_id ending
in a recognized code extension.
"""

from __future__ import annotations

from app.ingestion.processors.chunking import classify_content_type
from app.ingestion.schemas import RawDocument


def test_github_file_classifies_as_code() -> None:
    doc = RawDocument(
        source="github",
        external_id="acme/widgets:src/app.py",
        content="print('hi')",
        title="src/app.py",
        metadata={"repo": "acme/widgets", "path": "src/app.py", "ref": "main"},
    )

    assert classify_content_type(doc) == "code"


def test_github_commit_classifies_as_document() -> None:
    doc = RawDocument(
        source="github",
        external_id="acme/widgets@abc123",
        content="Fix null pointer in checkout",
        title="Fix null pointer in checkout",
        metadata={
            "repo": "acme/widgets",
            "kind": "commit",
            "sha": "abc123",
            "author": "Ada Lovelace",
            "timestamp": "2026-07-01T10:00:00Z",
            "changed_files": "src/checkout.py",
        },
    )

    assert classify_content_type(doc) == "document"


def test_github_pull_request_classifies_as_document() -> None:
    doc = RawDocument(
        source="github",
        external_id="acme/widgets#pull-42",
        content="Add retry logic\n\nRetries flaky calls.",
        title="Add retry logic",
        metadata={"repo": "acme/widgets", "kind": "pull_request", "number": "42"},
    )

    assert classify_content_type(doc) == "document"


def test_github_issue_classifies_as_document() -> None:
    doc = RawDocument(
        source="github",
        external_id="acme/widgets#issue-7",
        content="Login fails after SSO redirect\n\nUsers see a blank page.",
        title="Login fails after SSO redirect",
        metadata={"repo": "acme/widgets", "kind": "issue", "number": "7", "labels": "bug"},
    )

    assert classify_content_type(doc) == "document"


def test_github_issue_with_extension_like_title_still_classifies_as_document() -> None:
    """Guards against a plausible false positive: an issue/commit whose
    title happens to *mention* a filename would still be misclassified as
    "code" if `classify_content_type` matched on substring rather than a
    strict suffix check on the title/external_id used as a stand-in path.
    This one's title ends in something that is not, in fact, a recognized
    code extension (`.md` isn't in `_CODE_EXTENSIONS`), so it still resolves
    to "document" -- included to make the "no path metadata key means this
    can't accidentally match" claim explicit and tested, not just reasoned
    about.
    """
    doc = RawDocument(
        source="github",
        external_id="acme/widgets#issue-9",
        content="Broken link in README.md",
        title="Broken link in README.md",
        metadata={"repo": "acme/widgets", "kind": "issue", "number": "9"},
    )

    assert classify_content_type(doc) == "document"
