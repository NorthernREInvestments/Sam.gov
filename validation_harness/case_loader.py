"""Load curated golden cases from validation_harness/corpus/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from validation_harness.models import CASE_REQUIRED_KEYS, SOURCE_TYPE_SYNTHETIC, empty_case

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"


def corpus_dir() -> Path:
    return CORPUS_DIR


def list_case_files() -> list[Path]:
    if not CORPUS_DIR.is_dir():
        return []
    files = list(CORPUS_DIR.glob("CASE_*.json"))
    files.extend(CORPUS_DIR.glob("BP_*.json"))
    files.extend(CORPUS_DIR.glob("ADV_*.json"))
    files.extend(CORPUS_DIR.glob("R_*.json"))
    files.extend(CORPUS_DIR.glob("REALITY_*.json"))
    # de-dupe by stem, stable order
    by_stem = {p.stem: p for p in files}
    return sorted(by_stem.values(), key=lambda p: p.stem)


def list_cases() -> list[str]:
    return [p.stem for p in list_case_files()]


def load_case(case_id_or_path: str | Path) -> dict[str, Any]:
    path = Path(case_id_or_path)
    if not path.suffix:
        # try by case_id / stem
        candidates = list(CORPUS_DIR.glob(f"{case_id_or_path}*.json")) if CORPUS_DIR.is_dir() else []
        if not candidates:
            candidates = list(CORPUS_DIR.glob(f"*{case_id_or_path}*.json")) if CORPUS_DIR.is_dir() else []
        if not candidates:
            raise FileNotFoundError(f"Golden case not found: {case_id_or_path}")
        path = candidates[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Case must be a JSON object: {path}")
    for key in CASE_REQUIRED_KEYS:
        if key not in data:
            raise ValueError(f"Case {path.name} missing required key: {key}")
    # Normalize defaults without inventing expected truth
    out = empty_case()
    out.update(data)
    if not out.get("source_type"):
        out["source_type"] = SOURCE_TYPE_SYNTHETIC
    return out


def load_cases(
    *,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    adversarial_only: bool = False,
) -> list[dict[str, Any]]:
    cases = [load_case(p) for p in list_case_files()]
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.get("case_id") in wanted or any(c.get("case_id", "").startswith(w) for w in wanted)]
    if tags:
        tagset = set(tags)
        cases = [c for c in cases if tagset.intersection(set(c.get("tags") or []))]
    if adversarial_only:
        cases = [c for c in cases if c.get("adversarial")]
    return cases
