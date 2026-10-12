"""Finalize CGC report from validated product-page prices only."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from buyer_intelligence.commercial_budget_control_v1 import dashboard_payload
from buyer_intelligence.commercial_gap_closure_v1 import (
    _price_evidence_compatible,
    build_deals,
    prepare_financing_packets,
    prepare_top_rfqs,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.product_line_store_v1 import get_entry, get_lines


def main() -> int:
    candidates = [
        {
            "oid": "dabd3d20ddefb326",
            "mpn": "600009-HC1",
            "unit_price": "9102.52",
            "url": "https://www.mopec.com/product/product-bariatric-carrier-chassis-600009hc1-2/",
            "supplier": "mopec.com",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "newly_acquired": True,
            "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
            "qty": 3,
        },
        {
            "oid": "2cb9b5eadcc67298",
            "mpn": "ECS-1YR-R",
            "unit_price": "2126.16",
            "url": "https://www.tecisoft.com/products/ekahau-connect-subscription-license-renewal-1-license-1-year",
            "supplier": "tecisoft.com",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "newly_acquired": True,
            "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
            "qty": 1,
        },
        {
            "oid": "84a7218a90c2830a",
            "mpn": "90225 LE",
            "unit_price": "29.95",
            "url": "https://midwesttargets.com/product/hornady-90225le-9mmp-135-gr-flexloc",
            "supplier": "midwesttargets.com",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "newly_acquired": True,
            "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
        },
        {
            "oid": "84a7218a90c2830a",
            "mpn": "83286 LE",
            "unit_price": "18.45",
            "url": "https://midwesttargets.com/product/hornady-83286le-223-remington-60gr-",
            "supplier": "midwesttargets.com",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "newly_acquired": True,
            "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
        },
    ]

    accepted = []
    for p in candidates:
        lines = {str(ln.get("MPN") or "").upper(): ln for ln in get_lines(p["oid"]) if ln.get("MPN")}
        ln = lines.get(p["mpn"].upper()) or {}
        ok, reason = _price_evidence_compatible(
            mpn=p["mpn"],
            description=ln.get("DESCRIPTION"),
            manufacturer=ln.get("MANUFACTURER"),
            url=p["url"],
            supplier=p["supplier"],
        )
        if not ok:
            raise SystemExit(f"candidate rejected: {p['mpn']} {reason}")
        if p.get("qty") is None:
            p["qty"] = ln.get("QTY")
        e = get_entry(p["oid"]) or {}
        accepted.append(
            {
                **p,
                "desc": (ln.get("DESCRIPTION") or "")[:90],
                "buyer": e.get("BUYER"),
                "title": e.get("TITLE"),
                "deadline": e.get("DEADLINE"),
            }
        )

    ok_velvac, _ = _price_evidence_compatible(
        mpn="600017",
        description="MOPEC STANDARD AUTOPSY CARRIER",
        manufacturer="MOPEC",
        url="https://www.velvac.com/products/brass-non-locking-fuel-cap-600017",
        supplier="velvac",
    )
    assert not ok_velvac

    res = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_results.json") or {}
    rows = res.get("results") or []
    by: dict[str, list] = {}
    for a in accepted:
        by.setdefault(a["oid"], []).append(a)

    for r in rows:
        oid = r.get("opportunity_id")
        kept = by.get(oid) or []
        r["prices"] = kept
        r["new_prices"] = len(kept)
        acq = Decimal("0")
        have = False
        for p in kept:
            try:
                unit = Decimal(str(p["unit_price"]))
                qty = p.get("qty")
                if qty not in (None, "", 0, "0"):
                    acq += (unit * Decimal(str(qty))).quantize(Decimal("0.01"))
                    have = True
                else:
                    acq += unit
                    have = True
            except Exception:
                pass
        econ = r.get("economics") or {}
        econ["acquisition_cost"] = str(acq) if have else None
        r["economics"] = econ
        if kept and not (r.get("historical") or {}).get("usable"):
            next_a = "Recover buyer unit prices"
        elif not kept and (r.get("completeness") or {}).get("full_mandatory_coverage"):
            next_a = "Request supplier quote"
        elif (r.get("completeness") or {}).get("mandatory_complete", 0) < (
            r.get("completeness") or {}
        ).get("mandatory_count", 1):
            next_a = "Finish product identification"
        else:
            next_a = "Continue evidence recovery"
        r["operator_next_action"] = next_a

    deals_raw = build_deals(rows)
    for d in deals_raw.get("deals") or []:
        oid = d.get("opportunity_id")
        match = next((r for r in rows if r.get("opportunity_id") == oid), {})
        d["next_action"] = match.get("operator_next_action") or d.get("next_action")
        if d.get("acquisition_prices"):
            d["next_action"] = "Recover buyer unit prices"
            d["filter_tags"] = list(set((d.get("filter_tags") or []) + ["needs_pricing"]))
        elif d.get("full_mandatory"):
            d["next_action"] = "Find exact acquisition price or request supplier quote"
            d["filter_tags"] = list(
                set((d.get("filter_tags") or []) + ["supplier_quote_needed", "needs_pricing"])
            )
        else:
            d["next_action"] = "Finish product identification"

    for path in (
        "_og_commercial_research_pipeline_out/m3_cex_v1_available_deals.json",
        "_og_commercial_research_pipeline_out/m3_rse_v1_available_deals.json",
        "_og_commercial_research_pipeline_out/m3_source_product_v1_available_deals.json",
        "_og_commercial_research_pipeline_out/m3_cgc_v1_available_deals.json",
    ):
        _save(path, deals_raw)

    rfqs = prepare_top_rfqs(rows, limit=10)
    fin = prepare_financing_packets(rows, limit=10)

    fin_elig = {
        "kind": "cgc_financing_eligibility",
        "CONFIRMED": 0,
        "NOTE": "Public marketing claims only — not underwriting approval",
        "providers": [
            {
                "name": "SouthStar Capital",
                "url": "https://www.southstarcapital.com/purchase-order-financing/",
                "public_claims": {
                    "supports_gov_contracts": True,
                    "supports_product_pos": True,
                    "typical_advance": "70-90% of PO (marketing)",
                    "min_deal_size": "UNKNOWN",
                    "business_age": "UNKNOWN",
                    "pg_required": "UNKNOWN",
                    "personal_credit_required": "UNKNOWN",
                    "supplier_payment": "Pays supplier after PO verification (marketing)",
                },
                "barriers_for_us": [
                    "new LLC",
                    "zero revenue history",
                    "no written confirmation",
                    "PG/personal-credit terms unknown until underwriting",
                ],
                "confirmed": False,
            },
            {
                "name": "TradeCap Partners",
                "url": "https://www.tradecappartners.com/purchase-order-financing/",
                "public_claims": {
                    "facility_range": "$50k-$15M (marketing/LinkedIn)",
                    "supports_gov_contracts": True,
                    "min_deal_size": "appears ~$50k marketing floor",
                    "business_age": "markets to startups with management experience",
                    "pg_required": "UNKNOWN",
                },
                "barriers_for_us": [
                    "most cash-flow targets under $50k may be too small",
                    "no written confirmation",
                ],
                "confirmed": False,
            },
            {
                "name": "King Trade Capital",
                "url": "https://www.kingtradecapital.com/",
                "public_claims": {
                    "facility_range": "$50k-$15M (third-party review)",
                    "no_minimum_time_in_business_claimed": True,
                    "no_minimum_credit_score_claimed": True,
                    "supports_gov_contracts": True,
                    "pg_required": "UNKNOWN",
                },
                "barriers_for_us": [
                    "transaction size may need to clear ~$50k",
                    "PG terms unknown",
                    "no written confirmation",
                ],
                "confirmed": False,
            },
            {
                "name": "SMB Compass",
                "public_claims": {"role": "broker/marketplace not direct funder"},
                "confirmed": False,
                "barriers_for_us": ["not a direct confirmation path"],
            },
            {
                "name": "Setscale",
                "public_claims": {"UNKNOWN_from_this_run": True},
                "confirmed": False,
            },
        ],
        "realistic_structures": [
            "Supplier net terms bridging Net-30/Net-45 government pay (requires supplier approval — unconfirmed)",
            "PO financier pays supplier directly after award + supplier quote (requires award first)",
            "Factoring after invoice (needs award + delivery + invoice — not pre-bid cash)",
        ],
        "packets": fin.get("packets") or [],
    }
    _save("_og_commercial_research_pipeline_out/m3_cgc_v1_financing.json", fin_elig)
    _save("_og_commercial_research_pipeline_out/m3_financing_qualification_packets_v1.json", fin_elig)

    learning = {
        "kind": "rse_learning_from_cgc",
        "at": _utc(),
        "price_research_success_rate": {
            "raw_candidates": 66,
            "validated_exact": len(accepted),
            "rejected": 66 - len(accepted),
            "validated_rate": round(len(accepted) / 66, 3),
        },
        "failure_modes": {
            "CATEGORY_MISMATCH_MPN_COLLISION": "dominant for numeric OEM part numbers",
            "SOURCE_BLOCKED_OR_LOGIN": 93,
            "PUBLIC_PRICE_UNAVAILABLE": 29,
            "SERP_NOT_PRODUCT_PAGE": "many HTML probes",
        },
        "working_routes": [
            "mopec.com OEM catalog",
            "tecisoft authorized software reseller",
            "midwesttargets Hornady LE ammo product pages",
        ],
        "supplier_quote_conversion": "0 sent (AUTO_SEND=false)",
        "bids_submitted": 0,
        "awards": 0,
        "realized_profit": None,
        "adaptive_rules": [
            "If public price fails for industrial MPN collisions -> prepare distributor RFQ immediately",
            "Prefer OEM/category sites over generic SERP dollar extraction",
            "Cash-flow lane: Hornady ammo lines with partial public prices -> RFQ remaining SKUs",
            "Financing: sub-$50k deals unlikely for TradeCap/King marketing floors -> prioritize supplier terms",
        ],
    }
    _save("_og_commercial_research_pipeline_out/m3_rse_v1_learning_store.json", learning)

    comp = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_completeness.json") or {}
    hist = _load("_og_commercial_research_pipeline_out/m3_cgc_v1_historical_report.json") or {}
    report = {
        "kind": "m3_commercial_gap_closure_v1_FINAL",
        "build": "20261012-m3-commercial-gap-closure-v1",
        "built_at": _utc(),
        "budget": dashboard_payload(),
        "A_SCORECARD": {
            "full_mandatory_before": 4,
            "full_mandatory_after": comp.get("full_mandatory_coverage_opps"),
            "complete_bidding_lots_before": "Unknown",
            "complete_bidding_lots_after": comp.get("complete_bidding_lot_instances"),
            "exact_acquisition_prices_before": 3,
            "exact_acquisition_prices_after_including_reused": 3 + len(accepted),
            "new_validated_acquisition_prices": len(accepted),
            "reused_known_catalog": 3,
            "raw_unvalidated_candidates_discarded": 66 - len(accepted),
            "equivalent_acquisition_prices": 0,
            "margin_usable_gov_unit_prices_new": hist.get("newly_acquired_unit_prices"),
            "complete_supplier_rfqs": rfqs.get("COUNT"),
            "conditional_positive": 0,
            "verified_profitable": 0,
            "confirmed_financeable": 0,
            "execution_ready": 0,
        },
        "validated_prices": accepted,
        "B_best_opportunities": [
            {
                "product": a["title"],
                "buyer": a["buyer"],
                "deadline": a.get("deadline"),
                "mpn": a["mpn"],
                "quantity": a.get("qty"),
                "acquisition_price": a["unit_price"],
                "historical_gov_price": "UNKNOWN",
                "potential_contribution": "UNKNOWN — selling price unknown",
                "financing": "UNKNOWN",
                "next_action": "Recover buyer unit price OR request competing supplier quotes",
            }
            for a in accepted
        ],
        "rfq_count": rfqs.get("COUNT"),
        "finance": fin_elig,
        "learning": learning,
    }
    _save("_og_commercial_research_pipeline_out/m3_cgc_v1_report.json", report)
    _save("_og_commercial_research_pipeline_out/m3_cgc_v1_results.json", {"build": report["build"], "at": _utc(), "results": rows})
    print("VALIDATED", len(accepted))
    print(report["A_SCORECARD"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
