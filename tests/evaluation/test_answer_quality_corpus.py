import json
from pathlib import Path


def test_controlled_e2e_corpus_has_resolvable_expected_citations() -> None:
    path = Path("app/evaluation/fixtures/answer_quality_e2e_v1.json")
    corpus = json.loads(path.read_text(encoding="utf-8"))
    documents = {document["id"]: document for document in corpus["documents"]}
    chunks = {document["chunk_id"] for document in corpus["documents"]}

    assert corpus["version"] == "answer-quality-e2e-v1"
    assert len(corpus["questions"]) >= 3
    for case in corpus["questions"]:
        for document_id in case["expected_document_ids"]:
            assert document_id in documents
            assert documents[document_id]["url"]
        assert set(case["expected_citation_ids"]).issubset(chunks)
        if case.get("citation_required"):
            assert case["expected_document_ids"]
            assert case["expected_citation_ids"]
            assert case["required_answer_terms"]

    negative = next(case for case in corpus["questions"] if case["id"] == "negative-control")
    assert negative["must_abstain"] is True
    assert negative["expected_document_ids"] == []
