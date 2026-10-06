"""UI / API service façade for Financing Intelligence."""

from __future__ import annotations

from typing import Any

from financing_intelligence.assess import assess_opportunity_financing, blocked_profit_summary
from financing_intelligence.capital import capital_snapshot, update_capital
from financing_intelligence.constants import BUILD, FACT_PROPOSED
from financing_intelligence.facts import decide_fact, list_facts
from financing_intelligence.notes_extract import ingest_call_notes
from financing_intelligence.sources import list_sources, source_display, upsert_source
from financing_intelligence.store import (
    load_assessments,
    load_notes,
    load_outcomes,
    load_owner_prefs,
    load_reservations,
    save_owner_prefs,
)


def dashboard() -> dict[str, Any]:
    snap = capital_snapshot()
    reserved = snap.get("active_commitments")
    proposed = sum(1 for f in list_facts(status=FACT_PROPOSED))
    sources = list_sources(active_only=True)
    outcomes = load_outcomes().get("items") or []
    open_apps = [o for o in outcomes if o.get("status") in {"CONTACTED", "APPLICATION_STARTED", "SUBMITTED"}]
    approved = [o for o in outcomes if o.get("status") in {"APPROVED", "FUNDED"}]
    blocked = blocked_profit_summary()
    assessments = load_assessments().get("by_opportunity") or {}
    status_counts: dict[str, int] = {}
    for payload in assessments.values():
        st = (payload.get("card") or {}).get("status")
        if st:
            status_counts[st] = status_counts.get(st, 0) + 1
    return {
        "kind": "FinancingIntelligenceDashboard",
        "build": BUILD,
        "title": "Financing Intelligence",
        "capital": snap,
        "capital_reserved": reserved,
        "confirmed_company_capital": snap.get("business_cash"),
        "deployable_capital": snap.get("deployable_capital"),
        "active_sources": len(sources),
        "facts_awaiting_review": proposed,
        "open_financing_applications": len(open_apps),
        "approved_or_funded": len(approved),
        "status_counts": {
            "likely_financeable": status_counts.get("LIKELY_FINANCEABLE", 0),
            "awaiting_lender_approval": status_counts.get("NEEDS_LENDER_APPROVAL", 0)
            + status_counts.get("LIKELY_FINANCEABLE", 0),
            "requires_supplier_terms": status_counts.get("REQUIRES_SUPPLIER_TERMS", 0),
            "financing_gaps": status_counts.get("FINANCING_GAP", 0),
            "execution_failed": status_counts.get("EXECUTION_FAIL", 0),
            "capital_confirmation_required": status_counts.get("CAPITAL_CONFIRMATION_REQUIRED", 0),
        },
        "blocked_profit": blocked,
        "sources_needing_verification": [
            source_display(s) for s in sources if not s.get("last_verified_date")
        ][:10],
        "owner_preferences": load_owner_prefs(),
    }


def ui_page(tab: str = "sources") -> dict[str, Any]:
    tab = (tab or "sources").lower()
    base = {
        "kind": "FinancingIntelligencePage",
        "build": BUILD,
        "tab": tab,
        "tabs": [
            {"id": "sources", "label": "Sources"},
            {"id": "add", "label": "Add Information"},
            {"id": "history", "label": "Deal History"},
            {"id": "capital", "label": "Capital"},
            {"id": "rules", "label": "Rules / Evidence"},
        ],
        "dashboard": dashboard(),
    }
    if tab == "sources":
        base["sources"] = [source_display(s) for s in list_sources()]
    elif tab == "add":
        base["recent_notes"] = (load_notes().get("items") or [])[-10:][::-1]
        base["proposed_facts"] = list_facts(status=FACT_PROPOSED)[:50]
    elif tab == "history":
        base["outcomes"] = (load_outcomes().get("items") or [])[-50:][::-1]
        base["reservations"] = (load_reservations().get("items") or [])[-50:][::-1]
    elif tab == "capital":
        base["capital"] = capital_snapshot()
        base["reservations"] = load_reservations().get("items") or []
    elif tab == "rules":
        base["approved_facts"] = list_facts(status="APPROVED")[:100]
        base["proposed_facts"] = list_facts(status=FACT_PROPOSED)[:50]
        base["rejected_facts"] = list_facts(status="REJECTED")[:20]
    return base


# re-exports for routes
__all__ = [
    "dashboard",
    "ui_page",
    "upsert_source",
    "ingest_call_notes",
    "decide_fact",
    "list_facts",
    "update_capital",
    "capital_snapshot",
    "assess_opportunity_financing",
    "save_owner_prefs",
    "load_owner_prefs",
]
