"""Owner-facing commercial queues: PROVEN / VERIFY / REJECT.

Only PROVEN rows appear in the bid-review queue. VERIFY stays in research.
REJECT is auditable but hidden from the default owner surface.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from application_clock import now_utc
from profit_first.router import evaluate_opportunity_profit
from verified_commercial_decision_v1 import PROVEN, REJECT, VERIFY, BUILD as VCD_BUILD

log = logging.getLogger("govtracker.owner_commercial_queue_v1")

BUILD = "20261107-m3-owner-commercial-queue-v1"


def _blocker_bucket(missing: list[str]) -> str:
    joined = " ".join(missing).upper()
    if any(x in joined for x in ("FINANCING", "PREPAYMENT", "PERSONAL_CASH", "GUARANTEE", "ADVANCE")):
        return "financing"
    if any(x in joined for x in ("ACQUISITION", "PRODUCT_ACQUISITION", "SUPPLIER", "QUOTE", "EQUIVALENT")):
        return "supplier_quotes"
    if any(x in joined for x in ("FREIGHT", "LOGISTICS", "PACKAGING", "INSURANCE")):
        return "logistics"
    if any(x in joined for x in ("PROPOSED_BID", "HISTORICAL", "GOVERNMENT_VALUE")):
        return "historical_research"
    if any(x in joined for x in ("PRODUCT", "IDENTITY", "LINE_SOURCING", "COVERAGE")):
        return "product_verification"
    return "other_requirements"


def evaluate_stored_opportunity(
    oid: str,
    rec: dict[str, Any],
    *,
    load_line_items: bool = False,
) -> dict[str, Any]:
    lie = None
    if load_line_items:
        try:
            from line_item_economics.engine import load_analysis

            lie = load_analysis(oid)
        except Exception:
            lie = None
    ev = evaluate_opportunity_profit(
        opportunity_id=oid,
        rec=rec,
        title=rec.get("title"),
        buyer=rec.get("buyer") or rec.get("agency"),
        line_item_analysis=lie,
    )
    econ = ev.get("economics") or {}
    readiness = econ.get("readiness") or VERIFY
    missing = list(econ.get("missing_requirements") or econ.get("missing_facts") or [])
    return {
        "opportunity_id": oid,
        "title": (ev.get("title") or "")[:160],
        "buyer": ev.get("buyer"),
        "deadline": rec.get("deadline") or rec.get("response_deadline"),
        "readiness": readiness,
        "profit_status": econ.get("profit_status"),
        "proposed_bid_revenue": econ.get("proposed_bid_revenue") or econ.get("expected_revenue"),
        "verified_acquisition_cost": econ.get("product_cost"),
        "verified_total_cost": econ.get("total_verified_costs"),
        "verified_financing_cost": econ.get("financing"),
        "verified_net_profit": econ.get("net_profit") or econ.get("post_financing_profit"),
        "verified_net_margin": econ.get("net_margin"),
        "verified_net_margin_pct": econ.get("net_margin_pct") or econ.get("margin_pct"),
        "financing_ready": bool(econ.get("financing_transaction_verified")),
        "owner_queue_eligible": bool(econ.get("owner_queue_eligible") or readiness == PROVEN),
        "missing_requirements": missing,
        "blocker_bucket": _blocker_bucket(missing),
        "next_action": (ev.get("owner_card") or {}).get("next"),
        "route": ev.get("route"),
        "decision_build": econ.get("build") or VCD_BUILD,
    }


def scan_owner_commercial_queues(
    *,
    limit: int = 200,
    product_only_titles: bool = True,
    prefer_non_bidnet: bool = True,
) -> dict[str, Any]:
    """Scan canonical store and classify into owner queues.

    Bounded + fail-soft: skips records that lack enough commercial fields to
    avoid multi-minute full-store evaluate storms.
    """
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    proven: list[dict[str, Any]] = []
    verify: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    buckets: Counter = Counter()
    examined = 0
    skipped_thin = 0

    # Prefer records that already carry commercial/economics hints
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for oid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        title = str(rec.get("title") or "")
        platform = " ".join(
            str(rec.get(k) or "") for k in ("platform", "source_id", "source", "authoritative_url")
        ).lower()
        if prefer_non_bidnet and "bidnet" in platform:
            continue
        if product_only_titles:
            blob = title.lower()
            if any(w in blob for w in ("service", "construction", "consulting", "janitorial")) and not any(
                w in blob for w in ("supply", "equipment", "parts", "materials", "goods")
            ):
                continue
        has_econ = bool(
            rec.get("deal_economics")
            or rec.get("phase_l_economics")
            or (isinstance(rec.get("row_ref"), dict) and (rec.get("row_ref") or {}).get("economics"))
            or rec.get("profit_first")
        )
        score = 2 if has_econ else 0
        if any(w in title.lower() for w in ("supply", "equipment", "parts", "toner", "paper", "furniture")):
            score += 1
        candidates.append((score, str(oid), rec))

    candidates.sort(key=lambda x: -x[0])
    for score, oid, rec in candidates:
        if examined >= limit:
            break
        if score <= 0 and examined > limit // 2:
            skipped_thin += 1
            continue
        examined += 1
        title = str(rec.get("title") or "")
        try:
            row = evaluate_stored_opportunity(oid, rec)
        except Exception as exc:
            row = {
                "opportunity_id": oid,
                "readiness": VERIFY,
                "missing_requirements": [f"EVAL_ERROR:{type(exc).__name__}"],
                "blocker_bucket": "other_requirements",
                "title": title[:160],
            }
        readiness = row.get("readiness")
        if readiness == PROVEN:
            proven.append(row)
        elif readiness == REJECT:
            rejected.append(row)
        else:
            verify.append(row)
            buckets[row.get("blocker_bucket") or "other_requirements"] += 1

    report = {
        "kind": "OwnerCommercialQueueReport",
        "build": BUILD,
        "decision_build": VCD_BUILD,
        "at": now_utc().isoformat(),
        "examined": examined,
        "skipped_thin_records": skipped_thin,
        "counts": {
            "PROVEN": len(proven),
            "VERIFY": len(verify),
            "REJECT": len(rejected),
        },
        "verify_blocker_counts": dict(buckets),
        "proven_opportunities": proven[:100],
        "verify_sample": verify[:50],
        "reject_sample": rejected[:30],
        "note": (
            "Only PROVEN opportunities belong in the owner bid-review queue. "
            "VERIFY continues automated research. REJECT is audited but hidden."
        ),
    }
    try:
        from m3_data_root import data_path

        path = data_path("m3_owner_commercial_queue_last_report.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("persist owner commercial queue failed")
    return report


def proven_bid_review_queue(*, limit: int = 100) -> dict[str, Any]:
    # Bound scan tightly for owner home — deep scans use commercial-queues/scan.
    full = scan_owner_commercial_queues(limit=min(max(int(limit or 50), 40), 250))
    return {
        "kind": "ProvenBidReviewQueue",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "count": full["counts"]["PROVEN"],
        "opportunities": (full.get("proven_opportunities") or [])[:limit],
        "verify_blocker_counts": full.get("verify_blocker_counts"),
        "examined": full.get("examined"),
        "persisted_path": full.get("persisted_path"),
    }
