"""Durable JSON store for Financing Intelligence (file-backed, Decimal-safe money as strings)."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from application_clock import now_utc

from financing_intelligence.constants import BUILD

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "financing_intelligence"
CAPITAL_PATH = DATA / "capital.json"
SOURCES_PATH = DATA / "sources.json"
FACTS_PATH = DATA / "facts.json"
NOTES_PATH = DATA / "call_notes.json"
RESERVATIONS_PATH = DATA / "capital_reservations.json"
OUTCOMES_PATH = DATA / "transaction_outcomes.json"
ASSESSMENTS_PATH = DATA / "opportunity_assessments.json"
OWNER_PREFS_PATH = DATA / "owner_preferences.json"


def set_data_root(path: Path | str | None) -> Path:
    """Test/isolation hook — redirects all financing JSON files under path."""
    global DATA, CAPITAL_PATH, SOURCES_PATH, FACTS_PATH, NOTES_PATH
    global RESERVATIONS_PATH, OUTCOMES_PATH, ASSESSMENTS_PATH, OWNER_PREFS_PATH
    DATA = Path(path) if path is not None else ROOT / "data" / "financing_intelligence"
    DATA.mkdir(parents=True, exist_ok=True)
    CAPITAL_PATH = DATA / "capital.json"
    SOURCES_PATH = DATA / "sources.json"
    FACTS_PATH = DATA / "facts.json"
    NOTES_PATH = DATA / "call_notes.json"
    RESERVATIONS_PATH = DATA / "capital_reservations.json"
    OUTCOMES_PATH = DATA / "transaction_outcomes.json"
    ASSESSMENTS_PATH = DATA / "opportunity_assessments.json"
    OWNER_PREFS_PATH = DATA / "owner_preferences.json"
    return DATA


def reset_data_root() -> Path:
    return set_data_root(None)


def _utc() -> str:
    return now_utc().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def money(v: Any) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money_str(v: Any) -> str:
    return str(money(v).quantize(Decimal("0.01")))


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    DATA.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def _save(path: Path, payload: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def default_capital() -> dict[str, Any]:
    return {
        "kind": "OwnerCapitalAccount",
        "build": BUILD,
        "business_cash": "0",
        "unrestricted_additional_capital": "0",
        "minimum_operating_reserve": "0",
        "max_deploy_per_deal": None,
        "owner_contribution_allowed": False,
        "max_owner_contribution": "0",
        "updated_at": None,
        "notes": "Default launch capital = $0. Never auto-spend company cash.",
    }


def default_owner_prefs() -> dict[str, Any]:
    return {
        "kind": "FinancingOwnerPreferences",
        "build": BUILD,
        "personal_guarantee_allowed": False,
        "personal_credit_dependency_allowed": False,
        "owner_cash_requirement_default": "0",
        "unknown_is_not_incompatible": True,
        "lender_compatible_is_not_approved": True,
        "updated_at": None,
    }


def load_capital() -> dict[str, Any]:
    return _load(CAPITAL_PATH, default_capital())


def save_capital(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(CAPITAL_PATH, doc)
    return doc


def load_owner_prefs() -> dict[str, Any]:
    return _load(OWNER_PREFS_PATH, default_owner_prefs())


def default_owner_defaults() -> dict[str, Any]:
    return default_owner_prefs()


def save_owner_prefs(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(OWNER_PREFS_PATH, doc)
    return doc


def load_sources() -> dict[str, Any]:
    return _load(SOURCES_PATH, {"kind": "FinancingSources", "build": BUILD, "items": []})


def save_sources(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(SOURCES_PATH, doc)
    return doc


def load_facts() -> dict[str, Any]:
    return _load(FACTS_PATH, {"kind": "FinancingFacts", "build": BUILD, "items": []})


def save_facts(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(FACTS_PATH, doc)
    return doc


def load_notes() -> dict[str, Any]:
    return _load(NOTES_PATH, {"kind": "FinancingCallNotes", "build": BUILD, "items": []})


def save_notes(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(NOTES_PATH, doc)
    return doc


def load_reservations() -> dict[str, Any]:
    return _load(
        RESERVATIONS_PATH,
        {"kind": "CapitalReservations", "build": BUILD, "items": []},
    )


def save_reservations(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(RESERVATIONS_PATH, doc)
    return doc


def load_outcomes() -> dict[str, Any]:
    return _load(OUTCOMES_PATH, {"kind": "FinancingOutcomes", "build": BUILD, "items": []})


def save_outcomes(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(OUTCOMES_PATH, doc)
    return doc


def load_assessments() -> dict[str, Any]:
    return _load(
        ASSESSMENTS_PATH,
        {"kind": "OpportunityFinancingAssessments", "build": BUILD, "by_opportunity": {}},
    )


def save_assessments(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc["build"] = BUILD
    doc["updated_at"] = _utc()
    _save(ASSESSMENTS_PATH, doc)
    return doc
