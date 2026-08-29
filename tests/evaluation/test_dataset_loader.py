"""Tests for `app.evaluation.datasets.loader`."""

from __future__ import annotations

import json

import pytest

from app.evaluation.datasets.loader import DatasetValidationError, load_dataset


def _write(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def test_loads_valid_dataset_with_metadata(tmp_path):
    dataset_path = tmp_path / "sample.jsonl"
    _write(
        dataset_path,
        [
            json.dumps({"id": "case-1", "category": "retrieval", "query": "why did it fail?"}),
            json.dumps({"id": "case-2", "category": "answer", "query": "what happened?"}),
        ],
    )
    (tmp_path / "sample.meta.json").write_text(
        json.dumps({"dataset_name": "sample", "version": "2.0", "description": "test dataset"}),
        encoding="utf-8",
    )

    dataset = load_dataset(dataset_path)

    assert dataset.metadata.dataset_name == "sample"
    assert dataset.metadata.version == "2.0"
    assert len(dataset.cases) == 2
    assert dataset.cases[0].id == "case-1"


def test_missing_metadata_file_falls_back_to_default(tmp_path):
    dataset_path = tmp_path / "no_meta.jsonl"
    _write(dataset_path, [json.dumps({"id": "case-1", "category": "retrieval", "query": "q"})])

    dataset = load_dataset(dataset_path)

    assert dataset.metadata.dataset_name == "no_meta"
    assert dataset.metadata.version == "1.0"


def test_blank_lines_are_skipped(tmp_path):
    dataset_path = tmp_path / "with_blanks.jsonl"
    _write(
        dataset_path,
        [
            json.dumps({"id": "case-1", "category": "retrieval", "query": "q"}),
            "",
            "   ",
            json.dumps({"id": "case-2", "category": "retrieval", "query": "q2"}),
        ],
    )

    dataset = load_dataset(dataset_path)

    assert len(dataset.cases) == 2


def test_invalid_json_line_raises_with_line_number(tmp_path):
    dataset_path = tmp_path / "bad.jsonl"
    valid_line = json.dumps({"id": "ok", "category": "retrieval", "query": "q"})
    _write(dataset_path, [valid_line, "{not valid json"])

    with pytest.raises(DatasetValidationError, match="bad.jsonl:2"):
        load_dataset(dataset_path)


def test_missing_required_field_raises_validation_error(tmp_path):
    dataset_path = tmp_path / "missing_field.jsonl"
    _write(dataset_path, [json.dumps({"category": "retrieval", "query": "q"})])  # no "id"

    with pytest.raises(DatasetValidationError, match="missing_field.jsonl:1"):
        load_dataset(dataset_path)


def test_duplicate_case_ids_raise_validation_error(tmp_path):
    dataset_path = tmp_path / "dupes.jsonl"
    _write(
        dataset_path,
        [
            json.dumps({"id": "same-id", "category": "retrieval", "query": "q1"}),
            json.dumps({"id": "same-id", "category": "retrieval", "query": "q2"}),
        ],
    )

    with pytest.raises(DatasetValidationError, match="duplicate case id"):
        load_dataset(dataset_path)


def test_empty_dataset_file_raises_validation_error(tmp_path):
    dataset_path = tmp_path / "empty.jsonl"
    _write(dataset_path, [])

    with pytest.raises(DatasetValidationError, match="no cases found"):
        load_dataset(dataset_path)


def test_malformed_metadata_json_raises_validation_error(tmp_path):
    dataset_path = tmp_path / "sample.jsonl"
    _write(dataset_path, [json.dumps({"id": "case-1", "category": "retrieval", "query": "q"})])
    (tmp_path / "sample.meta.json").write_text("{not valid", encoding="utf-8")

    with pytest.raises(DatasetValidationError, match="sample.meta.json"):
        load_dataset(dataset_path)


def test_missing_dataset_file_raises_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "does_not_exist.jsonl")


def test_shipped_fixture_datasets_all_load_without_error():
    from pathlib import Path

    fixtures_dir = Path(__file__).resolve().parents[2] / "app" / "evaluation" / "fixtures"
    dataset_names = (
        "retrieval_core_v1",
        "grounding_core_v1",
        "answer_core_v1",
        "investigation_core_v1",
    )
    for name in dataset_names:
        dataset = load_dataset(fixtures_dir / f"{name}.jsonl")
        assert dataset.metadata.dataset_name == name
        assert len(dataset.cases) > 0
