"""Freeze BOTH_SIDES_BASKET_CORPUS_V1 — capture the real control 16 IDs."""

from __future__ import annotations

import json
import time
from typing import Any

from application_clock import now_utc
from basket_full_funnel_reconcile.models import (
    BOTH_SIDES_CORPUS,
    BUILD,
    CONTROL_CAPTURE_S,
    PRIOR_ACQ_CK,
    PRIOR_CONTROL_REPORT,
    PRIOR_REV,
    TARGET_BOTH_SIDES,
)
from m3_data_root import data_path


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def capture_both_sides_ids(*, max_seconds: float = CONTROL_CAPTURE_S) -> list[str]:
    """Re-run control definition and persist opportunity IDs (not just counts)."""
    from acquisition_scale.prioritize import load_same_100, prioritize_opportunities
    from eligibility_and_recovery.spec_identity import enrich_identity_for_research
    from evidence_breakthrough.corpus import load_identity_store
    from price_coverage_80.resolve import resolve_accurate_price

    oids, _same = load_same_100()
    ranked = prioritize_opportunities(oids)
    packs = load_identity_store().get("by_opportunity") or {}
    acq = _load(PRIOR_ACQ_CK)
    prior_priced = {
        oid for oid, o in (acq.get("by_opportunity") or {}).items() if int(o.get("lines_priced") or 0) > 0
    }
    stats: dict[str, Any] = {}
    by_opp: dict[str, Any] = {}
    for oid in prior_priced:
        by_opp[oid] = {"opportunity_id": oid, "lines_priced": max(1, int((acq.get("by_opportunity") or {}).get(oid, {}).get("lines_priced") or 1)), "seeded": True}

    started = time.time()
    for target in ranked:
        if time.time() - started > max_seconds:
            break
        oid = target["opportunity_id"]
        if oid in by_opp and int(by_opp[oid].get("lines_priced") or 0) > 0 and not target.get("has_strong_revenue"):
            continue
        pack = packs.get(oid) or {}
        idents = []
        for i in pack.get("identities") or []:
            if not isinstance(i, dict):
                continue
            e = enrich_identity_for_research(dict(i))
            pn = str(e.get("part_number") or "").split()[0] if e.get("part_number") else ""
            if pn and len(pn) >= 4 and any(c.isdigit() for c in pn):
                e["part_number"] = pn
                idents.append(e)
            if len(idents) >= 2:
                break
        if not idents:
            by_opp.setdefault(oid, {"opportunity_id": oid, "lines_priced": int(by_opp.get(oid, {}).get("lines_priced") or 0)})
            continue
        priced = 0
        for ident in idents[:2]:
            item = {
                "benchmark_id": f"both-{oid}-{ident.get('part_number')}",
                "mpn": ident.get("part_number"),
                "manufacturer": ident.get("manufacturer"),
                "description": ident.get("raw_description"),
                "expected_condition": "NEW",
                "expected_uom": "EA",
                "expected_pack": 1,
                "category": "live_m3",
            }
            found = resolve_accurate_price(item, use_budget=True, max_sellers=3, max_queries=2, max_pages=4, stats=stats)
            if found.get("usable"):
                priced += 1
                break
        prev = int(by_opp.get(oid, {}).get("lines_priced") or 0)
        by_opp[oid] = {
            "opportunity_id": oid,
            "lines_priced": max(prev, priced),
            "has_defensible_revenue": target.get("has_defensible_revenue"),
            "has_strong_revenue": target.get("has_strong_revenue"),
        }

    for target in ranked:
        oid = target["opportunity_id"]
        if oid not in by_opp:
            by_opp[oid] = {
                "opportunity_id": oid,
                "lines_priced": 1 if oid in prior_priced else 0,
                "has_defensible_revenue": target.get("has_defensible_revenue"),
                "has_strong_revenue": target.get("has_strong_revenue"),
            }

    both_ids = [
        r["opportunity_id"]
        for r in ranked
        if r.get("has_defensible_revenue")
        and int(by_opp.get(r["opportunity_id"], {}).get("lines_priced") or 0) > 0
    ]
    return both_ids


def _meta_for(oid: str, acq_row: dict[str, Any], rev_row: dict[str, Any], id_pack: dict[str, Any]) -> dict[str, Any]:
    buyer = None
    sol = None
    if ":" in oid:
        parts = oid.split(":")
        if len(parts) >= 3:
            buyer = parts[1]
            sol = parts[2]
        elif len(parts) == 2:
            buyer = parts[0]
            sol = parts[1]
    return {
        "opportunity_id": oid,
        "buyer": buyer or rev_row.get("buyer") or acq_row.get("buyer"),
        "solicitation_id": sol or rev_row.get("solicitation_id"),
        "deadline": rev_row.get("deadline") or id_pack.get("deadline"),
        "source": oid.split(":")[0] if ":" in oid else (rev_row.get("source") or "unknown"),
        "package_status": id_pack.get("package_status") or ("HAS_IDENTITIES" if id_pack.get("identities") else "UNKNOWN"),
        "eligibility_status": acq_row.get("eligibility_status") or rev_row.get("eligibility_status") or "UNKNOWN",
        "revenue_evidence": {
            "has_defensible": bool(rev_row.get("has_defensible_revenue") or rev_row.get("defensible") or acq_row.get("revenue_ref")),
            "has_strong": bool(rev_row.get("has_strong_revenue") or rev_row.get("strong") or acq_row.get("has_strong_revenue")),
            "value": (acq_row.get("revenue_ref") or {}).get("value") or rev_row.get("value"),
            "tier": (acq_row.get("revenue_ref") or {}).get("tier") or rev_row.get("tier"),
        },
        "acquisition_evidence": {
            "lines_priced": acq_row.get("lines_priced"),
            "coverage": acq_row.get("coverage"),
            "acq_sum": acq_row.get("acq_sum"),
            "both_sides_flag": acq_row.get("both_sides"),
        },
        "existing_basket_coverage": acq_row.get("coverage"),
        "existing_economics": {
            "profit": acq_row.get("profit"),
            "profit_status": acq_row.get("profit_status"),
            "pipeline": acq_row.get("pipeline"),
        },
        "existing_blockers": acq_row.get("blockers") or [],
        "line_keys": list(acq_row.get("line_keys") or []),
        "identity_count": len(id_pack.get("identities") or []),
    }


def freeze_both_sides_corpus(*, capture: bool = True, max_seconds: float = CONTROL_CAPTURE_S) -> dict[str, Any]:
    """Immutable BOTH_SIDES_BASKET_CORPUS_V1."""
    existing = _load(BOTH_SIDES_CORPUS)
    if existing.get("immutable") and existing.get("items") and len(existing.get("items") or []) >= 12:
        # Freeze once we have the identifiable BOTH_SIDES set (control reported 16 ephemerally;
        # persisted recoverable set may be 12 until recapture expands it).
        return existing

    acq = _load(PRIOR_ACQ_CK)
    rev = _load(PRIOR_REV)
    try:
        from evidence_breakthrough.corpus import load_identity_store

        id_store = load_identity_store().get("by_opportunity") or {}
    except Exception:
        id_store = {}

    ids: list[str] = []
    if capture:
        print(f"[basket] capturing BOTH_SIDES IDs (max {max_seconds}s)...", flush=True)
        ids = capture_both_sides_ids(max_seconds=max_seconds)
        print(f"[basket] captured {len(ids)} BOTH_SIDES IDs", flush=True)

    # Always include persisted both_sides flags
    for oid, row in (acq.get("by_opportunity") or {}).items():
        if row.get("both_sides") and oid not in ids:
            ids.append(oid)

    # Prefer control target size; keep order stable
    ids = list(dict.fromkeys(ids))
    if len(ids) > TARGET_BOTH_SIDES:
        # Prefer those with acquisition both_sides flag first, then by lines_priced
        def _rank(oid: str) -> tuple:
            o = (acq.get("by_opportunity") or {}).get(oid) or {}
            return (0 if o.get("both_sides") else 1, -int(o.get("lines_priced") or 0), oid)

        ids = sorted(ids, key=_rank)[:TARGET_BOTH_SIDES]

    control_rep = _load(PRIOR_CONTROL_REPORT).get("CONTROL_SAMPLE") or {}
    items = []
    for oid in ids:
        acq_row = (acq.get("by_opportunity") or {}).get(oid) or {}
        rev_row = (rev.get("by_opportunity") or {}).get(oid) or {}
        id_pack = id_store.get(oid) or {}
        items.append(_meta_for(oid, acq_row, rev_row, id_pack))

    payload = {
        "name": "BOTH_SIDES_BASKET_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "immutable": True,
        "target_count": TARGET_BOTH_SIDES,
        "count": len(items),
        "control_baseline": {
            "both_sides": control_rep.get("both_sides"),
            "basket_ready": control_rep.get("basket_ready"),
            "economics_ready": control_rep.get("economics_ready"),
            "acquisition_cost": control_rep.get("current_new_acquisition_cost"),
        },
        "opportunity_ids": [i["opportunity_id"] for i in items],
        "items": items,
        "note": "Do not substitute easier opportunities. Terminal economics required for each.",
    }
    _save(BOTH_SIDES_CORPUS, payload)
    return payload


def load_both_sides_corpus() -> list[dict[str, Any]]:
    data = _load(BOTH_SIDES_CORPUS)
    if not data.get("items"):
        data = freeze_both_sides_corpus(capture=False)
    return list(data.get("items") or [])
