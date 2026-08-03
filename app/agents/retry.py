"""Shared retry/backoff helper for LangGraph nodes (AGENT_WORKFLOWS.md
section 4, "applies across all nodes"): up to 2 retries per node with
exponential backoff, tracked in `GraphState.retry_count[node_name]`.

Owned by: agents/. Used by any node that performs I/O that can transiently
fail (an LLM call, a vector-store query, an external API call) -- the
Retrieval Agent's query rewriting and hybrid search today; the Answer
Agent's generation call and the Investigation Agent's evidence-gathering
sources later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.shared.config.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# AGENT_WORKFLOWS.md section 4: "up to 2 retries per node with exponential
# backoff."
_MAX_RETRIES = 2


async def call_with_retry(
    node_name: str,
    operation: Callable[[], Awaitable[T]],
    *,
    retry_count: dict[str, int],
) -> T:
    """Run `operation()`, retrying up to `_MAX_RETRIES` times with
    exponential backoff (1s, 2s, ...) on any exception.

    Mutates `retry_count[node_name]` in place (the caller's
    `GraphState.retry_count` dict) with the number of attempts made so far,
    so the final count is visible in state regardless of whether this call
    ultimately succeeds or exhausts its retries -- AGENT_WORKFLOWS.md section
    1's own reason for keeping `confidence_signals` applies equally here:
    routing/degradation decisions must be debuggable from stored state, not
    re-derived by guessing.

    Re-raises the last exception once retries are exhausted. Converting that
    into a node's own graceful-degradation behavior (e.g. the Retrieval
    Agent's "proceed with zero chunks") is each caller's responsibility, not
    this helper's -- a generic retry wrapper has no way to know what a safe
    degraded fallback looks like for every caller.
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            retry_count[node_name] = attempt + 1
            if attempt >= _MAX_RETRIES:
                logger.warning(
                    "node_retries_exhausted",
                    node_name=node_name,
                    attempts=attempt + 1,
                    error=str(exc),
                )
                raise
            backoff_seconds = 2**attempt
            logger.info(
                "node_retrying",
                node_name=node_name,
                attempt=attempt + 1,
                backoff_seconds=backoff_seconds,
                error=str(exc),
            )
            await asyncio.sleep(backoff_seconds)
            attempt += 1
