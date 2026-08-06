"""Application configuration, loaded from environment variables.

This is the first implementation file in the project, deliberately: almost
every other module (database connections, Redis, LLM API keys, MCP auth)
depends on settings being loaded correctly, so it has to exist before
anything else can be written meaningfully.

Owned by: shared/ (ARCHITECTURE.md section 3 -- cross-cutting, no business
meaning of its own, importable by every other module).
"""

from functools import lru_cache
from typing import Literal
from typing import ClassVar

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for runtime configuration.

    Every field here corresponds to a concrete need from a document we've
    already written:
      - database_url          -> Neon Postgres connection (DATABASE_DESIGN.md)
      - redis_url              -> ingestion job queue (ENGINEERING_DECISIONS.md #002)
      - default_vector_backend -> per-collection choice exists in
                                   ARCHITECTURE.md section 8, but the
                                   *default* backend for new collections is
                                   a global setting
      - openai_api_key      -> LLM calls in agents/ (AGENT_WORKFLOWS.md)
      - confidence_threshold   -> the routing threshold in the Confidence
                                   Evaluation Node (AGENT_WORKFLOWS.md 2.2)
                                   -- exposed as config, not hardcoded, since
                                   the exact value is still an open item in
                                   ENGINEERING_DECISIONS.md 

    Deliberately NOT included: individual connector credentials (Slack/GitHub/
    Jira tokens). Those belong to ingestion/connectors/ configuration, scoped
    per-source, not global app settings -- mixing them in here would make
    this class a dumping ground and couple core app startup to whichever
    connectors happen to be configured.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment -----------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Database (DATABASE_DESIGN.md) ------------------------------------
    database_url: PostgresDsn = Field(
        description="Neon Postgres connection string, asyncpg driver."
    )

    # --- Job queue (ENGINEERING_DECISIONS.md #002) ------------------------
    redis_url: RedisDsn = Field(
        description="Backs the arq job queue used by ingestion workers."
    )

    # --- Vector retrieval (ARCHITECTURE.md section 8) ----------------------
    default_vector_backend: Literal["pgvector", "qdrant"] = "qdrant"
    qdrant_url: str | None = Field(
        default=None,
        description="Required only if any collection uses the qdrant backend.",
    )

    # --- LLM (AGENT_WORKFLOWS.md; ENGINEERING_DECISIONS.md #008) -----------
    openai_api_key: str = Field(description="Used by all agent LLM calls.")
    agent_llm_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "OpenAI chat model used by every LLM-calling agent node (query "
            "rewriting, Answer Agent generation, hypothesis generation). A "
            "single global setting, not one per node -- no node in "
            "AGENT_WORKFLOWS.md has a documented reason to use a different "
            "model than any other yet; split this into per-node settings if "
            "one ever does."
        ),
    )

    # --- Agent behavior (AGENT_WORKFLOWS.md 2.2) ---------------------------
    confidence_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
    )

    # --- Auth (API_DESIGN.md section 1) -------------------------------------
    jwt_secret_key: str = Field(description="Signs/verifies session tokens.")
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    REFRESH_TOKEN_EXPIRY_DAYS: ClassVar[int] = 30

    # --- Investigation Agent live evidence (AGENT_WORKFLOWS.md 2.4's hybrid
    # evidence-gathering extension -- agents/investigation/live/) -----------
    investigation_live_evidence_enabled: bool = Field(
        default=True,
        description=(
            "Global kill-switch for the Investigation Agent's live GitHub/"
            "Slack lookups (agents/investigation/live/). Set False to fall "
            "back to indexed-only evidence gathering without a code change "
            "-- e.g. if live external API calls start tripping rate limits "
            "or add unacceptable latency in production."
        ),
    )
    investigation_live_evidence_lookback_hours: int = Field(
        default=24,
        ge=1,
        description=(
            "How far back (from now) a live evidence source searches for "
            "recent commits/PRs/issues/messages, independent of the "
            "hourly ingestion reconciliation cadence (app.ingestion.workers."
            "main.scheduled_reconciliation) -- live evidence's job is "
            "covering the gap between the last sync and right now, not "
            "re-walking a source's whole history."
        ),
    )

    # --- Knowledge Gap Agent (AGENT_WORKFLOWS.md 2.6 / PROJECT_PLAN.md 6.6,
    # app/agents/knowledge_gap/) ---------------------------------------------
    knowledge_gap_lookback_days: int = Field(
        default=14,
        ge=1,
        description=(
            "How far back the Knowledge Gap Agent looks for low-confidence "
            "`answer_question` executions when clustering for repeated gaps."
        ),
    )
    knowledge_gap_min_cluster_size: int = Field(
        default=3,
        ge=2,
        description=(
            "A cluster of similar low-confidence queries must reach this "
            "size before it's surfaced as a `GapReport` -- distinguishes a "
            "genuinely repeated gap from a one-off hard question "
            "(AGENT_WORKFLOWS.md: 'repeated gaps rather than one-off "
            "low-confidence queries')."
        ),
    )
    knowledge_gap_similarity_threshold: float = Field(
        default=0.82,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine-similarity threshold for joining a query to an existing "
            "cluster (app.agents.knowledge_gap.clustering.cluster_by_"
            "similarity) -- resolves AGENT_WORKFLOWS.md's previously-open "
            "'clustering method/threshold ... not yet decided' item in "
            "favor of similarity-threshold (leader) clustering over k-means; "
            "see that module's docstring for the full reasoning."
        ),
    )

    # --- Ingestion rate limiting (PROJECT_PLAN.md sections 4.5/10,
    # app/ingestion/rate_limiter.py) ------------------------------------------
    ingestion_org_max_requests_per_second: float = Field(
        default=5.0,
        gt=0.0,
        description=(
            "Aggregate requests/second budget shared across every "
            "connector_config belonging to one organization -- the 'per "
            "tenant' half of section 4.5's 'per connector, per tenant' "
            "rate-limiting requirement. Independent of (and in addition to) "
            "each individual connector's own declared `requests_per_second` "
            "ceiling; see `app.ingestion.rate_limiter`'s module docstring."
        ),
    )

    # --- Secret management (PROJECT_PLAN.md section 12.5, Milestone 10) -----
    connector_secret_master_key: str = Field(
        description=(
            "Hex-encoded 32-byte AES key -- the key-encryption-key (KEK) "
            "`app.shared.security.kms.LocalKeyManagementService` uses to wrap "
            "each per-secret data-encryption-key (DEK). This is a platform "
            "secret (like `jwt_secret_key` above), injected from the "
            "environment/secrets-manager, never committed -- it is NOT a "
            "per-tenant connector credential itself. `LocalKeyManagementService` "
            "is explicitly a pre-production stand-in for a real cloud KMS "
            "(AWS KMS / GCP Cloud KMS / Azure Key Vault); see that class's "
            "own docstring for exactly what swapping to one would change "
            "(the `KeyManagementService` protocol, not any caller)."
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    Using a cached function rather than a module-level singleton keeps this
    override-able in tests (pytest fixtures can call
    `get_settings.cache_clear()` and monkeypatch environment variables
    per-test) without every other module needing to know that trick exists.
    """
    return Settings()