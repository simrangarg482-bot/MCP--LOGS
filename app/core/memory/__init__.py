"""core/memory -- persistent, permission-aware agent memory (Priority 4).

Owned by: core/memory. Owns exactly one table (`agent_memories`).

WHAT THIS IS NOT
    Not conversation history. `agent_executions` already records what was
    asked and how the agent performed, and backs `GET /ask/history`. This
    module holds the small number of *curated, durable* statements worth
    recalling in a later, unrelated request -- each one explicitly created,
    never harvested from every message. See `docs/AGENT_MEMORY.md`.

    Not a second knowledge base. Organization knowledge lives in
    `documents`/`*_chunks` and reaches answers as cited evidence through
    `app.retrieval`. Memory is *context*, never a citation source -- it is
    deliberately kept out of `ScoredChunk`/`assemble_context` so it cannot
    become a numbered reference (see `app.agents.answer.generation`).

THE ONE INVARIANT
    A memory must never become visible merely because it is semantically
    similar. Authorization is part of the SQL `WHERE` clause, evaluated by
    Postgres *before* any vector ordering or `LIMIT` selects anything --
    never a post-filter over a globally-similar candidate set. See
    `repository.recall`, which is the only read path used for injection.
"""
