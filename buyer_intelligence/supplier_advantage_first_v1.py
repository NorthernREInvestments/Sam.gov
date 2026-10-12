"""Route B — Supplier-advantage-first commercial research.

Verified acquisition price → product → gov buyer history → live opp → economics.
No hypothetical discounts. AUTO_SEND=false.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from buyer_intelligence.commercial_budget_control_v1 import dashboard_payload, is_paid_research_paused
from buyer_intelligence.commercial_gap_closure_v1 import (
    _price_evidence_compatible,
    classify_opportunity_completeness,
    research_historical_unit_price,
    research_mpn_public_price,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.first_revenue_validation_sprint_v1 import _days_to_deadline
from buyer_intelligence.product_line_store_v1 import get_entry, get_lines, load_store
from buyer_intelligence.research_novelty_engine_v1 import evidence_version_hash, record_research_attempt, should_revisit
from buyer_intelligence.revenue_strategy_engine_v1 import (
    assign_lane_from_contribution,
    calculate_remaining_contribution,
)
from buyer_intelligence.selling_price_first_v1 import _LOW_FRICTION, _RESTRICTED, _usable, harvest_historical_unit_prices

log = logging.getLogger("govtracker.supplier_advantage_first_v1")

BUILD = "20261012-m3-supplier-advantage-first-v1"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_saf_v1_report.json"
CHECKPOINT = f"{OUT}/m3_saf_v1_checkpoint.json"
ADVANTAGE = f"{OUT}/m3_saf_v1_advantage_scores.json"


def _ckpt(**kwargs: Any) -> None:
    cur = _load(CHECKPOINT) or {"kind": "saf_checkpoint", "build": BUILD}
    cur.update(kwargs)
    cur["at"] = _utc()
    _save(CHECKPOINT, cur)


def supplier_advantage_score(ev: dict[str, Any]) -> dict[str, Any]:
    """Score real price evidence — category listing alone scores near zero."""
    score = 0
    reasons = []
    if ev.get("unit_price") and ev.get("url"):
        score += 40
        reasons.append("VERIFIED_UNIT_PRICE_URL")
    if ev.get("class") == "EXACT_PUBLIC_ACQUISITION_PRICE":
        score += 25
        reasons.append("EXACT_PUBLIC")
    elif ev.get("class") == "COMPLIANT_EQUIVALENT_PRICE":
        score += 10
        reasons.append("EQUIVALENT_ONLY")
    if ev.get("authorized"):
        score += 15
        reasons.append("AUTHORIZED")
    if ev.get("volume_tier"):
        score += 10
        reasons.append("VOLUME_TIER_DOCUMENTED")
    if ev.get("payment_terms"):
        score += 5
        reasons.append("TERMS_KNOWN")
    if ev.get("freight") not in (None, "", "UNKNOWN"):
        score += 5
        reasons.append("FREIGHT_KNOWN")
    if not ev.get("unit_price"):
        score = min(score, 5)
        reasons.append("NO_PRICE_CAP")
    return {"score": score, "reasons": reasons, "advantage_class": "STRONG" if score >= 60 else "MODERATE" if score >= 35 else "WEAK"}


def select_supplier_advantage_cohort(*, limit: int = 25) -> list[dict[str, Any]]:
    store = load_store(force=True)
    # Prefer prior validated CGC prices + simple MPN opps
    cgc = _load(f"{OUT}/m3_cgc_v1_report.json") or {}
    validated = list(cgc.get("validated_prices") or [])
    scored = []
    for v in validated:
        desc = str(v.get("desc") or v.get("title") or "")
        if _RESTRICTED.search(desc) or _RESTRICTED.search(str(v.get("mpn") or "")):
            # keep but lower for first-revenue
            pass
        scored.append(
            {
                "source": "CGC_VALIDATED",
                "opportunity_id": v.get("oid"),
                "mpn": v.get("mpn"),
                "unit_price": v.get("unit_price"),
                "url": v.get("url"),
                "supplier": v.get("supplier"),
                "class": v.get("class") or "EXACT_PUBLIC_ACQUISITION_PRICE",
                "qty": v.get("qty"),
                "buyer": v.get("buyer"),
                "title": v.get("title"),
                "description": desc,
                "restricted": bool(_RESTRICTED.search(desc)),
                "low_friction": bool(_LOW_FRICTION.search(desc)),
                "newly_acquired": False,  # reused from CGC
            }
        )

    for oid, e in (store.get("by_opportunity") or {}).items():
        if str(oid).startswith("__"):
            continue
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 1:
            continue
        title = str(e.get("TITLE") or "")
        if re.search(r"\b(service|construction|install|renovation)\b", title, re.I):
            continue
        lines = e.get("LINES") or []
        if not (1 <= len(lines) <= 8):
            continue
        mpns = [ln for ln in lines if ln.get("MPN")]
        if not mpns:
            continue
        desc_blob = " ".join(str(ln.get("DESCRIPTION") or "") for ln in lines)
        if _RESTRICTED.search(title + " " + desc_blob):
            continue
        score = 0
        if _LOW_FRICTION.search(title + " " + desc_blob):
            score += 40
        if 1 <= len(mpns) <= 5:
            score += 30
        if days and days >= 5:
            score += 15
        scored.append(
            {
                "source": "LIVE_SIMPLE_MPN",
                "opportunity_id": oid,
                "mpn": mpns[0].get("MPN"),
                "unit_price": None,
                "url": None,
                "buyer": e.get("BUYER"),
                "title": title,
                "deadline": e.get("DEADLINE"),
                "days_left": days,
                "line_count": len(lines),
                "exact_lines": len(mpns),
                "description": desc_blob[:160],
                "restricted": False,
                "low_friction": bool(_LOW_FRICTION.search(title + " " + desc_blob)),
                "select_score": score,
                "newly_acquired": False,
            }
        )

    # Rank: validated first, then high select_score low-friction
    validated_rows = [x for x in scored if x.get("source") == "CGC_VALIDATED"]
    live_rows = [x for x in scored if x.get("source") == "LIVE_SIMPLE_MPN"]
    live_rows.sort(key=lambda x: (-int(x.get("low_friction") or 0), -int(x.get("select_score") or 0)))
    out = []
    seen = set()
    for row in validated_rows + live_rows:
        oid = row.get("opportunity_id")
        mpn = str(row.get("mpn") or "")
        key = (oid, mpn)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    _save(f"{OUT}/m3_saf_v1_selection.json", {"build": BUILD, "at": _utc(), "count": len(out), "rows": out})
    return out


def run_supplier_advantage_first(*, cohort: int = 25, per_item_s: float = 28) -> dict[str, Any]:
    import time

    _ckpt(stage="START")
    if is_paid_research_paused():
        return {"status": "PAUSED", "reason": "M3_PAID_RESEARCH_PAUSED"}
    selected = select_supplier_advantage_cohort(limit=cohort)
    hist_store = harvest_historical_unit_prices(limit=120)
    hist_by_mpn: dict[str, list] = {}
    for r in hist_store.get("records") or []:
        mpn = str(r.get("mpn") or "").upper()
        if mpn:
            hist_by_mpn.setdefault(mpn, []).append(r)

    results = []
    new_prices = 0
    t0 = time.time()
    for i, row in enumerate(selected, 1):
        _ckpt(stage="EXECUTE", done=i, total=len(selected), oid=row.get("opportunity_id"))
        oid = row.get("opportunity_id")
        mpn = row.get("mpn")
        acq = None
        if row.get("unit_price") and row.get("url"):
            acq = {
                "unit_price": row["unit_price"],
                "url": row.get("url"),
                "supplier": row.get("supplier"),
                "class": row.get("class"),
                "newly_acquired": False,
                "route": "CGC_VALIDATED_REUSED",
                "mpn": mpn,
            }
        elif oid and mpn:
            eh = evidence_version_hash(get_entry(oid) or {})
            gate = should_revisit(
                oid,
                task="SAF_ACQUISITION_PRICE",
                method="public_price_search_v1",
                sources=["public_html"],
                current_evidence_hash=eh,
            )
            if gate.get("allowed"):
                lines = {str(ln.get("MPN") or "").upper(): ln for ln in get_lines(oid) if ln.get("MPN")}
                ln = lines.get(str(mpn).upper()) or {}
                pr = research_mpn_public_price(
                    mpn=str(mpn),
                    manufacturer=ln.get("MANUFACTURER") or ln.get("BRAND"),
                    description=ln.get("DESCRIPTION") or row.get("description"),
                    opportunity_id=oid,
                    qty=ln.get("QTY") or row.get("qty"),
                    uom=ln.get("UOM"),
                    deadline_s=per_item_s,
                )
                if pr.get("unit_price"):
                    ok, reason = _price_evidence_compatible(
                        mpn=str(mpn),
                        description=ln.get("DESCRIPTION") or row.get("description"),
                        manufacturer=ln.get("MANUFACTURER"),
                        url=pr.get("url"),
                        supplier=str(pr.get("supplier") or ""),
                    )
                    if ok:
                        acq = pr
                        if pr.get("newly_acquired"):
                            new_prices += 1
                    else:
                        acq = {"failure_class": reason, "url": pr.get("url"), "newly_acquired": False}
                else:
                    acq = pr
                record_research_attempt(
                    oid,
                    task="SAF_ACQUISITION_PRICE",
                    method="public_price_search_v1",
                    sources=[(acq or {}).get("route") or "none"],
                    status="COMPLETED" if (acq or {}).get("unit_price") else "NO_NEW_EVIDENCE",
                    prices_recovered=1 if (acq or {}).get("newly_acquired") else 0,
                    evidence_hash=eh,
                )
            else:
                acq = {"skip_reason": "NOVELTY_SUPPRESSED", "gate": gate}

        adv = supplier_advantage_score(acq or {})
        # Government history match
        gov_hits = hist_by_mpn.get(str(mpn or "").upper(), [])
        usable_gov = [h for h in gov_hits if _usable(str(h.get("match_level") or ""))]
        hist = None
        if oid:
            hist = research_historical_unit_price(oid, max_mpns=3)
            for u in hist.get("usable") or []:
                usable_gov.append(u)

        econ = None
        lane = None
        if (acq or {}).get("unit_price") and usable_gov:
            try:
                gov = Decimal(str(usable_gov[0].get("unit_price")))
                acquisition = Decimal(str(acq["unit_price"]))
                qty = row.get("qty") or 1
                # try line qty
                if oid:
                    for ln in get_lines(oid):
                        if str(ln.get("MPN") or "").upper() == str(mpn or "").upper() and ln.get("QTY") not in (None, "", 0, "0"):
                            qty = ln.get("QTY")
                            break
                econ = calculate_remaining_contribution(
                    selling_price=(gov * Decimal(str(qty))).quantize(Decimal("0.01")),
                    acquisition_cost=(acquisition * Decimal(str(qty))).quantize(Decimal("0.01")),
                    freight=None,
                    financing_fees=None,
                    allow_conditional=True,
                    conditional_assumptions={"freight": "0", "financing_fees": "0"},
                )
                if econ.get("CONTRIBUTION_STATE") in {
                    "VERIFIED_CONTRIBUTION",
                    "CONDITIONAL_CONTRIBUTION",
                    "VERIFIED_NEGATIVE_CONTRIBUTION",
                }:
                    econ["CONTRIBUTION_STATE"] = "CONDITIONAL_CONTRIBUTION"
                    econ["NOTE"] = (
                        "PRELIMINARY ONLY: historical gov unit is benchmark; "
                        "freight=0 and financing=0 assumed for screening — not bid-ready"
                    )
                lane = assign_lane_from_contribution(econ)
            except Exception:
                econ = None

        completeness = classify_opportunity_completeness(oid) if oid else {}
        results.append(
            {
                **{k: row.get(k) for k in ("opportunity_id", "buyer", "title", "deadline", "mpn", "low_friction", "restricted", "source")},
                "acquisition": acq,
                "advantage": adv,
                "gov_history_hits": len(usable_gov),
                "gov_best": usable_gov[0] if usable_gov else None,
                "economics": econ,
                "lane": lane,
                "completeness": {
                    "full_mandatory": completeness.get("full_mandatory_coverage"),
                    "mandatory_complete": completeness.get("mandatory_complete"),
                    "mandatory_count": completeness.get("mandatory_count"),
                },
            }
        )

    conditional_pos = 0
    for r in results:
        econ = r.get("economics") or {}
        if econ.get("CONTRIBUTION_STATE") == "CONDITIONAL_CONTRIBUTION":
            try:
                if float(str(econ.get("REMAINING_CONTRIBUTION") or 0) or 0) > 0:
                    conditional_pos += 1
            except Exception:
                pass

    report = {
        "kind": "m3_supplier_advantage_first_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "cohort": "B_SUPPLIER_ADVANTAGE_FIRST",
        "researched": len(results),
        "with_acquisition_price": sum(1 for r in results if (r.get("acquisition") or {}).get("unit_price")),
        "new_acquisition_prices": new_prices,
        "reused_acquisition_prices": sum(
            1 for r in results if (r.get("acquisition") or {}).get("unit_price") and not (r.get("acquisition") or {}).get("newly_acquired")
        ),
        "with_gov_history": sum(1 for r in results if r.get("gov_history_hits")),
        "strong_advantage": sum(1 for r in results if (r.get("advantage") or {}).get("advantage_class") == "STRONG"),
        "conditional_positive": conditional_pos,
        "verified_profitable": 0,
        "results": results,
        "elapsed_s": round(time.time() - t0, 2),
        "budget": dashboard_payload(),
        "sam_calls": 0,
        "openai_usd": "0",
    }
    _save(REPORT, report)
    _save(ADVANTAGE, {"build": BUILD, "at": _utc(), "rows": [{"oid": r.get("opportunity_id"), "mpn": r.get("mpn"), **(r.get("advantage") or {})} for r in results]})
    _ckpt(stage="DONE", summary={k: report[k] for k in ("researched", "new_acquisition_prices", "conditional_positive")})
    return report
