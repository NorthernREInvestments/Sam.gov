"""Hard eligibility kill-switch before expensive evidence research."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from eligibility_and_recovery.models import (
    BID_ELIGIBLE,
    BID_ELIGIBLE_WITH_ACTION,
    BID_INELIGIBLE,
    BUILD,
    ELIGIBILITY_UNKNOWN,
)
from eligibility_gate import (
    ELIGIBLE_CONDITIONAL,
    ELIGIBLE_CONFIRMED,
    ELIGIBILITY_NOT_APPLICABLE,
    ELIGIBILITY_UNKNOWN as GATE_UNKNOWN,
    NOT_CURRENTLY_ELIGIBLE,
    evaluate_eligibility_gate,
    load_company_eligibility_profile,
)
from m3_data_root import data_path

# State/local actionable patterns (completable before close)
_ACTIONABLE_LOCAL = [
    (re.compile(r"vendor\s+registration|supplier\s+registration|must\s+register", re.I), "VENDOR_REGISTRATION"),
    (re.compile(r"insurance\s+(?:certificate|endorsement|COI)|certificate\s+of\s+insurance", re.I), "INSURANCE_COI"),
    (re.compile(r"W-?9\b|tax\s+ID\s+form", re.I), "W9_SUBMISSION"),
    (re.compile(r"portal\s+setup|create\s+an?\s+account|e-?procurement\s+account", re.I), "PORTAL_SETUP"),
    (re.compile(r"bid\s+bond|bid\s+guarantee|performance\s+bond|payment\s+bond", re.I), "BONDING"),
    (re.compile(r"mandatory\s+site\s+visit|pre[- ]bid\s+(?:conference|meeting)", re.I), "SITE_VISIT_OR_PREBID"),
    (re.compile(r"sample(?:s)?\s+required|product\s+sample", re.I), "SAMPLES_REQUIRED"),
]

# Fatal patterns beyond gate vehicle/set-aside detection
_FATAL_EXTRA = [
    (re.compile(r"facility\s+clearance\s+required|must\s+hold.{0,40}facility\s+clearance", re.I), "FACILITY_CLEARANCE"),
    (re.compile(r"source\s+approval\s+required|approved\s+source\s+list|QPL\s+required", re.I), "SOURCE_APPROVAL"),
    (re.compile(r"CMMC\s+Level\s+[23]|NIST\s+800-171\s+required", re.I), "CYBER_CMMC"),
    (re.compile(r"only\s+authorized\s+(?:dealers?|distributors?)", re.I), "DEALER_AUTHORIZATION"),
]


def _store_path() -> Path:
    return data_path("m3_eligibility_gate_store.json")


def load_eligibility_store() -> dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {"kind": "EligibilityGateStore", "build": BUILD, "by_opportunity": {}, "updated_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "EligibilityGateStore", "build": BUILD, "by_opportunity": {}, "updated_at": None}


def save_eligibility_store(store: dict[str, Any]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = now_utc().isoformat()
    store["build"] = BUILD
    p.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")


def _map_gate_status(overall: str) -> str:
    if overall in {ELIGIBLE_CONFIRMED, ELIGIBILITY_NOT_APPLICABLE}:
        return BID_ELIGIBLE
    if overall == ELIGIBLE_CONDITIONAL:
        return BID_ELIGIBLE_WITH_ACTION
    if overall == NOT_CURRENTLY_ELIGIBLE:
        return BID_INELIGIBLE
    if overall == GATE_UNKNOWN:
        return ELIGIBILITY_UNKNOWN
    return ELIGIBILITY_UNKNOWN


_BUYER_ELIG_CACHE: dict[str, str] = {}


def _buyer_restriction_snippets(code: str) -> str:
    if not code:
        return ""
    if code in _BUYER_ELIG_CACHE:
        return _BUYER_ELIG_CACHE[code]
    hist = data_path("opengov_buyer_history", f"{code}.json")
    snippets: list[str] = []
    if hist.exists():
        try:
            # Cap read — eligibility only needs restriction phrases
            with hist.open("r", encoding="utf-8", errors="ignore") as fh:
                raw = fh.read(250_000)
            for m in re.finditer(
                r".{0,40}(?:set[\s-]*aside|SDVOSB|WOSB|HUBZone|8\(a\)|IDIQ|BPA|GSA|"
                r"clearance|CMMC|approved\s+source|vendor\s+registration).{0,40}",
                raw,
                re.I,
            ):
                snippets.append(m.group(0))
                if len(snippets) >= 40:
                    break
        except Exception:
            pass
    _BUYER_ELIG_CACHE[code] = "\n".join(snippets)
    return _BUYER_ELIG_CACHE[code]


def _collect_package_text(oid: str, pack: dict[str, Any] | None = None) -> str:
    """Assemble eligibility text from identity pack + lightweight doc signals."""
    parts: list[str] = [str(oid)]
    pack = pack or {}
    for ident in (pack.get("identities") or [])[:80]:
        if not isinstance(ident, dict):
            continue
        for k in (
            "raw_description",
            "solicitation_notes",
            "manufacturer",
            "brand",
            "identity_type",
            "commercial_search_key",
        ):
            v = ident.get(k)
            if v:
                parts.append(str(v))
    code = oid.split(":")[1] if oid.startswith("opengov:") and oid.count(":") >= 2 else ""
    if code:
        parts.append(_buyer_restriction_snippets(code))
    if code and oid.count(":") >= 2:
        pid = oid.split(":")[-1]
        docs_dir = data_path("opengov_public_docs", "documents", code, pid)
        if docs_dir.exists():
            for fp in list(docs_dir.rglob("*"))[:40]:
                parts.append(fp.name)
    return "\n".join(parts)


def evaluate_opportunity_eligibility(
    oid: str,
    *,
    pack: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Hard eligibility gate for one opportunity. Returns BID_* status + actions."""
    profile = profile or load_company_eligibility_profile()
    package_text = text or _collect_package_text(oid, pack)
    gate = evaluate_eligibility_gate(text=package_text, profile=profile)
    status = _map_gate_status(str(gate.get("overall_status") or GATE_UNKNOWN))

    actionable: list[dict[str, Any]] = []
    fatal_extra: list[dict[str, Any]] = []
    for rx, code in _ACTIONABLE_LOCAL:
        if rx.search(package_text):
            actionable.append(
                {
                    "blocking_requirement": code,
                    "required_action": f"Complete {code.replace('_', ' ').lower()} before close",
                    "source_document": "package_text",
                    "confidence": "C",
                }
            )
    for rx, code in _FATAL_EXTRA:
        if rx.search(package_text):
            fatal_extra.append(
                {
                    "blocking_requirement": code,
                    "required_action": "STOP — cannot satisfy before close",
                    "source_document": "package_text",
                    "confidence": "B",
                }
            )

    if fatal_extra and status != BID_INELIGIBLE:
        status = BID_INELIGIBLE
    elif actionable and status == BID_ELIGIBLE:
        status = BID_ELIGIBLE_WITH_ACTION

    package_gap: dict[str, Any] | None = None
    # Without real package/solicitation body, do not green-light expensive research
    has_docs = False
    code = oid.split(":")[1] if oid.startswith("opengov:") and oid.count(":") >= 2 else ""
    if code and oid.count(":") >= 2:
        pid = oid.split(":")[-1]
        docs_dir = data_path("opengov_public_docs", "documents", code, pid)
        try:
            has_docs = docs_dir.exists() and any(docs_dir.iterdir())
        except Exception:
            has_docs = False
    restriction_hits = bool(
        fatal_extra
        or actionable
        or gate.get("vehicles_detected")
        or gate.get("access_requirements")
    )
    if status in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION} and not has_docs and not restriction_hits:
        status = ELIGIBILITY_UNKNOWN
        package_gap = {
            "blocking_requirement": "PACKAGE_NOT_REVIEWED",
            "required_action": "Recover solicitation package and re-run eligibility before deep research",
            "source_document": "missing_package",
            "confidence": "B",
        }

    can_bid = {
        BID_ELIGIBLE: "YES",
        BID_ELIGIBLE_WITH_ACTION: "YES WITH ACTION",
        ELIGIBILITY_UNKNOWN: "UNKNOWN",
        BID_INELIGIBLE: "NO",
    }.get(status, "UNKNOWN")

    primary_block = None
    if status == BID_INELIGIBLE:
        primary_block = (fatal_extra[0] if fatal_extra else None) or {
            "blocking_requirement": gate.get("next_action") or gate.get("plain") or "GATE_INELIGIBLE",
            "required_action": "Do not pursue",
            "source_document": "eligibility_gate",
            "confidence": "B",
        }
    elif status == BID_ELIGIBLE_WITH_ACTION:
        primary_block = actionable[0] if actionable else {
            "blocking_requirement": gate.get("next_action") or "CONDITIONAL_GATE",
            "required_action": str(gate.get("plain") or "Complete conditional eligibility action"),
            "source_document": "eligibility_gate",
            "confidence": "C",
        }
    elif status == ELIGIBILITY_UNKNOWN:
        primary_block = package_gap or {
            "blocking_requirement": "ELIGIBILITY_UNRESOLVED",
            "required_action": "Resolve eligibility question before deep research",
            "source_document": "eligibility_gate",
            "confidence": "C",
        }

    return {
        "opportunity_id": oid,
        "eligibility_status": status,
        "can_we_bid": can_bid,
        "why": (primary_block or {}).get("blocking_requirement") or gate.get("plain"),
        "what_action": (primary_block or {}).get("required_action"),
        "when_due": None,
        "evidence": {
            "gate_overall": gate.get("overall_status"),
            "gate_plain": gate.get("plain"),
            "actionable_items": actionable,
            "fatal_items": fatal_extra,
            "package_text_chars": len(package_text),
        },
        "blocking_requirement": (primary_block or {}).get("blocking_requirement"),
        "required_action": (primary_block or {}).get("required_action"),
        "deadline": None,
        "source_document": (primary_block or {}).get("source_document"),
        "page_section": None,
        "confidence": (primary_block or {}).get("confidence") or "C",
        "proceed_to_research": status in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION},
        "gate_raw": {
            "overall_status": gate.get("overall_status"),
            "actionable_for_quote_or_bid": gate.get("actionable_for_quote_or_bid"),
            "next_action": gate.get("next_action"),
        },
        "evaluated_at": now_utc().isoformat(),
        "build": BUILD,
    }
