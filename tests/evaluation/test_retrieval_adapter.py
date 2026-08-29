"""Tests for `app.evaluation.adapters.retrieval` -- specifically the
permission-aware filtering requirement (section 15 of this package's spec):
the same query, same corpus, different identity permissions must produce a
different result.
"""

from __future__ import annotations

import pytest

from app.evaluation.adapters.retrieval import FixtureRetrievalAdapter
from app.evaluation.fixtures.corpus import CORPUS, document_id_for_label
from app.evaluation.schemas import EvalIdentity, EvaluationCase


@pytest.mark.asyncio
async def test_restricted_document_hidden_without_permission():
    case = EvaluationCase(
        id="t1",
        category="retrieval",
        query="confidential security incident response plan for auth breaches",
    )
    adapter = FixtureRetrievalAdapter(CORPUS)
    results = await adapter.search(case, top_k=10)
    retrieved_ids = {str(chunk.document_id) for chunk in results}
    assert document_id_for_label("restricted-security-plan") not in retrieved_ids


@pytest.mark.asyncio
async def test_restricted_document_visible_with_permission():
    case = EvaluationCase(
        id="t2",
        category="retrieval",
        query="confidential security incident response plan for auth breaches",
        identity=EvalIdentity(permissions=frozenset({"security:read"})),
    )
    adapter = FixtureRetrievalAdapter(CORPUS)
    results = await adapter.search(case, top_k=10)
    retrieved_ids = {str(chunk.document_id) for chunk in results}
    assert document_id_for_label("restricted-security-plan") in retrieved_ids


@pytest.mark.asyncio
async def test_unrestricted_documents_visible_regardless_of_permissions():
    case = EvaluationCase(
        id="t3", category="retrieval", query="authentication service deployment failure"
    )
    adapter = FixtureRetrievalAdapter(CORPUS)
    results = await adapter.search(case, top_k=10)
    retrieved_ids = {str(chunk.document_id) for chunk in results}
    assert document_id_for_label("incident-123") in retrieved_ids


@pytest.mark.asyncio
async def test_search_respects_top_k():
    case = EvaluationCase(
        id="t4", category="retrieval", query="authentication service deployment failure"
    )
    adapter = FixtureRetrievalAdapter(CORPUS)
    results = await adapter.search(case, top_k=1)
    assert len(results) <= 1


@pytest.mark.asyncio
async def test_no_overlap_returns_empty_results():
    case = EvaluationCase(
        id="t5", category="retrieval", query="xyzzy plugh nonexistent-domain-term"
    )
    adapter = FixtureRetrievalAdapter(CORPUS)
    results = await adapter.search(case, top_k=10)
    assert results == []
