"""Commercial pilot over existing non-BidNet opportunities through production CFE path."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from buyer_intelligence.product_line_validation_v1 import (
    filter_usable_product_lines,
    validate_product_line,
)

log = logging.getLogger("govtracker.non_bidnet_commercial_pilot_v1")

BUILD = "20261016-m3-non-bidnet-commercial-pilot-v1"

_PRODUCT = __import__("re").compile(
    r"\b(supply|supplies|equipment|furniture|paper|toner|ppe|gloves|tools|"
    r"chemical|commodity|goods|materials|parts|vehicle|fleet|office)\b",
    __import__("re").I,
)


def _is_bidnet(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("platform", "source_id", "source", "authoritative_url")
    ).lower()
    return "bidnet" in blob


def inventory_non_bidnet_commercial(*, limit_scan: int = 20000) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import DEAD_FUNNEL_STATES, _is_available_rec

    store = load_store()
    stats = Counter()
    product_candidates: list[tuple[str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        stats["total"] += 1
        if _is_bidnet(rec):
            stats["bidnet"] += 1
            continue
        stats["non_bidnet"] += 1
        st = str(rec.get("current_funnel_state") or "")
        if st in DEAD_FUNNEL_STATES:
            stats["dead_funnel"] += 1
            continue
        fresh = str(rec.get("freshness") or "").upper()
        if fresh in {"EXPIRED", "CANCELED", "CANCELLED"}:
            stats["expired"] += 1
            continue
        if not (_is_available_rec(rec) or st in {"RAW", "ACCESSIBLE_PRODUCT", "WATCH", "NEEDS_SOURCE_DATA"}):
            stats["not_available"] += 1
            continue
        stats["active_non_bidnet"] += 1
        title = str(rec.get("title") or "")
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        has_docs = bool(
            rec.get("attachments_metadata")
            or (rec.get("bidnet_recovery") or {}).get("free_package_chase", {}).get("documents")
            if isinstance(rec.get("bidnet_recovery"), dict)
            else False
        )
        if has_docs:
            stats["with_package_hints"] += 1
        if _PRODUCT.search(title):
            stats["productish"] += 1
            product_candidates.append((str(cid), rec))
        if "bidnet" not in url.lower() and url.startswith("http"):
            stats["non_bidnet_url"] += 1
        if len(product_candidates) >= limit_scan:
            break
    return {
        "kind": "NonBidNetCommercialInventory",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "counts": dict(stats),
        "product_candidate_ids": [c for c, _ in product_candidates[:200]],
        "product_candidate_count": len(product_candidates),
    }


def run_non_bidnet_commercial_pilot(
    *,
    limit: int = 15,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run production run_one_opportunity on product-ish non-BidNet opportunities."""
    from buyer_intelligence.commercial_evidence_index_v1 import build_commercial_evidence_index
    from buyer_intelligence.commercial_funnel_execution_v1 import run_one_opportunity
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from phase_l.l23_full_population_funnel import load_store

    run_id = run_id or f"NBCP-{uuid4().hex[:10]}"
    inv = inventory_non_bidnet_commercial()
    store = load_store()
    evidence = build_commercial_evidence_index()
    http = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=200,
            max_requests_per_source=20,
            max_pages_per_source=3,
            max_records_per_source=50,
            max_runtime_seconds=900.0,
            max_retries=0,
            min_interval_seconds=0.3,
            timeout_seconds=25.0,
        ),
        authorize_live=True,
        user_agent="M3NonBidNetCommercialPilot/1.0",
    )

    results: list[dict[str, Any]] = []
    usable_lines_total = 0
    priced = 0
    for cid in inv.get("product_candidate_ids") or []:
        if len(results) >= limit:
            break
        rec = store.get(cid) if isinstance(store, dict) else None
        if not isinstance(rec, dict):
            continue
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        if "bidnet" in url.lower():
            # Prefer agency_source if present
            url = str(rec.get("agency_source_url") or rec.get("original_posting_url") or url)
        if not url.startswith("http") or "bidnet" in url.lower():
            continue
        item = {
            "opportunity_id": cid,
            "title": rec.get("title"),
            "buyer": rec.get("buyer") or rec.get("agency"),
            "authoritative_url": url,
            "deadline": rec.get("deadline"),
            "platform": rec.get("platform") or rec.get("source_id"),
        }
        try:
            row = run_one_opportunity(
                item,
                client=http,
                evidence_index=evidence,
                auth_status={"authenticated": False, "status": "BIDNET_ABANDONED"},
                l23_rec=rec,
                auth_client=None,
            )
        except Exception as exc:
            row = {"opportunity_id": cid, "error": f"{type(exc).__name__}:{exc}"[:200]}
        lines = row.get("line_items") or []
        validation = filter_usable_product_lines(lines if isinstance(lines, list) else [])
        usable_lines_total += int(validation.get("accepted_count") or 0)
        if row.get("public_unit_price") not in (None, "", 0, "0"):
            priced += 1
        results.append(
            {
                "opportunity_id": cid,
                "title": (item.get("title") or "")[:120],
                "buyer": item.get("buyer"),
                "url": url[:160],
                "package_status": row.get("package_status"),
                "fetch_via": row.get("fetch_via"),
                "docs": row.get("documents_downloaded") or row.get("document_count"),
                "line_count": row.get("line_count"),
                "usable_product_lines": validation.get("accepted_count"),
                "line_classes": validation.get("by_classification"),
                "public_unit_price": row.get("public_unit_price"),
                "terminal_blocker": row.get("terminal_blocker") or row.get("package_blocker"),
                "free_chase_status": row.get("free_chase_status"),
                "official_source_resolved": row.get("official_source_resolved"),
            }
        )

    report = {
        "kind": "NonBidNetCommercialPilot",
        "build": BUILD,
        "run_id": run_id,
        "at": now_utc().isoformat(),
        "inventory": inv.get("counts"),
        "attempted": len(results),
        "usable_product_lines_total": usable_lines_total,
        "verified_public_prices": priced,
        "results": results,
        "AUTO_SEND": False,
        "note": "Uses production run_one_opportunity with BidNet lane abandoned",
    }
    try:
        from m3_data_root import data_path
        import json

        path = data_path("m3_non_bidnet_commercial_pilot_last_report.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("persist commercial pilot failed")
    return report
