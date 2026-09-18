"""Live validation for product-resale discovery intelligence.

Scans pipeline store opportunities (not Iowa seed as primary).
Filters to product-purchase candidates with public evidence potential.
Does not invent economics or fabricate suppliers.
"""

from __future__ import annotations

import re
from typing import Any

from product_resale_source_intelligence import (
    BUILD_TAG,
    IOWA_SEED_SOLICITATION,
    infer_category,
    select_price_path,
    suppliers_for_category,
)

_SERVICE_DOMINANT = re.compile(
    r"\b(services?|consulting|staffing|maintenance\s+agreement|janitorial\s+services|"
    r"construction|renovation|remodel|paving\s+project|installation\s+only|"
    r"professional\s+services|training\s+services)\b",
    re.I,
)
_PRODUCT_HINT = re.compile(
    r"\b(NSN|NIIN|P/?N|part\s*number|supply|equipment|hardware|pump|motor|generator|"
    r"laptop|server|switch|tools?|PPE|safety|fleet|parts?|furniture|container|"
    r"HVAC|electrical|medical\s+device|lab(?:oratory)?)\b",
    re.I,
)


def _is_iowa_seed(row: dict[str, Any]) -> bool:
    sol = str(row.get("solicitation_number") or "")
    title = str(row.get("title") or "")
    eid = str(row.get("external_id") or row.get("canonical_id") or "")
    return (
        IOWA_SEED_SOLICITATION in sol
        or IOWA_SEED_SOLICITATION in title
        or IOWA_SEED_SOLICITATION in eid
        or "645-DOTRFB" in sol.upper()
    )


def _product_score(row: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    blob = f"{title}\n{desc}"
    cls = str(
        row.get("cheap_screen_class")
        or row.get("product_classification")
        or row.get("federal_product_class")
        or ""
    ).upper()

    if _is_iowa_seed(row):
        return -999, ["excluded_iowa_seed"]

    if cls in {"LIKELY_PRODUCT_RESALE", "CORE_PRODUCT", "PRODUCT_RESALE"}:
        score += 40
        reasons.append(f"cheap_screen={cls}")
    elif cls in {"SERVICE", "CONSTRUCTION"}:
        score -= 50
        reasons.append(f"service_or_construction_class={cls}")

    if _PRODUCT_HINT.search(blob):
        score += 20
        reasons.append("product_language")
    if _SERVICE_DOMINANT.search(title) and not _PRODUCT_HINT.search(title):
        score -= 30
        reasons.append("service_dominant_title")

    struct = row.get("dla_product_structure") or {}
    if struct.get("has_exact_nsn") or row.get("exact_nsn"):
        score += 25
        reasons.append("exact_nsn")
    if struct.get("has_exact_pn") or row.get("exact_part_number"):
        score += 15
        reasons.append("exact_pn")
    if struct.get("has_quantity") or row.get("quantity") is not None:
        score += 15
        reasons.append("quantity_known")

    if row.get("document_links") or row.get("detail_url") or row.get("description"):
        score += 10
        reasons.append("public_evidence_pointer")

    readiness = str(row.get("readiness_state") or "")
    if "COMMERCIAL_RESEARCH" in readiness or "HISTORICAL_RESEARCH" in readiness:
        score += 20
        reasons.append(f"readiness={readiness}")

    if row.get("is_dla") or str(row.get("solicitation_number") or "").upper().startswith(("SPE", "SPR")):
        score += 10
        reasons.append("dla_channel")

    bid = row.get("bid_quote_ready") or row.get("notice_semantic_class") == "BID_OR_QUOTE_READY"
    if bid:
        score += 10
        reasons.append("bid_quote_ready")

    return score, reasons


def _missing_and_next(row: dict[str, Any], reasons: list[str]) -> tuple[list[str], str]:
    missing: list[str] = []
    struct = row.get("dla_product_structure") or {}
    if not (struct.get("has_exact_nsn") or struct.get("has_exact_pn") or row.get("exact_nsn") or row.get("exact_part_number")):
        missing.append("exact_product_identity")
    if not (struct.get("has_quantity") or row.get("quantity") is not None):
        missing.append("quantity_uoi")
    if not row.get("description") or str(row.get("description") or "").startswith("http"):
        if not row.get("description_recovered"):
            missing.append("recovered_description_or_package")
    if not (row.get("recovered_documents") or (row.get("enrichment") or {}).get("docs_recovered")):
        missing.append("public_package_body")

    if "exact_product_identity" in missing:
        nxt = "Recover SAM noticedesc / public package; extract NSN or OEM+P/N"
    elif "quantity_uoi" in missing:
        nxt = "Recover line-item quantity/UOI from description or attachment"
    elif "public_package_body" in missing:
        nxt = "Fetch public resourceLinks attachment (no auth bypass) for compliance/source restrictions"
    else:
        path = select_price_path(row)
        nxt = f"Run price path `{path['selected_path_id']}` then supplier quote — do not invent economics"
    return missing, nxt


def filter_product_resale_candidates(
    rows: list[dict[str, Any]],
    *,
    min_raw: int = 100,
    top_n: int = 20,
    min_score: int = 35,
) -> dict[str, Any]:
    """Scan rows → product candidates → top N. Iowa seed never primary."""
    scanned = 0
    iowa_excluded = 0
    scored: list[tuple[int, dict[str, Any], list[str]]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned += 1
        score, reasons = _product_score(row)
        if score == -999:
            iowa_excluded += 1
            continue
        if score >= min_score:
            scored.append((score, row, reasons))

    scored.sort(key=lambda x: -x[0])
    product_candidates = len(scored)
    top20: list[dict[str, Any]] = []
    for score, row, reasons in scored[:top_n]:
        cat = infer_category(row)
        missing, nxt = _missing_and_next(row, reasons)
        price = select_price_path(row)
        suppliers = suppliers_for_category(cat)
        top20.append(
            {
                "opportunity": (row.get("title") or "")[:160],
                "solicitation_number": row.get("solicitation_number"),
                "agency": row.get("agency") or row.get("department"),
                "product": cat or "UNKNOWN_CATEGORY",
                "source": row.get("source_id") or row.get("jurisdiction"),
                "external_id": row.get("external_id") or row.get("notice_id") or row.get("canonical_id"),
                "score": score,
                "why_passed": reasons,
                "missing_information": missing,
                "next_research_action": nxt,
                "price_path_id": price["selected_path_id"],
                "supplier_seeds_for_category": [s["supplier_id"] for s in suppliers],
                "economics_invented": False,
                "fabricated_supplier_match": False,
                "iowa_seed": False,
            }
        )

    return {
        "kind": "PRODUCT_RESALE_LIVE_VALIDATION",
        "build": BUILD_TAG,
        "raw_scanned": scanned,
        "min_raw_target": min_raw,
        "raw_target_met": scanned >= min_raw,
        "iowa_excluded": iowa_excluded,
        "iowa_primary_validation": False,
        "product_candidates": product_candidates,
        "top20": top20,
        "request_counts": {"network_live_fetches": 0, "note": "validation uses existing pipeline store — no new scrape"},
    }


def run_live_validation_from_store(*, top_n: int = 20) -> dict[str, Any]:
    """Prefer durable pipeline; fall back to handoff survivor artifacts (no Iowa primary)."""
    import json
    from pathlib import Path

    rows: list[dict[str, Any]] = []
    source_used = "empty"

    try:
        from m3_discovery_service import restore_pipeline_store_from_db
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
        rows = store.all()
        if rows:
            source_used = "pipeline_store"
    except Exception:
        rows = []

    if len(rows) < 100:
        root = Path(__file__).resolve().parent
        candidates = [
            root / "artifacts" / "m3_handoff_survivors_federal-sam-7d-handoff.json",
            root / "artifacts" / "m3_federal_dla_product_intelligence_campaign.json",
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            extra: list[dict[str, Any]] = []
            if isinstance(data, dict) and isinstance(data.get("survivors"), list):
                extra = [r for r in data["survivors"] if isinstance(r, dict)]
                source_used = f"artifact:{path.name}:survivors"
            elif isinstance(data, dict) and isinstance((data.get("campaign") or {}).get("opportunities"), list):
                extra = [r for r in data["campaign"]["opportunities"] if isinstance(r, dict)]
                source_used = f"artifact:{path.name}:campaign"
            elif isinstance(data, list):
                extra = [r for r in data if isinstance(r, dict)]
                source_used = f"artifact:{path.name}"
            # Merge by external_id
            seen = {str(r.get("external_id") or r.get("notice_id") or r.get("canonical_id") or id(r)) for r in rows}
            for r in extra:
                eid = str(r.get("external_id") or r.get("notice_id") or r.get("canonical_id") or "")
                if eid and eid in seen:
                    continue
                rows.append(r)
                if eid:
                    seen.add(eid)
            if len(rows) >= 100:
                break

    out = filter_product_resale_candidates(rows, top_n=top_n)
    out["row_source"] = source_used
    return out
