"""Loads a versioned evaluation dataset from disk.

On-disk shape (per this package's spec): one JSONL file, each line a JSON
object matching `EvaluationCase`, plus an optional sidecar metadata file --
`<dataset>.jsonl` + `<dataset>.meta.json` (JSON, not YAML: this codebase has
no existing YAML-config convention anywhere -- `pyyaml` is only a transitive
dependency of unrelated packages, not a direct one -- so JSON keeps this
package consistent with every other config/data file in the repository
rather than introducing a new format for one file).

A missing metadata file is not an error -- it falls back to a minimal
`DatasetMetadata` derived from the JSONL filename, so a quick ad-hoc dataset
doesn't require a second file. A *malformed* metadata or case file is
always an error, with a message that names the exact file and (for cases)
line number, per this package's "clear validation errors" requirement.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.evaluation.schemas import Dataset, DatasetMetadata, EvaluationCase


class DatasetValidationError(Exception):
    """Raised for any malformed dataset file -- always names the file and,
    for a case-level problem, the 1-indexed line number, so a developer can
    jump straight to the bad line without re-deriving which one failed.
    """


def _metadata_path_for(dataset_path: Path) -> Path:
    return dataset_path.with_suffix("").with_suffix(".meta.json")


def _default_metadata(dataset_path: Path) -> DatasetMetadata:
    return DatasetMetadata(dataset_name=dataset_path.stem, description="(no .meta.json found)")


def _load_metadata(dataset_path: Path) -> DatasetMetadata:
    metadata_path = _metadata_path_for(dataset_path)
    if not metadata_path.exists():
        return _default_metadata(dataset_path)
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(f"{metadata_path}: invalid JSON ({exc})") from exc
    try:
        return DatasetMetadata.model_validate(raw)
    except ValidationError as exc:
        raise DatasetValidationError(f"{metadata_path}: {exc}") from exc


def _load_cases(dataset_path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    lines = dataset_path.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(
                f"{dataset_path}:{line_number}: invalid JSON ({exc})"
            ) from exc
        try:
            case = EvaluationCase.model_validate(payload)
        except ValidationError as exc:
            raise DatasetValidationError(f"{dataset_path}:{line_number}: {exc}") from exc
        if case.id in seen_ids:
            raise DatasetValidationError(
                f"{dataset_path}:{line_number}: duplicate case id {case.id!r}"
            )
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetValidationError(f"{dataset_path}: no cases found (empty dataset)")
    return cases


def load_dataset(dataset_path: str | Path) -> Dataset:
    """Load and validate one `.jsonl` dataset file plus its optional
    `.meta.json` sidecar. Raises `DatasetValidationError` on anything
    malformed; raises `FileNotFoundError` (not `DatasetValidationError`) if
    `dataset_path` itself doesn't exist -- a missing file is a different
    problem than a malformed one.
    """
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(f"dataset file not found: {path}")
    metadata = _load_metadata(path)
    cases = _load_cases(path)
    return Dataset(metadata=metadata, cases=cases)


def load_datasets(dataset_paths: list[str | Path]) -> list[Dataset]:
    """Load several dataset files, e.g. the four fixture datasets this
    package ships (retrieval/grounding/answer/investigation) -- a thin
    convenience over calling `load_dataset` in a loop, kept here so the
    runner and CLI don't each re-write the same loop."""
    return [load_dataset(path) for path in dataset_paths]
