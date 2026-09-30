"""Phase G supervised live batch — SAM-primary, read-only, no outreach.

Fetches LIVE_SOURCE opportunities, cheap-screens for product resale,
runs canonical enrich + owner gate, and writes funnel artifacts.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_g.provenance import PROVENANCE_LIVE_SOURCE, stamp_live_source

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "phase_g"
PRODUCTISH = {
    "CORE_PRODUCT",
    "PRODUCT_PLUS_SERVICE",
    "LIKELY_PRODUCT_RESALE",
    "PRODUCT_RESALE",
    "PRODUCT_PLUS_MINOR_SERVICE",
    "FEDERAL_PRODUCT_LIKELY",
    "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE",
}


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _blocker_bucket(blockers: list[Any], gate: dict[str, Any]) -> str:
    """Primary blocker bucket — priority order (most actionable / most severe first)."""
    codes = [str(b).upper() for b in (blockers or [])]
    joined = " ".join(codes) + " " + json.dumps(gate, default=str).upper()
    checks = [
        ("deadline", ("DEADLINE", "EXPIRED", "TOO_SHORT", "RUNWAY")),
        ("documents_tdp", ("TDP", "DRAWING", "TECHNICAL_DATA", "ATTACHMENT", "DOCUMENT")),
        ("identity_uom", ("PRODUCT_IDENTITY", "QUANTITY_UOM", "UOM", "IDENTITY_UNCONFIRMED")),
        ("supplier_pricing", ("SUPPLIER_NOT_VALIDATED", "QUOTE_NOT_EXECUTABLE", "ECONOMICS_INCOMPLETE", "ACQUISITION")),
        ("funding", ("FINANCING_INCOMPATIBLE", "FUNDING", "CASH_CYCLE", "CAPITAL")),
        ("compliance_packaging", ("PACKAGING", "MIL-STD", "MIL_STD", "FAT", "INSPECTION", "SOURCE_APPROVAL")),
        ("amendment", ("AMEND",)),
    ]
    for bucket, keys in checks:
        if any(k in joined for k in keys):
            return bucket
    if codes:
        return "other_blocker"
    return "none"


def _operator_action(enriched: dict[str, Any], rejected: bool) -> str:
    if rejected:
        return "REJECT"
    if enriched.get("ready_for_owner_approval") is True:
        return "READY_FOR_OWNER_APPROVAL"
    nxt = str(enriched.get("operator_next_action") or "")
    blockers = [str(b).upper() for b in (enriched.get("execution_critical_blockers") or [])]
    joined = " ".join(blockers) + " " + nxt.upper()
    # Prefer specific, executable states (order matters)
    ordered = [
        ("DEADLINE_TOO_SHORT", ("DEADLINE", "TOO SHORT", "EXPIRED")),
        ("REVIEW_AMENDMENT", ("AMEND",)),
        ("REVIEW_TDP", ("TDP", "DRAWING", "TECHNICAL_DATA", "ATTACHMENT")),
        ("VERIFY_PRODUCT_IDENTITY", ("PRODUCT_IDENTITY_UNCONFIRMED", "IDENTITY")),
        ("VERIFY_UOM", ("QUANTITY_UOM", "UOM_UNCONFIRMED")),
        ("GET_SUPPLIER_QUOTE", ("SUPPLIER_NOT_VALIDATED", "QUOTE_NOT_EXECUTABLE", "ECONOMICS_INCOMPLETE")),
        ("VERIFY_FINANCING_PATH", ("FINANCING_INCOMPATIBLE", "FUNDING", "CASH")),
        ("RESEARCH_PRICE_HISTORY", ("HISTORY", "AWARD_HISTORY")),
    ]
    for action, keys in ordered:
        if any(k in joined for k in keys):
            return action
    if nxt.strip():
        return "HELD_FOR_ACTION"
    return "HELD_FOR_ACTION"


def _history_class(row: dict[str, Any], enriched: dict[str, Any]) -> str:
    hist = (
        enriched.get("government_price_history")
        or enriched.get("award_history")
        or row.get("government_price_history")
        or row.get("usaspending")
        or {}
    )
    if not isinstance(hist, dict):
        return "NONE"
    if hist.get("STRONG") or str(hist.get("strength") or "").upper() == "STRONG":
        return "STRONG"
    if hist.get("awards") or hist.get("prior_awards") or hist.get("unit_price"):
        n = len(hist.get("awards") or hist.get("prior_awards") or [])
        if n >= 3:
            return "STRONG"
        if n >= 1:
            return "MODERATE"
        return "WEAK"
    if hist.get("status") in {"NONE", "UNKNOWN", None} and not hist:
        return "NONE"
    blob = json.dumps(hist, default=str)
    if "UNKNOWN" in blob and "price" not in blob.lower():
        return "NONE"
    if re.search(r"\d+\.\d{2}", blob):
        return "WEAK"
    return "NONE"


def _funding_status(enriched: dict[str, Any]) -> str:
    fr = enriched.get("funding_requirement") or {}
    gate = enriched.get("owner_approval_gate") or {}
    status = str(
        enriched.get("funding_status")
        or fr.get("status")
        or gate.get("funding_status")
        or "UNKNOWN"
    ).upper()
    if status in {"SUPPORTED", "FUNDED", "AVAILABLE"}:
        return "SUPPORTED"
    if status in {"BLOCKED", "FAILED", "INSUFFICIENT", "EXHAUSTED"}:
        return "BLOCKED"
    if "POSSIBLE" in status or status in {"PARTIAL", "CANDIDATE"}:
        return "POSSIBLE_BUT_UNPROVEN"
    blockers = [str(b).upper() for b in (enriched.get("execution_critical_blockers") or [])]
    if any("FUND" in b or "FINANC" in b or "CASH" in b for b in blockers):
        return "BLOCKED"
    if enriched.get("ready_for_owner_approval") is True:
        # Gate passed — funding not blocking; still may be unproven path
        return "POSSIBLE_BUT_UNPROVEN"
    return "UNKNOWN"


def evaluate_live_row(row: dict[str, Any]) -> dict[str, Any]:
    """Cheap-screen + enrich one LIVE_SOURCE row. Never outreach."""
    from discovery.dla_product_extract import classify_federal_product_cheap, enrich_with_dla_structure
    from national_discovery_funnel import stage1_ultra_cheap

    retrieved_at = row.get("live_retrieval_timestamp") or _utc()
    stamp_live_source(row, retrieved_at=retrieved_at, source_system=str(row.get("source_id") or "sam.gov"))

    discovery_health = "healthy"
    if not (row.get("title") or row.get("solicitation_number") or row.get("external_id")):
        discovery_health = "incomplete"
    if row.get("retrieval_error") or row.get("error"):
        discovery_health = "blocked"

    try:
        row = enrich_with_dla_structure(dict(row))
    except Exception:
        pass

    cheap = stage1_ultra_cheap(row)
    fed = classify_federal_product_cheap(row)
    product_class = str(
        fed.get("cheap_screen_class") or fed.get("product_classification") or cheap.get("classification") or "UNKNOWN"
    )
    survive = bool(cheap.get("survive"))
    is_product = survive and (
        product_class in PRODUCTISH
        or fed.get("federal_product_class") in PRODUCTISH
        or str(fed.get("product_classification") or "") in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}
    )
    rejected = (not survive) or (
        str(fed.get("federal_product_class") or "") in {"FEDERAL_SERVICE", "FEDERAL_CONSTRUCTION"}
        and not is_product
    )

    enriched: dict[str, Any] = dict(row)
    enrich_error = None
    if is_product and not rejected:
        try:
            from execution_requirements.enrichment import enrich_deal_for_operator

            enriched = enrich_deal_for_operator(row, include_summary=True, include_full_profile=False)
            stamp_live_source(
                enriched,
                retrieved_at=retrieved_at,
                source_system=str(row.get("source_id") or "sam.gov"),
            )
        except Exception as e:
            enrich_error = str(e)
            discovery_health = "degraded" if discovery_health == "healthy" else discovery_health

    gate = enriched.get("owner_approval_gate") or {}
    blockers = list(enriched.get("execution_critical_blockers") or gate.get("blockers") or [])
    ready = enriched.get("ready_for_owner_approval") is True
    bucket = _blocker_bucket(blockers, gate if isinstance(gate, dict) else {})
    action = _operator_action(enriched, rejected=rejected and not is_product)
    if ready:
        action = "READY_FOR_OWNER_APPROVAL"

    return {
        "canonical_id": enriched.get("canonical_id") or row.get("canonical_id") or row.get("notice_id"),
        "source": row.get("source_id") or "fed_sam_contract_opportunities",
        "source_opportunity_id": row.get("notice_id") or row.get("external_id") or row.get("canonical_id"),
        "solicitation_number": row.get("solicitation_number") or row.get("solicitationNumber"),
        "buyer": row.get("agency") or row.get("department") or (row.get("organization") or {}).get("department"),
        "title": row.get("title"),
        "deadline": row.get("response_deadline") or row.get("deadline") or row.get("responseDeadLine"),
        "listing_url": row.get("ui_link") or row.get("url") or row.get("listing_url"),
        "listing_retrieval_timestamp": retrieved_at,
        "phase_g_provenance": PROVENANCE_LIVE_SOURCE,
        "discovery_health": discovery_health,
        "product_class": product_class,
        "federal_product_class": fed.get("federal_product_class"),
        "is_product": is_product,
        "rejected": rejected and not ready,
        "ready_for_owner_approval": ready,
        "operator_action": action,
        "operator_next_action": enriched.get("operator_next_action"),
        "blocker_bucket": bucket if not ready else "none",
        "execution_critical_blockers": blockers[:20],
        "history_class": _history_class(row, enriched),
        "funding_status": _funding_status(enriched),
        "is_dla": bool(row.get("is_dla")),
        "enrich_error": enrich_error,
        "structure_signals": fed.get("structure_signals"),
        "has_nsn": bool((fed.get("structure_signals") or {}).get("nsn")),
        "ui_link": row.get("ui_link"),
    }


def fetch_live_sam_batch(
    *,
    target: int = 100,
    max_api_calls: int = 8,
    days_back: int = 14,
    page_limit: int = 100,
) -> dict[str, Any]:
    """Authorized live SAM pull — read-only public API."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    from discovery.federal_sam_ingest import run_federal_sam_bootstrap
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    retrieved_at = _utc()
    raw = run_federal_sam_bootstrap(
        authorize_live=True,
        authorize_federal_sam=True,
        max_api_calls=max_api_calls,
        page_limit=page_limit,
        days_back=days_back,
        chunk_days=7,
        resume=False,
    )
    opps = list(raw.get("opportunities") or [])
    for o in opps:
        if isinstance(o, dict):
            stamp_live_source(o, retrieved_at=retrieved_at, source_system="sam.gov")
            o.setdefault("source_id", "fed_sam_contract_opportunities")
    return {
        "retrieved_at": retrieved_at,
        "bootstrap": {k: v for k, v in raw.items() if k != "opportunities"},
        "opportunities": opps,
        "target": target,
        "LIVE_SAM_CALLS": raw.get("LIVE_SAM_CALLS"),
        "executed": raw.get("executed"),
        "error": raw.get("error"),
    }


def run_phase_g_live_batch(
    *,
    target: int = 100,
    max_api_calls: int = 8,
    days_back: int = 14,
    enrich_limit: int | None = None,
) -> dict[str, Any]:
    """
    Supervised live funnel for Phase G.

    enrich_limit defaults to target (cap expensive profile builds).
    """
    enrich_limit = enrich_limit if enrich_limit is not None else target
    fetch = fetch_live_sam_batch(target=target, max_api_calls=max_api_calls, days_back=days_back)
    opps = fetch.get("opportunities") or []
    attempted = len(opps)
    results: list[dict[str, Any]] = []
    enrich_n = 0

    # Prefer product-looking + DLA mix without cherry-picking only easy deals
    def _sort_key(o: dict[str, Any]) -> tuple:
        title = str(o.get("title") or "")
        dla = 0 if o.get("is_dla") else 1
        nsn = 0 if re.search(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b", title) else 1
        return (dla, nsn, title.lower())

    ordered = sorted([o for o in opps if isinstance(o, dict)], key=_sort_key)

    for row in ordered:
        if enrich_n >= enrich_limit and results:
            # Still classify remaining for funnel counts without full enrich
            from national_discovery_funnel import stage1_ultra_cheap
            from discovery.dla_product_extract import classify_federal_product_cheap

            cheap = stage1_ultra_cheap(row)
            fed = classify_federal_product_cheap(row)
            survive = bool(cheap.get("survive"))
            results.append(
                {
                    "canonical_id": row.get("canonical_id") or row.get("notice_id"),
                    "title": row.get("title"),
                    "phase_g_provenance": PROVENANCE_LIVE_SOURCE,
                    "discovery_health": "healthy",
                    "product_class": fed.get("cheap_screen_class") or cheap.get("classification"),
                    "is_product": survive,
                    "rejected": not survive,
                    "ready_for_owner_approval": False,
                    "operator_action": "REJECT" if not survive else "HELD_FOR_ACTION",
                    "blocker_bucket": "enrich_capped",
                    "funding_status": "UNKNOWN",
                    "history_class": "NONE",
                    "enriched": False,
                    "listing_retrieval_timestamp": fetch.get("retrieved_at"),
                    "source": "fed_sam_contract_opportunities",
                }
            )
            continue

        ev = evaluate_live_row(row)
        ev["enriched"] = enrich_n < enrich_limit and bool(ev.get("is_product"))
        if ev.get("is_product"):
            enrich_n += 1
        results.append(ev)

    product = [r for r in results if r.get("is_product")]
    rejected = [r for r in results if r.get("rejected") and not r.get("ready_for_owner_approval")]
    ready = [r for r in results if r.get("ready_for_owner_approval")]
    held = [r for r in results if r.get("is_product") and not r.get("ready_for_owner_approval") and not r.get("rejected")]

    buckets = Counter(r.get("blocker_bucket") or "none" for r in product if not r.get("ready_for_owner_approval"))
    actions = Counter(r.get("operator_action") or "UNKNOWN" for r in results)
    history = Counter(r.get("history_class") or "NONE" for r in product)
    funding = Counter(r.get("funding_status") or "UNKNOWN" for r in product)
    provenance = Counter(r.get("phase_g_provenance") or "UNKNOWN" for r in results)

    volume_class = "sufficient" if attempted >= 50 else "insufficient-volume"
    if not fetch.get("executed"):
        volume_class = "fetch-failed"

    scorecard = {
        "kind": "PhaseGLiveScorecard",
        "retrieved_at": fetch.get("retrieved_at"),
        "volume_class": volume_class,
        "total_live_attempted": attempted,
        "successfully_retrieved": attempted if fetch.get("executed") else 0,
        "structurally_parsed": sum(1 for r in results if r.get("title")),
        "classified_as_product": len(product),
        "rejected": len(rejected),
        "held_for_action": len(held),
        "READY_FOR_OWNER_APPROVAL": len(ready),
        "blocked_by_documents_tdp": buckets.get("documents_tdp", 0),
        "blocked_by_supplier_pricing": buckets.get("supplier_pricing", 0),
        "blocked_by_funding": buckets.get("funding", 0),
        "blocked_by_deadline": buckets.get("deadline", 0),
        "blocked_by_identity_uom": buckets.get("identity_uom", 0),
        "blocked_other": buckets.get("other_blocker", 0) + buckets.get("compliance_packaging", 0),
        "historical_price_found": sum(1 for r in product if r.get("history_class") in {"STRONG", "MODERATE", "WEAK"}),
        "history_by_class": dict(history),
        "funding_by_status": dict(funding),
        "operator_actions": dict(actions),
        "provenance_counts": dict(provenance),
        "LIVE_SAM_CALLS": fetch.get("LIVE_SAM_CALLS"),
        "bootstrap_error": fetch.get("error"),
        "bootstrap_stop": (fetch.get("bootstrap") or {}).get("stop_reason"),
        "funnel_pct": {
            "product_of_retrieved": round(100.0 * len(product) / attempted, 1) if attempted else 0,
            "ready_of_product": round(100.0 * len(ready) / len(product), 1) if product else 0,
            "held_of_product": round(100.0 * len(held) / len(product), 1) if product else 0,
            "rejected_of_retrieved": round(100.0 * len(rejected) / attempted, 1) if attempted else 0,
        },
        "false_readiness_count": None,  # filled by ready audit
        "false_rejection_count": None,  # filled by rejection audit
        "external_actions": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "target": target,
    }

    report = {
        "kind": "PhaseGLiveRun",
        "scorecard": scorecard,
        "ready_cases": ready,
        "held_sample": held[:40],
        "rejected_sample": rejected[:40],
        "all_results": results,
        "bootstrap_meta": fetch.get("bootstrap"),
    }
    _write("live_run_latest.json", report)
    _write("live_scorecard_latest.json", scorecard)
    _write(
        "ready_candidates_latest.json",
        {"count": len(ready), "cases": ready, "provenance": PROVENANCE_LIVE_SOURCE},
    )
    return report


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Phase G supervised live batch (read-only)")
    p.add_argument("--target", type=int, default=100)
    p.add_argument("--max-api-calls", type=int, default=8)
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--enrich-limit", type=int, default=0, help="0 = same as target")
    args = p.parse_args()
    out = run_phase_g_live_batch(
        target=args.target,
        max_api_calls=args.max_api_calls,
        days_back=args.days_back,
        enrich_limit=args.enrich_limit or None,
    )
    sc = out["scorecard"]
    print(json.dumps({k: sc[k] for k in sc if k != "all_results"}, indent=2, default=str))
    print("artifact", ARTIFACTS / "live_run_latest.json")
