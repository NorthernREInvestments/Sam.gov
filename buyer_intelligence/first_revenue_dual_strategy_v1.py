"""Orchestrate selling-price-first + supplier-advantage + low-friction research.

Research allocation (effort, not capital): 40% SPF / 30% SAF / 20% low-friction / 10% explore.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from buyer_intelligence.commercial_budget_control_v1 import dashboard_payload
from buyer_intelligence.commercial_execution_queue_v1 import task_financing_gap, task_find_suppliers, task_prepare_rfq
from buyer_intelligence.commercial_gap_closure_v1 import (
    classify_opportunity_completeness,
    research_mpn_public_price,
    _price_evidence_compatible,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.first_revenue_validation_sprint_v1 import _days_to_deadline
from buyer_intelligence.product_line_store_v1 import get_entry, get_lines, load_store
from buyer_intelligence.research_novelty_engine_v1 import evidence_version_hash, record_research_attempt, should_revisit
from buyer_intelligence.selling_price_first_v1 import _LOW_FRICTION, _RESTRICTED, run_selling_price_first
from buyer_intelligence.supplier_advantage_first_v1 import run_supplier_advantage_first
from buyer_intelligence.supplier_outreach_queue_v1 import sync_from_rfqs

log = logging.getLogger("govtracker.first_revenue_dual_strategy_v1")

BUILD = "20261012-m3-first-revenue-dual-strategy-v1"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_frds_v1_report.json"
DEALS = f"{OUT}/m3_frds_v1_available_deals.json"
RFQS = f"{OUT}/m3_frds_v1_rfqs.json"
CHECKPOINT = f"{OUT}/m3_frds_v1_checkpoint.json"


def _ckpt(**kwargs: Any) -> None:
    cur = _load(CHECKPOINT) or {"kind": "frds_checkpoint", "build": BUILD}
    cur.update(kwargs)
    cur["at"] = _utc()
    _save(CHECKPOINT, cur)


def run_low_friction_cohort(*, limit: int = 25, per_item_s: float = 25) -> dict[str, Any]:
    import time

    store = load_store(force=True)
    cands = []
    for oid, e in (store.get("by_opportunity") or {}).items():
        if str(oid).startswith("__"):
            continue
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 1:
            continue
        title = str(e.get("TITLE") or "")
        if re.search(r"\b(service|construction|install|renovation)\b", title, re.I):
            continue
        blob = title + " " + " ".join(str(ln.get("DESCRIPTION") or "") for ln in (e.get("LINES") or [])[:12])
        if _RESTRICTED.search(blob):
            continue
        if not _LOW_FRICTION.search(blob):
            continue
        lines = e.get("LINES") or []
        if not (1 <= len(lines) <= 12):
            continue
        mpns = [ln for ln in lines if ln.get("MPN")]
        if not mpns:
            continue
        # Prefer smaller lots for first revenue
        size_bonus = 40 if len(lines) <= 5 else 15 if len(lines) <= 8 else 0
        cands.append(
            {
                "opportunity_id": oid,
                "buyer": e.get("BUYER"),
                "title": title,
                "deadline": e.get("DEADLINE"),
                "days_left": days,
                "line_count": len(lines),
                "exact_lines": len(mpns),
                "score": (50 if days and days >= 5 else 20) + size_bonus + min(30, len(mpns) * 8),
            }
        )
    cands.sort(key=lambda x: -x["score"])
    selected = cands[:limit]
    results = []
    new_prices = 0
    t0 = time.time()
    for i, opp in enumerate(selected, 1):
        _ckpt(stage="LOW_FRICTION", done=i, total=len(selected))
        oid = opp["opportunity_id"]
        prices = []
        eh = evidence_version_hash(get_entry(oid) or {})
        for ln in get_lines(oid)[:5]:
            mpn = ln.get("MPN")
            if not mpn:
                continue
            gate = should_revisit(
                oid,
                task="LOW_FRICTION_ACQUISITION",
                method="public_price_search_v1",
                sources=["public_html"],
                current_evidence_hash=eh,
            )
            if not gate.get("allowed"):
                continue
            pr = research_mpn_public_price(
                mpn=str(mpn),
                manufacturer=ln.get("MANUFACTURER") or ln.get("BRAND"),
                description=ln.get("DESCRIPTION"),
                opportunity_id=oid,
                qty=ln.get("QTY"),
                uom=ln.get("UOM"),
                deadline_s=per_item_s,
            )
            if pr.get("unit_price"):
                ok, reason = _price_evidence_compatible(
                    mpn=str(mpn),
                    description=ln.get("DESCRIPTION"),
                    manufacturer=ln.get("MANUFACTURER"),
                    url=pr.get("url"),
                    supplier=str(pr.get("supplier") or ""),
                )
                if ok:
                    prices.append(pr)
                    if pr.get("newly_acquired"):
                        new_prices += 1
                else:
                    prices.append({"mpn": mpn, "failure_class": reason, "url": pr.get("url")})
            else:
                prices.append(pr)
            record_research_attempt(
                oid,
                task="LOW_FRICTION_ACQUISITION",
                method="public_price_search_v1",
                sources=[pr.get("route") or "none"],
                status=str(pr.get("status") or "NO_NEW_EVIDENCE"),
                prices_recovered=1 if pr.get("unit_price") and pr.get("newly_acquired") else 0,
                evidence_hash=eh,
            )
        comp = classify_opportunity_completeness(oid)
        results.append({**opp, "prices": prices, "new_prices": sum(1 for p in prices if p.get("newly_acquired")), "completeness": comp})
    out = {
        "kind": "m3_low_friction_cohort_v1",
        "build": BUILD,
        "at": _utc(),
        "qualified_available": len(cands),
        "researched": len(selected),
        "new_acquisition_prices": new_prices,
        "results": results,
        "elapsed_s": round(time.time() - t0, 2),
        "NOTE": f"Requested {limit}; actual qualified simple open product opps={len(cands)}",
    }
    _save(f"{OUT}/m3_frds_v1_low_friction.json", out)
    return out


def build_unified_deals(spf: dict, saf: dict, low: dict) -> dict[str, Any]:
    deals = []
    for r in (spf.get("bridge_sample") or []) + ((_load(f"{OUT}/m3_spf_v1_acquisition_bridge.json") or {}).get("results") or []):
        econ = r.get("economics") or {}
        deals.append(
            {
                "opportunity_id": r.get("opportunity_id"),
                "product": r.get("title"),
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "strategy": "selling_price_first",
                "acquisition_prices": [{"mpn": r.get("mpn"), "unit_price": (r.get("acquisition") or {}).get("unit_price"), "url": (r.get("acquisition") or {}).get("url")}],
                "historical_unit": r.get("historical_unit_price"),
                "historical_class": r.get("historical_match_level"),
                "contribution_state": econ.get("CONTRIBUTION_STATE"),
                "conditional_remaining_contribution": econ.get("REMAINING_CONTRIBUTION")
                if econ.get("CONTRIBUTION_STATE") == "CONDITIONAL_CONTRIBUTION"
                else None,
                "lane": (r.get("lane") or {}).get("LANE") if isinstance(r.get("lane"), dict) else r.get("lane"),
                "financing_status": "FINANCING_UNKNOWN",
                "next_action": "Recover freight/terms or request supplier quote" if (r.get("acquisition") or {}).get("unit_price") else "Find acquisition price",
                "filter_tags": ["selling_price_first", "revenue_strategy", "needs_pricing"],
            }
        )
    for r in saf.get("results") or []:
        econ = r.get("economics") or {}
        deals.append(
            {
                "opportunity_id": r.get("opportunity_id"),
                "product": r.get("title"),
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "strategy": "supplier_advantage_first",
                "acquisition_prices": [
                    {
                        "mpn": r.get("mpn"),
                        "unit_price": (r.get("acquisition") or {}).get("unit_price"),
                        "url": (r.get("acquisition") or {}).get("url"),
                    }
                ],
                "historical_unit": (r.get("gov_best") or {}).get("unit_price"),
                "historical_class": (r.get("gov_best") or {}).get("match_level") or (r.get("gov_best") or {}).get("class"),
                "contribution_state": econ.get("CONTRIBUTION_STATE"),
                "conditional_remaining_contribution": econ.get("REMAINING_CONTRIBUTION")
                if econ.get("CONTRIBUTION_STATE") == "CONDITIONAL_CONTRIBUTION"
                else None,
                "advantage_class": (r.get("advantage") or {}).get("advantage_class"),
                "lane": (r.get("lane") or {}).get("LANE") if isinstance(r.get("lane"), dict) else None,
                "financing_status": "FINANCING_UNKNOWN",
                "next_action": "Compare supplier quotes / recover buyer unit price",
                "filter_tags": ["supplier_advantage_first", "revenue_strategy"]
                + (["cash_flow"] if r.get("low_friction") else [])
                + (["needs_pricing"] if not (r.get("gov_best") or {}).get("unit_price") else []),
            }
        )
    for r in low.get("results") or []:
        deals.append(
            {
                "opportunity_id": r.get("opportunity_id"),
                "product": r.get("title"),
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "strategy": "low_friction",
                "acquisition_prices": [
                    {"mpn": p.get("mpn"), "unit_price": p.get("unit_price"), "url": p.get("url")}
                    for p in (r.get("prices") or [])
                    if p.get("unit_price")
                ],
                "full_mandatory": (r.get("completeness") or {}).get("full_mandatory_coverage"),
                "contribution_state": "CONTRIBUTION_UNKNOWN",
                "financing_status": "FINANCING_UNKNOWN",
                "next_action": "Recover government unit prices",
                "filter_tags": ["low_friction", "cash_flow", "needs_pricing", "revenue_strategy"],
            }
        )
    # Dedup by oid keeping richest
    by = {}
    for d in deals:
        oid = d.get("opportunity_id")
        if not oid:
            continue
        prev = by.get(oid)
        if not prev:
            by[oid] = d
            continue
        # merge tags/strategies
        tags = list(set((prev.get("filter_tags") or []) + (d.get("filter_tags") or [])))
        if d.get("conditional_remaining_contribution") and not prev.get("conditional_remaining_contribution"):
            by[oid] = {**d, "filter_tags": tags, "strategies": list({prev.get("strategy"), d.get("strategy")})}
        else:
            prev["filter_tags"] = tags
            prev.setdefault("strategies", [prev.get("strategy")])
            if d.get("strategy") not in prev["strategies"]:
                prev["strategies"].append(d.get("strategy"))
    out_deals = list(by.values())
    out_deals.sort(
        key=lambda d: (
            0 if d.get("conditional_remaining_contribution") else 1,
            0 if d.get("acquisition_prices") else 1,
            0 if d.get("historical_unit") else 1,
        )
    )
    out = {"kind": "frds_available_deals", "build": BUILD, "built_at": _utc(), "count": len(out_deals), "deals": out_deals, "budget": dashboard_payload()}
    _save(DEALS, out)
    # Sync into CEX/RSE feeds without wiping richer CGC deals — merge
    for feed in (
        f"{OUT}/m3_cex_v1_available_deals.json",
        f"{OUT}/m3_rse_v1_available_deals.json",
        f"{OUT}/m3_source_product_v1_available_deals.json",
    ):
        existing = _load(feed) or {}
        ex_deals = existing.get("deals") or []
        have = {d.get("opportunity_id") for d in ex_deals}
        merged = list(ex_deals)
        for d in out_deals:
            if d.get("opportunity_id") not in have:
                merged.append(d)
            else:
                # enrich tags on existing
                for ed in merged:
                    if ed.get("opportunity_id") == d.get("opportunity_id"):
                        ed["filter_tags"] = list(set((ed.get("filter_tags") or []) + (d.get("filter_tags") or [])))
                        if d.get("strategy"):
                            ed.setdefault("strategies", [])
                            if d["strategy"] not in ed["strategies"]:
                                ed["strategies"].append(d["strategy"])
                        if d.get("historical_unit") and not ed.get("historical_unit"):
                            ed["historical_unit"] = d.get("historical_unit")
                            ed["historical_class"] = d.get("historical_class")
                        break
        _save(feed, {**existing, "deals": merged, "count": len(merged), "frds_merged_at": _utc(), "budget": dashboard_payload()})
    return out


def prepare_priority_rfqs(*, limit: int = 10) -> dict[str, Any]:
    deals = (_load(DEALS) or {}).get("deals") or []
    # Prefer low-friction with acquisition OR full mandatory simple
    ranked = sorted(
        deals,
        key=lambda d: (
            0 if "low_friction" in (d.get("filter_tags") or []) else 1,
            0 if d.get("acquisition_prices") else 1,
            0 if d.get("historical_unit") else 1,
            0 if "selling_price_first" in (d.get("filter_tags") or []) else 1,
        ),
    )
    packets = []
    for d in ranked:
        oid = d.get("opportunity_id")
        if not oid:
            continue
        # skip restricted first-revenue
        title = str(d.get("product") or "")
        if _RESTRICTED.search(title):
            continue
        suppliers = task_find_suppliers(oid).get("suppliers") or []
        rfq = task_prepare_rfq(oid, suppliers)
        pkt = rfq.get("packet")
        if not pkt:
            continue
        pkt = dict(pkt)
        pkt["STRATEGY"] = d.get("strategy")
        pkt["AUTO_SEND"] = False
        packets.append(pkt)
        if len(packets) >= limit:
            break
    out = {"kind": "frds_rfqs", "build": BUILD, "AUTO_SEND": False, "COUNT": len(packets), "PACKETS": packets}
    _save(RFQS, out)
    _save(f"{OUT}/m3_cex_v1_rfqs.json", {**out, "kind": "cex_rfqs", "NOTE": "Merged FRDS priority RFQs"})
    sync_from_rfqs()
    return out


def run_dual_strategy(
    *,
    spf_n: int = 25,
    saf_n: int = 25,
    low_n: int = 25,
) -> dict[str, Any]:
    _ckpt(stage="START", allocation={"spf": 0.4, "saf": 0.3, "low": 0.2, "explore": 0.1})
    spf = run_selling_price_first(cohort=spf_n)
    _ckpt(stage="SPF_DONE", spf=spf.get("researched"))
    saf = run_supplier_advantage_first(cohort=saf_n)
    _ckpt(stage="SAF_DONE", saf=saf.get("researched"))
    low = run_low_friction_cohort(limit=low_n)
    _ckpt(stage="LOW_DONE", low=low.get("researched"))
    deals = build_unified_deals(spf, saf, low)
    rfqs = prepare_priority_rfqs(limit=10)
    # financing packets for top deals with acquisition
    fin_packets = []
    for d in (deals.get("deals") or [])[:10]:
        oid = d.get("opportunity_id")
        if not oid:
            continue
        econ = {"acquisition_cost": None, "contribution": {"CONTRIBUTION_STATE": d.get("contribution_state"), "REMAINING_CONTRIBUTION": d.get("conditional_remaining_contribution")}}
        if d.get("acquisition_prices"):
            # rough sum when present
            try:
                from decimal import Decimal

                s = Decimal("0")
                for p in d["acquisition_prices"]:
                    if p.get("unit_price"):
                        s += Decimal(str(p["unit_price"]))
                econ["acquisition_cost"] = str(s)
            except Exception:
                pass
        fin = task_financing_gap(oid, econ)
        if fin.get("packet"):
            fin_packets.append(fin["packet"])
    fin_out = {
        "kind": "frds_financing",
        "build": BUILD,
        "CONFIRMED": 0,
        "AUTO_SUBMIT": False,
        "packets": fin_packets,
        "NOTE": "Advertisements are not confirmation",
    }
    _save(f"{OUT}/m3_frds_v1_financing.json", fin_out)

    report = {
        "kind": "m3_first_revenue_dual_strategy_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "allocation": {"spf": "40%", "saf": "30%", "low_friction": "20%", "explore": "10%"},
        "A_SPF": {
            "researched": spf.get("researched"),
            "usable_historical": spf.get("usable_historical_unit_prices"),
            "live_matches": spf.get("live_matches"),
            "new_acquisition_prices": spf.get("new_acquisition_prices"),
            "conditional_positive": spf.get("conditional_positive"),
        },
        "B_SAF": {
            "researched": saf.get("researched"),
            "with_acquisition": saf.get("with_acquisition_price"),
            "new_acquisition_prices": saf.get("new_acquisition_prices"),
            "reused_acquisition_prices": saf.get("reused_acquisition_prices"),
            "with_gov_history": saf.get("with_gov_history"),
            "conditional_positive": saf.get("conditional_positive"),
        },
        "C_LOW": {
            "qualified_available": low.get("qualified_available"),
            "researched": low.get("researched"),
            "new_acquisition_prices": low.get("new_acquisition_prices"),
        },
        "rfqs": rfqs.get("COUNT"),
        "finance_packets": len(fin_packets),
        "deals": deals.get("count"),
        "budget": dashboard_payload(),
        "verified_profitable": 0,
        "confirmed_financeable": 0,
        "execution_ready": 0,
        "top_deals": (deals.get("deals") or [])[:20],
    }
    _save(REPORT, report)
    _ckpt(stage="DONE")
    return report
