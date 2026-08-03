"""Document Processing stage 2: turn a `RawDocument`'s free-form `metadata`
dict into persistable `DocumentMetadataEntry` rows, plus compute the content
hash that backs `documents`' idempotency key (DATABASE_DESIGN.md).

Owned by: ingestion/processors/. Per PROJECT_PLAN.md section 4.6's
"Document Processing" stage: "strip noise, extract metadata (author,
timestamp, source URL), attach organization_id + project_id".
`organization_id`/`project_id` are deliberately NOT attached here -- they
end up as direct columns on the `documents` row itself
(`ingestion_models.Document`), not EAV `document_metadata` entries, so
attaching them is the repository/service layer's job (task #12) when it
persists a `Document`, not this module's.
"""

from __future__ import annotations

import hashlib

from app.ingestion.schemas import DocumentMetadataEntry, RawDocument


def extract_metadata(raw_document: RawDocument) -> list[DocumentMetadataEntry]:
    """Convert a `RawDocument`'s free-form metadata dict into the EAV rows
    `document_metadata` stores.

    A direct, lossless mapping -- connectors already produce
    source-appropriate keys (`SlackConnector`: `channel_id`/`user`/
    `thread_ts`; `GitHubConnector`: `repo`/`path`/`ref`), so there is no
    additional normalization to do beyond converting dict entries into the
    row shape. Sorted by key for a stable, deterministic ordering (makes
    diffing two versions of the same document's metadata in a test or a log
    meaningful).
    """
    return [
        DocumentMetadataEntry(key=key, value=value)
        for key, value in sorted(raw_document.metadata.items())
    ]


def compute_content_hash(cleaned_content: str) -> str:
    """SHA-256 of the *cleaned* content -- the idempotency key ingredient
    from DATABASE_DESIGN.md (`documents` unique constraint on
    `(organization_id, source, external_id, content_hash)`).

    Hashes the cleaned content, not the connector's raw content: two fetches
    whose only difference is whitespace/HTML noise that `clean_content`
    already normalizes away should hash identically and be treated as
    unchanged, not spuriously version-bumped.
    """
    return hashlib.sha256(cleaned_content.encode("utf-8")).hexdigest()
