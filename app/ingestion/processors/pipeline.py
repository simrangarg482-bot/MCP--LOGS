"""Orchestrates the three Document Processing stages (cleaning, metadata
extraction, chunking) into one call, per PROJECT_PLAN.md section 4.6.

Owned by: ingestion/processors/. This is the single entry point the worker
(`ingestion/service.py`) calls per fetched `RawDocument`. It does no
persistence itself (no database access here, matching every other
processors/ module) and does not call retrieval either; it only produces the
`ProcessedDocument` the worker then persists and hands off to
`retrieval.service.upsert`.
"""

from __future__ import annotations

from app.ingestion.processors.chunking import chunk_document, classify_content_type
from app.ingestion.processors.cleaning import clean_content
from app.ingestion.processors.metadata import compute_content_hash, extract_metadata
from app.ingestion.schemas import ProcessedDocument, RawDocument


def process_document(raw_document: RawDocument) -> ProcessedDocument:
    """Run `raw_document` through cleaning, metadata extraction, and
    content-type-aware chunking, in that order.

    Chunking runs against the *cleaned* content, and `content_hash` is
    computed from it too -- see `metadata.compute_content_hash`'s docstring
    for why the idempotency key deliberately ignores noise `clean_content`
    already strips.
    """
    cleaned_content = clean_content(raw_document.content)
    content_hash = compute_content_hash(cleaned_content)
    metadata_entries = extract_metadata(raw_document)
    content_type = classify_content_type(raw_document)
    chunks = chunk_document(cleaned_content, content_type)

    return ProcessedDocument(
        source=raw_document.source,
        external_id=raw_document.external_id,
        content_hash=content_hash,
        title=raw_document.title,
        source_url=raw_document.source_url,
        content_type=content_type,
        metadata_entries=metadata_entries,
        chunks=chunks,
    )
