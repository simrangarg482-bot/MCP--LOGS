"""The Mode 1 synthetic corpus: one small, coherent incident scenario (an
auth-service outage after a deployment shrank its DB connection pool) served
by `FixtureRetrievalAdapter`. See package docstring.

Each document has a short, readable label (`"incident-123"`, `"deploy-456"`,
...) and a deterministic UUID derived from it (`stable_uuid`, in this
package's `__init__.py`). The runner compares a case's expected relevant/
required-evidence ids against `str(chunk.document_id)`, so JSONL datasets
must reference documents by the *resolved UUID string*, not the bare label
-- `document_id_for_label(label)` below returns exactly that string, and is
what a dataset-authoring script (or a human copying from this file) should
call rather than hand-deriving the UUID.
"""

from __future__ import annotations

from app.evaluation.fixtures import stable_uuid
from app.retrieval.schemas import ScoredChunk

#: One permission code, used by exactly one restricted chunk below, to
#: exercise the "same query, different permissions, different result"
#: requirement (see `app.evaluation.adapters.retrieval._visible`).
_SECURITY_READ_PERMISSION = "security:read"


def document_id_for_label(label: str) -> str:
    """The string form of a fixture document's id -- what dataset JSONL
    files should put in `relevant_document_ids`/`required_evidence_ids`."""
    return str(stable_uuid(label))


def _chunk(
    *,
    label: str,
    collection: str,
    content: str,
    title: str,
    acl_permission_code: str | None = None,
) -> ScoredChunk:
    document_id = stable_uuid(label)
    metadata = {"acl_permission_code": acl_permission_code} if acl_permission_code else {}
    return ScoredChunk(
        chunk_id=stable_uuid(f"{label}:chunk"),
        document_id=document_id,
        collection=collection,  # type: ignore[arg-type]
        content=content,
        score=0.0,  # placeholder -- FixtureRetrievalAdapter recomputes this per query
        source_offset_start=0,
        source_offset_end=len(content),
        title=title,
        source_url=f"https://example.invalid/{label}",
        metadata=metadata,
    )


CORPUS: list[ScoredChunk] = [
    _chunk(
        label="incident-123",
        collection="documentation",
        title="Incident 123: Auth service outage",
        content=(
            "Incident 123: the authentication service failed after deployment on "
            "2026-08-01. Root cause: the database connection pool was exhausted "
            "after the new deployment reduced the max connection pool size from "
            "100 to 10, causing authentication requests to fail with timeout "
            "errors."
        ),
    ),
    _chunk(
        label="deploy-456",
        collection="documentation",
        title="Deployment 456 changelog",
        content=(
            "Deployment 456 changed the auth-service configuration: DB_POOL_SIZE "
            "was reduced from 100 to 10 to save memory. Deployed 2026-08-01 "
            "14:00 UTC."
        ),
    ),
    _chunk(
        label="runbook-auth-001",
        collection="documentation",
        title="Runbook: auth service failures after deployment",
        content=(
            "Runbook: if the authentication service fails shortly after a "
            "deployment, check the database connection pool size configuration "
            "first. Restore DB_POOL_SIZE to a safe value and redeploy."
        ),
    ),
    _chunk(
        label="slack-thread-789",
        collection="conversations",
        title="Slack: #incidents",
        content=(
            "alice: auth service is throwing 500s since the last deploy. "
            "bob: looks like database connections are maxed out, might be the "
            "pool size change from deployment 456."
        ),
    ),
    _chunk(
        label="code-auth-service",
        collection="code",
        title="auth_service/db.py",
        content=(
            "def get_db_pool():\n"
            "    return create_pool(size=settings.DB_POOL_SIZE)  # DB_POOL_SIZE "
            "defaults to 10 since the deployment 456 config change"
        ),
    ),
    _chunk(
        label="unrelated-billing-doc",
        collection="documentation",
        title="Billing provider migration",
        content=(
            "The billing platform migrated to a new payment vendor in Q1, updating "
            "invoicing templates and currency rounding rules for international "
            "customers."
        ),
    ),
    _chunk(
        label="restricted-security-plan",
        collection="documentation",
        title="Confidential: auth breach response plan",
        content=(
            "Confidential: security incident response plan for authentication "
            "service breaches -- database connection pool exhaustion is listed "
            "as a known denial-of-service vector."
        ),
        acl_permission_code=_SECURITY_READ_PERMISSION,
    ),
]
