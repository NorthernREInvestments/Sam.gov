"""Raw call-note intake → proposed facts (never auto-active)."""

from __future__ import annotations

import re
from typing import Any

from financing_intelligence.constants import (
    BUILD,
    CASE_BY_CASE,
    CREDIT_HARD,
    CREDIT_NOT_REQUIRED,
    CREDIT_UNKNOWN,
    EV_VERBAL_CONFIRMATION,
    FACT_PROPOSED,
    NO,
    PG_CASE_BY_CASE,
    PG_NOT_REQUIRED,
    PG_UNKNOWN,
    UNKNOWN,
    YES,
)
from financing_intelligence.store import load_notes, money_str, new_id, save_notes, _utc


def extract_proposed_facts_from_notes(text: str) -> list[dict[str, Any]]:
    """Heuristic extraction — proposed only. Operator must approve before rules activate."""
    blob = text or ""
    low = blob.lower()
    proposed: list[dict[str, Any]] = []

    def add(field: str, value: Any, snippet: str, confidence: str = "MEDIUM"):
        proposed.append(
            {
                "field": field,
                "value": value,
                "source_snippet": snippet.strip()[:240],
                "confidence": confidence,
                "evidence_type": EV_VERBAL_CONFIRMATION,
            }
        )

    m = re.search(r"(?:above|over|greater than|>)\s*\$?\s*([\d,]+)\s*k", low)
    if m:
        add("preferred_deal_size_min", money_str(int(m.group(1).replace(",", "")) * 1000), m.group(0), "HIGH")
    m = re.search(r"\$\s*([\d,]+)\s*(?:k)?\+", low)
    if m and "preferred_deal_size_min" not in {p["field"] for p in proposed}:
        raw = m.group(1).replace(",", "")
        val = int(raw) * (1000 if "k" in m.group(0).lower() or int(raw) < 1000 else 1)
        if "k" in low[max(0, m.start() - 5) : m.end() + 2]:
            val = int(raw) * 1000
        add("preferred_deal_size_min", money_str(val), m.group(0))

    if re.search(r"whole supplier invoice|cover the whole|100%\s*of\s*supplier|fund 100%", low):
        add("can_fund_100_percent_supplier_invoice", YES, "100%/whole supplier invoice", "HIGH")
        add("max_advance_pct", "100", "100% supplier invoice", "MEDIUM")
    m = re.search(r"(\d{1,3})\s*%\s*(?:advance|of\s*supplier|coverage)", low)
    if m:
        add("max_advance_pct", m.group(1), m.group(0), "HIGH")

    m = re.search(r"margin\s*(?:is|of)?\s*(\d{1,2})\s*%\+?", low)
    if not m:
        m = re.search(r"(\d{1,2})\s*%\+?\s*(?:gross\s*)?margin", low)
    if m:
        add("minimum_gross_margin_pct", m.group(1), m.group(0), "HIGH")

    if re.search(r"no hard (?:credit )?pull|soft pull|no hard credit", low):
        add("personal_credit", CREDIT_NOT_REQUIRED, "no hard credit pull", "HIGH")
    elif re.search(r"hard (?:credit )?pull", low):
        add("personal_credit", CREDIT_HARD, "hard pull", "HIGH")
    else:
        if "credit" in low:
            add("personal_credit", CREDIT_UNKNOWN, "credit mentioned", "LOW")

    if re.search(r"\bno pg\b|no personal guarantee|pg not required", low):
        add("personal_guarantee", PG_NOT_REQUIRED, "no PG", "HIGH")
    elif re.search(r"pg depends|pg case|personal guarantee depends", low):
        add("personal_guarantee", PG_CASE_BY_CASE, "PG depends/case-by-case", "HIGH")
    elif re.search(r"\bpg\b|personal guarantee", low):
        add("personal_guarantee", PG_UNKNOWN, "PG mentioned", "LOW")

    if re.search(r"supplier (?:has )?to be established|established supplier|supplier must be approved", low):
        add("supplier_must_be_established", YES, "supplier established/approved", "HIGH")

    m = re.search(r"(\d+)\s*days?\s*(?:underwriting|approval|to approve)", low)
    if m:
        add("underwriting_days_max", int(m.group(1)), m.group(0), "HIGH")
        add("underwriting_days_min", max(1, int(m.group(1)) - 1), m.group(0), "MEDIUM")

    if re.search(r"pay(?:s|ing)? supplier directly|direct supplier payment", low):
        add("pays_supplier_directly", YES, "pays supplier directly", "HIGH")

    if re.search(r"gov(?:ernment)?\s*po|government contracts?|federal", low):
        add("federal_contracts_accepted", YES, "gov/federal mentioned", "MEDIUM")
        add("government_po_sufficient", YES, "gov PO", "MEDIUM")

    if re.search(r"first[- ]time|first contract", low):
        add("first_time_contractor_accepted", YES, "first-time/first contract", "MEDIUM")

    return proposed


def ingest_call_notes(
    *,
    raw_notes: str,
    source_id: str | None = None,
    company_name: str | None = None,
    contact: str | None = None,
    call_date: str | None = None,
    follow_up_date: str | None = None,
) -> dict[str, Any]:
    from financing_intelligence.store import load_facts, save_facts

    proposed = extract_proposed_facts_from_notes(raw_notes)
    note = {
        "note_id": new_id("NOTE"),
        "source_id": source_id,
        "company_name": company_name,
        "contact": contact,
        "call_date": call_date or _utc()[:10],
        "follow_up_date": follow_up_date,
        "raw_notes": raw_notes,
        "created_at": _utc(),
        "build": BUILD,
        "proposed_fact_ids": [],
    }
    facts_doc = load_facts()
    for p in proposed:
        fact = {
            "fact_id": new_id("FACT"),
            "note_id": note["note_id"],
            "source_id": source_id,
            "field": p["field"],
            "value": p["value"],
            "proposed_value": p["value"],
            "source_snippet": p["source_snippet"],
            "confidence": p["confidence"],
            "evidence_type": p["evidence_type"],
            "status": FACT_PROPOSED,
            "created_at": _utc(),
            "decided_at": None,
            "decided_by": None,
            "build": BUILD,
        }
        facts_doc.setdefault("items", []).append(fact)
        note["proposed_fact_ids"].append(fact["fact_id"])
    save_facts(facts_doc)
    notes_doc = load_notes()
    notes_doc.setdefault("items", []).append(note)
    save_notes(notes_doc)
    return {
        "ok": True,
        "note": note,
        "proposed_facts": [f for f in facts_doc["items"] if f["fact_id"] in note["proposed_fact_ids"]],
        "auto_activated": False,
        "message": "Extracted facts are PROPOSED only — approve before they affect opportunities.",
    }
