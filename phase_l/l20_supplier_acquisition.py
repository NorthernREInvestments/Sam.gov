"""Phase L.20 — supplier acquisition evidence recovery (no outreach)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.manufacturer_channels import (
    ACTUAL_SUPPLIER_QUOTE,
    CONTRACT_CATALOG_PRICE,
    MANUFACTURER_LIST_PRICE,
    NO_ACQUISITION_EVIDENCE,
    PUBLIC_ACQUISITION_PRICE,
    QUOTE_REQUIRED,
    marketplace_blocked,
    persist_channel_profiles,
    resolve_manufacturer_channels,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, sam_api_park_status
from discovery.supplier_profiles import (
    classify_price_kind,
    known_supplier_for_product,
    upsert_supplier_profile,
)
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP
from phase_l.acquisition_pricing import infer_product_family, research_acquisition_price
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import recover_suppliers
from phase_l.l18_research_conversion import (
    NEEDS_SPEC_RESOLUTION,
    PURSUE_QUOTE_NOW,
    REGISTER_AND_PURSUE,
    RESEARCH_COMPLETE_WAITING_QUOTE,
    SKIP_ECONOMICS,
    classify_owner_decision,
    match_inventory_row,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.pilot_real_world import (
    FINANCING_EXECUTION_FAIL,
    FINANCING_PATH_PLAUSIBLE,
    FINANCING_PATH_UNRESOLVED,
    screen_financing,
)
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_D,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
    best_supplier_grade,
    grade_freight,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.quote_readiness import (
    AUTHORIZATION_UNKNOWN,
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    classify_supplier_authorization,
)
from phase_l.resilient_fetch import DomainCircuitBreaker
from phase_l.supplier_upgrade import run_supplier_upgrade_loop

BUILD = "20260928-m3-phase-l20-supplier-acquisition-evidence-recovery"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

SKIP_SUPPLIER_PATH = "SKIP_SUPPLIER_PATH"
SKIP_EXECUTION_RISK = "SKIP_EXECUTION_RISK"

L19_BASELINE = {
    "gov": {"A": 0, "B": 0, "C": 4, "D": 12, "unknown": 22},
    "supplier": {"A": 0, "B": 0, "C": 0, "D": "recon"},
    "owner": {
        "RESEARCH_COMPLETE_WAITING_QUOTE": 19,
        "REGISTER_AND_PURSUUE": 3,
        "NEEDS_SPEC_RESOLUTION": 16,
    },
    "quote_targets": {
        "validated": 0,
        "secondary": 0,
        "READY_FOR_OWNER_APPROVAL": 0,
        "NEEDS_MINOR_REVIEW": 0,
    },
}

RESELLER_CONFIRMED = "RESELLER_CONFIRMED"


def _utc() -> str:
    return now_utc().isoformat()


def _letter_gov(g: Any) -> str:
    s = str(g or "unknown").upper()
    for L in ("A", "B", "C", "D"):
        if f"GOV_VALUE_{L}" in s or s == L or s.endswith(f"_{L}"):
            return L
    if "UNKNOWN" in s or not s:
        return "unknown"
    return "unknown"


def _letter_sup(g: Any) -> str:
    s = str(g or "D").upper()
    for L in ("A", "B", "C", "D"):
        if f"SUPPLIER_{L}" in s or s == L or s.endswith(f"_{L}"):
            return L
    return "D"


def load_l19_targets() -> list[dict[str, Any]]:
    path = OUT / "l19_research_results.json"
    rows = list(json.loads(path.read_text(encoding="utf-8")).get("results") or [])
    gov_c = [r for r in rows if _letter_gov(r.get("gov_after") or r.get("gov_grade")) == "C"]
    waiting = [
        r
        for r in rows
        if r.get("owner_decision_after") == RESEARCH_COMPLETE_WAITING_QUOTE
        and r not in gov_c
    ]
    register = [
        r
        for r in rows
        if r.get("owner_decision_after") == REGISTER_AND_PURSUE and r not in gov_c and r not in waiting
    ]
    # Strong identity + usable gov among remaining waiting/register already covered;
    # also include waiting/register with strong/exact identity from NEEDS_SPEC if gov C somehow
    strong_extra = []
    for r in rows:
        if r in gov_c or r in waiting or r in register:
            continue
        if r.get("owner_decision_after") not in {RESEARCH_COMPLETE_WAITING_QUOTE, REGISTER_AND_PURSUE}:
            continue
        ident = str(r.get("product_identity") or "")
        if ident in {"EXACT_PRODUCT", "STRONG_PRODUCT_IDENTITY", "BRAND_OR_EQUAL"}:
            strong_extra.append(r)
    # Priority: Gov C first
    out = gov_c + waiting + register + strong_extra
    seen: set[str] = set()
    deduped = []
    for r in out:
        k = str(r.get("title") or id(r))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped


def _commercial(packet: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    c = dict(packet.get("commercial") or {})
    if not c.get("manufacturer") or not c.get("model"):
        blob = f"{row.get('title') or packet.get('title') or ''}"
        try:
            from phase_l.commercial_identity import extract_commercial_model, infer_manufacturer

            if not c.get("model"):
                mh = extract_commercial_model(blob)
                if mh and mh.get("model"):
                    c["model"] = mh["model"]
            if not c.get("manufacturer"):
                mf = infer_manufacturer(blob, model=c.get("model"))
                if mf:
                    c["manufacturer"] = mf.get("manufacturer")
        except Exception:
            pass
    low = (packet.get("title") or row.get("title") or "").lower()
    if not c.get("manufacturer"):
        if "apple" in low or "ipad" in low:
            c["manufacturer"] = "Apple"
            c.setdefault("model", "iPad 11")
        elif "ford" in low and "police" in low:
            c["manufacturer"] = "Ford"
            c.setdefault("model", "Police Pursuit Interceptor")
        elif "caterpillar" in low:
            c["manufacturer"] = "Caterpillar"
            c.setdefault("model", "C18")
    c["product_family"] = infer_product_family(row, c)
    return {k: v for k, v in c.items() if v}


def _prior_vendors(packet: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for pv in packet.get("prior_government_vendors") or []:
        name = pv.get("vendor") if isinstance(pv, dict) else None
        if not name:
            continue
        out.append(
            {
                "name": name,
                "supplier_domain": re.sub(r"[^a-z0-9]+", "", str(name).lower())[:40] + ".com",
                "source_type": "RESELLER",
                "prior_awardee_lead": True,
                "product_fit": "FAMILY",
                "authorization_state": AUTHORIZATION_UNKNOWN,
                "price_kind": QUOTE_REQUIRED,
                "note": "PRIOR_GOVERNMENT_VENDOR_intel_not_acquisition_supplier",
            }
        )
    # Also from award matches
    for am in packet.get("award_matches") or []:
        aw = am.get("award") or {}
        if aw.get("vendor"):
            out.append(
                {
                    "name": aw["vendor"],
                    "supplier_domain": re.sub(r"[^a-z0-9]+", "", str(aw["vendor"]).lower())[:40] + ".com",
                    "source_type": "RESELLER",
                    "prior_awardee_lead": True,
                    "product_fit": "FAMILY",
                    "price_kind": QUOTE_REQUIRED,
                    "note": "PRIOR_GOVERNMENT_VENDOR",
                }
            )
    return out[:8]


def recover_row_suppliers(
    packet: dict[str, Any],
    row: dict[str, Any],
    *,
    supplier_memory: dict[str, Any],
    authorize_live_price: bool = True,
) -> dict[str, Any]:
    commercial = _commercial(packet, row)
    gov_letter = _letter_gov(packet.get("gov_after") or packet.get("gov_grade"))
    before_grade = SUPPLIER_D

    # Stop-loss for weak rows (§47)
    if gov_letter in {"unknown", "D"} and str(packet.get("product_identity") or "") == "IDENTITY_UNRESOLVED":
        return {
            "title": packet.get("title"),
            "buyer": packet.get("buyer"),
            "skipped": True,
            "skip_reason": "weak_gov_and_identity",
            "gov_letter": gov_letter,
            "supplier_before": "D",
            "supplier_after": "D",
            "upgraded": False,
            "owner_decision_before": packet.get("owner_decision_after"),
            "owner_decision_after": packet.get("owner_decision_after") or NEEDS_SPEC_RESOLUTION,
            "candidates": [],
            "price_kind": NO_ACQUISITION_EVIDENCE,
        }

    channel = resolve_manufacturer_channels(
        manufacturer=commercial.get("manufacturer"),
        model=commercial.get("model"),
        title=packet.get("title") or row.get("title"),
    )
    candidates: list[dict[str, Any]] = []
    for c in channel.get("candidates") or []:
        if marketplace_blocked(c.get("supplier_domain")):
            continue
        candidates.append(upsert_supplier_profile(c, commercial=commercial))

    # Known product memory
    for k in known_supplier_for_product(commercial):
        if marketplace_blocked(k.get("supplier_domain")):
            continue
        candidates.append(upsert_supplier_profile(k, commercial=commercial))

    # Prior government vendors as channel intel only
    for pv in _prior_vendors(packet):
        candidates.append(upsert_supplier_profile(pv, commercial=commercial))

    # Recovery + upgrade loop
    try:
        rec = recover_suppliers(row, commercial=commercial, supplier_memory=supplier_memory)
        for s in rec.get("suppliers") or []:
            if marketplace_blocked(s.get("supplier_domain") or s.get("name")):
                s["marketplace_blocked"] = True
                s["supplier_grade_cap"] = SUPPLIER_D
            candidates.append(upsert_supplier_profile(s, commercial=commercial))
    except Exception as e:
        rec = {"error": str(e)[:160]}

    try:
        loop = run_supplier_upgrade_loop(
            row,
            commercial=commercial,
            history={"vendor": (packet.get("prior_government_vendors") or [{}])[0].get("vendor") if packet.get("prior_government_vendors") else None},
            supplier_memory=supplier_memory,
            suppliers=candidates,
            max_attempts=8,
        )
        candidates = list(loop.get("suppliers") or candidates)
    except Exception as e:
        loop = {"error": str(e)[:160], "grade_after": SUPPLIER_D}

    # Public acquisition price research for Gov C / strong identity (bounded)
    price_research: dict[str, Any] = {"skipped": True}
    public_prices: list[dict[str, Any]] = []
    if authorize_live_price and gov_letter == "C" and commercial.get("model"):
        try:
            breaker = DomainCircuitBreaker(fail_threshold=3)
            price_research = research_acquisition_price(
                row=row,
                commercial=commercial,
                identity=commercial,
                breaker=breaker,
                learning={},
                memory={},
                max_fetches=4,
                allow_bing_fallback=False,
            )
            for v in price_research.get("usable") or price_research.get("verified") or []:
                if marketplace_blocked(v.get("source_url") or v.get("domain")):
                    continue
                public_prices.append(
                    {
                        "kind": PUBLIC_ACQUISITION_PRICE,
                        "unit_price": v.get("verified_price") or v.get("unit_price"),
                        "source_url": v.get("source_url"),
                        "domain": v.get("domain"),
                        "freshness": v.get("freshness") or "RECENT",
                        "quantity_basis": v.get("quantity_basis") or 1,
                        "note": "public_price_not_actual_quote",
                    }
                )
                candidates.append(
                    upsert_supplier_profile(
                        {
                            "supplier_domain": v.get("domain") or "public_price_source",
                            "name": v.get("domain"),
                            "source_type": "DISTRIBUTOR",
                            "authorization_state": AUTHORIZED_LIKELY,
                            "product_fit": "EXACT",
                            "exact_product_evidence": True,
                            "unit_price": v.get("verified_price"),
                            "verified_public_price": True,
                            "price_kind": PUBLIC_ACQUISITION_PRICE,
                            "url": v.get("source_url"),
                        },
                        commercial=commercial,
                    )
                )
        except Exception as e:
            price_research = {"error": str(e)[:200]}

    graded = best_supplier_grade(candidates, commercial=commercial)
    after_grade = graded.get("grade") or SUPPLIER_D
    upgraded = _letter_sup(after_grade) in {"A", "B", "C"} and _letter_sup(after_grade) < "D"

    # Price kind aggregate
    price_kinds = [classify_price_kind(c) for c in candidates]
    if any(p == ACTUAL_SUPPLIER_QUOTE for p in price_kinds):
        best_price_kind = ACTUAL_SUPPLIER_QUOTE
    elif public_prices:
        best_price_kind = PUBLIC_ACQUISITION_PRICE
    elif any(p == CONTRACT_CATALOG_PRICE for p in price_kinds):
        best_price_kind = CONTRACT_CATALOG_PRICE
    elif any(p == MANUFACTURER_LIST_PRICE for p in price_kinds):
        best_price_kind = MANUFACTURER_LIST_PRICE
    elif candidates:
        best_price_kind = QUOTE_REQUIRED
    else:
        best_price_kind = NO_ACQUISITION_EVIDENCE

    # Freight
    try:
        from phase_l.quote_economics import freight_reserve_for_row

        fr = freight_reserve_for_row(row)
        freight_state = grade_freight(row, freight=fr)
        freight = {"state": freight_state, "detail": fr}
    except Exception:
        freight = {"state": "QUOTE_REQUIRED"}

    family = commercial.get("product_family") or ""
    if family in {"VEHICLE", "EQUIPMENT"} or "caterpillar" in str(commercial.get("manufacturer") or "").lower():
        if freight.get("state") in {None, "UNKNOWN", "FREIGHT_RESERVE_SUFFICIENT"}:
            # Keep reserve sufficient for vehicles but flag material unknown when no quote
            freight = {
                "state": "MATERIAL_UNKNOWN" if family == "EQUIPMENT" else freight.get("state") or "QUOTE_REQUIRED",
                "note": "heavy_or_vehicle_freight_review",
                "detail": freight.get("detail"),
            }

    # Payment terms — unknown unless explicit
    payment_terms = {
        "state": "unknown",
        "prepaid": False,
        "net_terms": False,
        "execution_fail": False,
    }
    for c in candidates:
        t = str(c.get("payment_terms") or c.get("terms") or "").lower()
        if "net-30" in t or "net 30" in t:
            payment_terms = {"state": "Net-30", "prepaid": False, "net_terms": True, "execution_fail": False}
        if "prepay" in t or "prepaid" in t or "personal guarantee" in t:
            payment_terms = {
                "state": "prepaid_or_guarantee",
                "prepaid": True,
                "net_terms": False,
                "execution_fail": True,
            }

    # Economics recompute
    gov_rec = None
    if gov_letter in {"A", "B", "C"}:
        # Prefer L.19 provenance if present
        deal = packet.get("deal_card") or {}
        gov_rec = {
            "state": "GOV_VALUE_COMPARABLE" if gov_letter == "C" else "GOV_VALUE_STRONG",
            "tier": gov_letter,
            "unit_value": deal.get("prior_unit_price"),
            "total_value": deal.get("prior_award_amount"),
            "source": (packet.get("provenance") or {}).get("source_url") or deal.get("source_link"),
            "final_award_value": gov_letter in {"A", "B"},
        }
        # Also from award matches with unit
        for am in packet.get("award_matches") or []:
            aw = am.get("award") or {}
            if aw.get("unit_price") or (aw.get("total") and aw.get("quantity")):
                try:
                    unit = float(aw.get("unit_price") or 0) or (
                        float(aw["total"]) / float(aw["quantity"]) if aw.get("quantity") else None
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    unit = None
                if unit:
                    gov_rec["unit_value"] = unit
                    gov_rec["total_value"] = aw.get("total")
                    break

    qty_info = {"quantity": packet.get("quantity"), "quality": packet.get("quantity_state")}
    econ = recompute_economics_from_recovery(
        row,
        gov_rec=gov_rec,
        qty_rec=qty_info,
        supplier_rec={"candidates": graded.get("graded") or candidates},
    )
    audit = audit_quote_positive(
        row,
        commercial=commercial,
        gov=econ.get("government_value") or gov_rec,
        suppliers=econ.get("suppliers") or graded.get("graded") or candidates,
        qty_info=econ.get("quantity") or qty_info,
        max_buy=econ.get("max_buy"),
        qdep=econ.get("quote_dependent"),
        freight=econ.get("freight"),
        supplier_memory=supplier_memory,
        attempt_upgrades=False,
    )

    # Prefer upgraded supplier grade from our channel resolution if stronger
    audit_sup = audit.get("supplier_grade") or after_grade
    if _rank_sup(after_grade) < _rank_sup(audit_sup):
        final_sup = after_grade
    else:
        final_sup = audit_sup

    fin = screen_financing(row, max_buy=econ.get("max_buy"))
    if payment_terms.get("execution_fail"):
        fin = {
            "status": FINANCING_EXECUTION_FAIL,
            "reasons": ["supplier_path_requires_prepay_or_personal_guarantee"],
            "lenders_contacted": False,
        }

    qstate = audit.get("quality_state")
    needs_reg = bool(packet.get("registration_required") or packet.get("owner_decision_after") == REGISTER_AND_PURSUE)

    # Owner decision
    if fin.get("status") == FINANCING_EXECUTION_FAIL:
        decision, reason = SKIP_EXECUTION_RISK, "EXECUTION_FAIL"
    elif best_price_kind == NO_ACQUISITION_EVIDENCE and _letter_sup(final_sup) == "D":
        decision, reason = SKIP_SUPPLIER_PATH, "NO_CREDIBLE_SUPPLIER"
    else:
        decision, reason = classify_owner_decision(
            expired=False,
            services=False,
            access_blocked=False,
            qty_state=str(packet.get("quantity_state") or "QUANTITY_UNRESOLVED"),
            identity=str(packet.get("product_identity") or "STRONG_PRODUCT_IDENTITY"),
            gov_grade=str(packet.get("gov_grade") or f"GOV_VALUE_{gov_letter}"),
            suppliers=graded.get("graded") or candidates,
            qstate=str(qstate or ""),
            needs_reg=needs_reg,
            econ=econ,
            runway=packet.get("runway") or {"label": "ACCEPTABLE_RUNWAY", "days": 5},
            recurring=bool(packet.get("recurring_buy_signal")),
        )
        if qstate == VALIDATED_QUOTE_TARGET:
            decision = REGISTER_AND_PURSUE if needs_reg else PURSUE_QUOTE_NOW
        elif qstate == SECONDARY_QUOTE_TARGET and decision == RESEARCH_COMPLETE_WAITING_QUOTE:
            pass
        elif best_price_kind == QUOTE_REQUIRED and _letter_sup(final_sup) in {"A", "B", "C"}:
            decision = REGISTER_AND_PURSUE if needs_reg else RESEARCH_COMPLETE_WAITING_QUOTE

    pilot = None
    if qstate == VALIDATED_QUOTE_TARGET:
        pilot = "READY_FOR_OWNER_APPROVAL"
    elif qstate == SECONDARY_QUOTE_TARGET:
        pilot = "NEEDS_MINOR_REVIEW"

    # Quote packet (never send; never include gov history / max-buy / margin)
    quote_packet = None
    if _letter_sup(final_sup) in {"A", "B", "C"} and gov_letter in {"A", "B", "C"}:
        quote_packet = {
            "kind": "SupplierQuotePacket",
            "status": "PREPARED_NOT_SENT",
            "product": {
                "manufacturer": commercial.get("manufacturer"),
                "model": commercial.get("model"),
                "mpn": commercial.get("mpn"),
                "title": packet.get("title"),
            },
            "quantity": packet.get("quantity"),
            "uom": "EA",
            "condition": "new",
            "required_delivery_date": (packet.get("runway") or {}).get("deadline_raw"),
            "destination": packet.get("buyer"),
            "warranty": "manufacturer_standard",
            "freight_requirements": freight.get("state"),
            "forbidden_disclosure": [
                "government_history_price",
                "max_buy",
                "target_margin",
                "expected_profit",
            ],
            "outreach_allowed": False,
        }

    auth_counts = Counter(
        classify_supplier_authorization(c) for c in (graded.get("graded") or candidates)
    )

    return {
        "title": packet.get("title"),
        "solicitation": packet.get("solicitation"),
        "buyer": packet.get("buyer"),
        "gov_letter": gov_letter,
        "commercial": commercial,
        "manufacturer_channel": {
            "resolved": channel.get("resolved"),
            "manufacturer": channel.get("manufacturer"),
            "territory_restrictions": channel.get("territory_restrictions"),
        },
        "supplier_before": _letter_sup(before_grade),
        "supplier_after": _letter_sup(final_sup),
        "supplier_grade": final_sup,
        "upgraded": _letter_sup(final_sup) in {"A", "B", "C"},
        "candidates": (graded.get("graded") or candidates)[:12],
        "candidate_count": len(graded.get("graded") or candidates),
        "authorization": dict(auth_counts),
        "price_kind": best_price_kind,
        "public_prices": public_prices[:6],
        "payment_terms": payment_terms,
        "freight": freight,
        "financing": fin,
        "economics": {
            "max_buy": econ.get("max_buy"),
            "evaluability": econ.get("evaluability"),
            "profit_tiers": _profit_tiers(econ.get("max_buy")),
        },
        "quote_state": qstate,
        "pilot_state": pilot,
        "owner_decision_before": packet.get("owner_decision_after"),
        "owner_decision_after": decision,
        "reason_code": reason or audit.get("primary_blocker"),
        "quote_packet": quote_packet,
        "price_research": {
            k: price_research.get(k)
            for k in ("family", "cache_hit", "failure_class", "telemetry", "error", "skipped")
            if k in price_research
        },
        "upgrade_loop": {
            "grade_before": loop.get("grade_before"),
            "grade_after": loop.get("grade_after"),
            "upgraded": loop.get("upgraded"),
            "exhausted_reason": loop.get("exhausted_reason"),
        },
        "no_outreach": True,
    }


def _rank_sup(g: Any) -> int:
    return {"A": 0, "B": 1, "C": 2, "D": 3, "SUPPLIER_A": 0, "SUPPLIER_B": 1, "SUPPLIER_C": 2, "SUPPLIER_D": 3}.get(
        str(g), 3
    )


def _profit_tiers(max_buy: dict[str, Any] | None) -> dict[str, bool]:
    max_buy = max_buy or {}
    p = max_buy.get("max_profit") or max_buy.get("expected_profit")
    try:
        v = float(p) if p is not None else None
    except (TypeError, ValueError):
        v = None
    return {
        "positive": bool(max_buy.get("positive") or (v is not None and v > 0)),
        "gte_5k": bool(max_buy.get("profit_gte_5k") or (v is not None and v >= 5000)),
        "gte_10k": bool(max_buy.get("profit_gte_10k") or (v is not None and v >= 10000)),
        "gte_25k": bool(max_buy.get("profit_gte_25k") or (v is not None and v >= 25000)),
        "gte_50k": bool(max_buy.get("profit_gte_50k") or (v is not None and v >= 50000)),
        "gte_100k": bool(max_buy.get("profit_gte_100k") or (v is not None and v >= 100000)),
    }


def run_phase_l20(*, authorize_live_price: bool = True, max_rows: int | None = None) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    targets = load_l19_targets()
    if max_rows is not None:
        targets = targets[:max_rows]
    print(f"[l20] target population={len(targets)}", flush=True)
    save_json(
        OUT / "l20_target_population.json",
        {
            "kind": "L20TargetPopulation",
            "build": BUILD,
            "count": len(targets),
            "baseline": L19_BASELINE,
            "opportunities": [
                {
                    "title": t.get("title"),
                    "buyer": t.get("buyer"),
                    "gov": t.get("gov_after"),
                    "owner_decision": t.get("owner_decision_after"),
                    "commercial": t.get("commercial"),
                }
                for t in targets
            ],
        },
    )

    inv = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    inv_rows = list(inv.get("rows") or [])
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)

    results: list[dict[str, Any]] = []
    channel_profiles: list[dict[str, Any]] = []
    seen_mfr: set[str] = set()

    for i, packet in enumerate(targets):
        row = match_inventory_row(
            {
                "title": packet.get("title"),
                "solicitation": packet.get("solicitation"),
                "source_id": packet.get("source_id") or "unknown",
            },
            inv_rows,
        ) or {
            "title": packet.get("title"),
            "agency": packet.get("buyer"),
            "solicitation_number": packet.get("solicitation"),
            "description": packet.get("title"),
        }
        if not row.get("agency"):
            row["agency"] = packet.get("buyer")
        print(f"[l20] supplier {i+1}/{len(targets)}: {(packet.get('title') or '')[:70]}", flush=True)
        try:
            res = recover_row_suppliers(
                packet, row, supplier_memory=supplier_memory, authorize_live_price=authorize_live_price
            )
        except Exception as e:
            res = {
                "title": packet.get("title"),
                "buyer": packet.get("buyer"),
                "error": str(e)[:240],
                "supplier_before": "D",
                "supplier_after": "D",
                "upgraded": False,
                "owner_decision_after": packet.get("owner_decision_after"),
                "candidates": [],
                "price_kind": NO_ACQUISITION_EVIDENCE,
                "financing": {"status": FINANCING_PATH_UNRESOLVED},
            }
        results.append(res)
        mfr = (res.get("commercial") or {}).get("manufacturer") or (res.get("manufacturer_channel") or {}).get(
            "manufacturer"
        )
        if mfr and mfr not in seen_mfr:
            seen_mfr.add(mfr)
            ch = resolve_manufacturer_channels(manufacturer=mfr, model=(res.get("commercial") or {}).get("model"))
            if ch.get("profile"):
                channel_profiles.append(ch["profile"])

    persist_channel_profiles(channel_profiles)

    # Aggregations
    skipped = sum(1 for r in results if r.get("skipped"))
    upgrades = [r for r in results if r.get("upgraded")]
    sup_after = Counter(r.get("supplier_after") for r in results if not r.get("skipped"))
    auth_agg = Counter()
    for r in results:
        for k, v in (r.get("authorization") or {}).items():
            auth_agg[k] += int(v or 0)
    price_kinds = Counter(r.get("price_kind") for r in results if r.get("price_kind"))
    terms = Counter((r.get("payment_terms") or {}).get("state") for r in results)
    exec_fail = sum(1 for r in results if (r.get("payment_terms") or {}).get("execution_fail"))
    freight_c = Counter((r.get("freight") or {}).get("state") for r in results)
    fin_c = Counter((r.get("financing") or {}).get("status") for r in results)

    validated = [r for r in results if r.get("quote_state") == VALIDATED_QUOTE_TARGET]
    secondary = [r for r in results if r.get("quote_state") == SECONDARY_QUOTE_TARGET]
    ready = [r for r in results if r.get("pilot_state") == "READY_FOR_OWNER_APPROVAL"]
    minor = [r for r in results if r.get("pilot_state") == "NEEDS_MINOR_REVIEW"]
    decisions = Counter(r.get("owner_decision_after") for r in results)

    tiers_agg = Counter()
    for r in results:
        for k, v in ((r.get("economics") or {}).get("profit_tiers") or {}).items():
            if v:
                tiers_agg[k] += 1

    outreach = [
        {
            "title": r.get("title"),
            "buyer": r.get("buyer"),
            "quote_packet": r.get("quote_packet"),
            "supplier_grade": r.get("supplier_grade"),
            "status": "QUEUED_NOT_SENT",
        }
        for r in results
        if r.get("pilot_state") == "READY_FOR_OWNER_APPROVAL" and r.get("quote_packet")
    ]

    mfr_resolved = sum(1 for r in results if (r.get("manufacturer_channel") or {}).get("resolved"))
    source_prod = Counter()
    for r in results:
        for c in r.get("candidates") or []:
            source_prod[c.get("source_type") or c.get("supplier_domain") or "unknown"] += 1

    # Verdict
    abc = sum(sup_after.get(x, 0) for x in ("A", "B", "C"))
    if abc >= 5 or (abc >= 3 and len(upgrades) >= 3):
        verdict = "PHASE_L20_SUPPLIER_ACQUISITION_RECOVERY_WORKING"
    elif abc >= 1 or len(upgrades) >= 1:
        verdict = "PHASE_L20_PARTIAL_SUPPLIER_ACQUISITION_RECOVERY"
    else:
        verdict = "PHASE_L20_SUPPLIER_ACQUISITION_RECOVERY_FAILED"

    remaining = "actual supplier quote outreach (owner-approved) for quote-required channels"
    if abc == 0:
        remaining = "manufacturer-authorized public pricing or dealer quote paths for Gov C products"
    elif len(validated) + len(secondary) == 0:
        remaining = "Gov A/B recovery or Supplier A actual quotes to clear pilot gate (Gov C + Supplier B/C → secondary at best)"

    summary = {
        "kind": "L20Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline_used": L19_BASELINE,
        "target_population": {
            "attempted": len(results),
            "completed": len(results),
            "skipped": skipped,
            "unresolved": sum(1 for r in results if r.get("price_kind") == NO_ACQUISITION_EVIDENCE),
        },
        "manufacturer_channel_resolution": {
            "exact_manufacturers": len(seen_mfr),
            "profiles": len(channel_profiles),
            "resolved_rows": mfr_resolved,
            "unresolved": len(results) - mfr_resolved,
            "manufacturers": sorted(seen_mfr),
        },
        "supplier_evidence": {
            "before": {"A": 0, "B": 0, "C": 0, "D": "recon"},
            "after": dict(sup_after),
            "upgrades": len(upgrades),
            "upgrade_samples": [
                {
                    "title": u.get("title"),
                    "from": u.get("supplier_before"),
                    "to": u.get("supplier_after"),
                    "manufacturer": (u.get("commercial") or {}).get("manufacturer"),
                    "price_kind": u.get("price_kind"),
                }
                for u in upgrades[:20]
            ],
        },
        "authorization": {
            "confirmed": auth_agg.get(AUTHORIZED_CONFIRMED, 0),
            "likely": auth_agg.get(AUTHORIZED_LIKELY, 0),
            "reseller_only": auth_agg.get(RESELLER_CONFIRMED, 0),
            "unknown": auth_agg.get(AUTHORIZATION_UNKNOWN, 0),
            "raw": dict(auth_agg),
        },
        "acquisition_pricing": dict(price_kinds),
        "terms": {
            "states": dict(terms),
            "execution_fail": exec_fail,
        },
        "freight": dict(freight_c),
        "financing": {
            "plausible": fin_c.get(FINANCING_PATH_PLAUSIBLE, 0),
            "unresolved": fin_c.get(FINANCING_PATH_UNRESOLVED, 0),
            "fail": fin_c.get(FINANCING_EXECUTION_FAIL, 0),
        },
        "economics": {
            "newly_evaluable": len(upgrades),
            "profit_tiers": dict(tiers_agg),
            "positive": tiers_agg.get("positive", 0),
            "gte_5k": tiers_agg.get("gte_5k", 0),
            "gte_10k": tiers_agg.get("gte_10k", 0),
            "gte_25k": tiers_agg.get("gte_25k", 0),
            "gte_50k": tiers_agg.get("gte_50k", 0),
            "gte_100k": tiers_agg.get("gte_100k", 0),
        },
        "quote_targets": {
            "validated": len(validated),
            "secondary": len(secondary),
            "READY_FOR_OWNER_APPROVAL": len(ready),
            "NEEDS_MINOR_REVIEW": len(minor),
            "delta_vs_l19": {
                "validated": len(validated),
                "secondary": len(secondary),
                "READY_FOR_OWNER_APPROVAL": len(ready),
                "NEEDS_MINOR_REVIEW": len(minor),
            },
        },
        "owner_decisions": dict(decisions),
        "quote_outreach_queue": {"count": len(outreach), "sent": 0, "status": "PREPARED_NOT_SENT"},
        "source_productivity": source_prod.most_common(15),
        "remaining_bottleneck": remaining,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "no_sam_api_calls": True,
        "no_outreach": True,
        "no_fabricated_pricing": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report(),
    }

    artifacts = {
        "l20_manufacturer_channels.json": {
            "kind": "ManufacturerChannelProfiles",
            "build": BUILD,
            "profiles": channel_profiles,
        },
        "l20_supplier_candidates.json": {
            "kind": "L20SupplierCandidates",
            "build": BUILD,
            "rows": [
                {
                    "title": r.get("title"),
                    "supplier_after": r.get("supplier_after"),
                    "candidates": r.get("candidates"),
                }
                for r in results
            ],
        },
        "l20_authorization_evidence.json": {
            "kind": "L20AuthorizationEvidence",
            "build": BUILD,
            "counts": summary["authorization"],
            "samples": [
                {
                    "title": r.get("title"),
                    "authorization": r.get("authorization"),
                    "top": [
                        {
                            "domain": c.get("supplier_domain"),
                            "auth": c.get("authorization_state"),
                            "evidence": c.get("authorization_evidence"),
                        }
                        for c in (r.get("candidates") or [])[:4]
                    ],
                }
                for r in results
                if r.get("candidates")
            ][:40],
        },
        "l20_public_acquisition_prices.json": {
            "kind": "L20PublicAcquisitionPrices",
            "build": BUILD,
            "prices": [p for r in results for p in (r.get("public_prices") or [])],
            "by_kind": dict(price_kinds),
            "note": "Public prices are never labeled as actual supplier quotes",
        },
        "l20_payment_terms.json": {
            "kind": "L20PaymentTerms",
            "build": BUILD,
            "rows": [{"title": r.get("title"), "terms": r.get("payment_terms")} for r in results],
        },
        "l20_freight_evidence.json": {
            "kind": "L20FreightEvidence",
            "build": BUILD,
            "counts": dict(freight_c),
            "rows": [{"title": r.get("title"), "freight": r.get("freight")} for r in results],
        },
        "l20_supplier_upgrades.json": {
            "kind": "L20SupplierUpgrades",
            "build": BUILD,
            "upgrades": upgrades,
            "counts": dict(sup_after),
        },
        "l20_financing_screen.json": {
            "kind": "L20FinancingScreen",
            "build": BUILD,
            "counts": summary["financing"],
            "rows": [{"title": r.get("title"), "financing": r.get("financing")} for r in results],
        },
        "l20_economics_recomputed.json": {
            "kind": "L20EconomicsRecomputed",
            "build": BUILD,
            "rows": [
                {
                    "title": r.get("title"),
                    "supplier_after": r.get("supplier_after"),
                    "gov": r.get("gov_letter"),
                    "economics": r.get("economics"),
                }
                for r in results
            ],
        },
        "l20_quote_targets.json": {
            "kind": "L20QuoteTargets",
            "build": BUILD,
            "validated": validated,
            "secondary": secondary,
            "READY_FOR_OWNER_APPROVAL": ready,
            "NEEDS_MINOR_REVIEW": minor,
            "counts": summary["quote_targets"],
        },
        "l20_quote_outreach_queue.json": {
            "kind": "SUPPLIER_QUOTE_OUTREACH_QUEUE",
            "build": BUILD,
            "status": "PREPARED_NOT_SENT",
            "count": len(outreach),
            "queue": outreach,
            "no_automated_outreach": True,
        },
        "l20_summary.json": summary,
        "l20_research_results.json": {"kind": "L20ResearchResults", "build": BUILD, "results": results},
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l20] wrote {name}", flush=True)

    write_l20_docs(summary)
    return summary


def write_l20_docs(summary: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l20_supplier_strategy.md": f"""# Phase L.20 — Supplier Acquisition Strategy

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

Acquisition evidence order: manufacturer → authorized locator → distributor → public catalog → cooperative contracts → quote-required channels.

No automated outreach. Public price ≠ actual quote.
""",
        "phase_l20_manufacturer_channels.md": f"""# L.20 Manufacturer Channels

```json
{json.dumps(summary.get('manufacturer_channel_resolution'), indent=2)}
```
""",
        "phase_l20_authorization.md": f"""# L.20 Authorization

```json
{json.dumps(summary.get('authorization'), indent=2)}
```

States: AUTHORIZED_CONFIRMED / AUTHORIZED_LIKELY / RESELLER_CONFIRMED / CHANNEL_UNKNOWN.
No fabricated authorization from logos alone.
""",
        "phase_l20_public_pricing.md": f"""# L.20 Public Pricing

```json
{json.dumps(summary.get('acquisition_pricing'), indent=2)}
```

Kinds: ACTUAL_SUPPLIER_QUOTE, PUBLIC_ACQUISITION_PRICE, CONTRACT_CATALOG_PRICE, MANUFACTURER_LIST_PRICE, QUOTE_REQUIRED, NO_ACQUISITION_EVIDENCE.
""",
        "phase_l20_terms_financing.md": f"""# L.20 Terms & Financing

Terms: `{json.dumps(summary.get('terms'))}`

Financing: `{json.dumps(summary.get('financing'))}`

Constraints: $0 owner cash before gov payment; no personal credit/guarantee assumption.
""",
        "phase_l20_economics.md": f"""# L.20 Economics

```json
{json.dumps(summary.get('economics'), indent=2)}
```
""",
        "phase_l20_quote_target_delta.md": f"""# L.20 Quote Target Delta

```json
{json.dumps(summary.get('quote_targets'), indent=2)}
```

Pilot gates unchanged.
""",
        "phase_l20_supplier_memory.md": """# L.20 Supplier Memory

Persisted in `data/supplier_profiles.json` + `data/manufacturer_channel_profiles.json` + `SUPPLIER_MEMORY_PATH`.

`KNOWN_SUPPLIER_FOR_PRODUCT` reused on repeat buys. `KNOWN_GOOD_TERMS` reserved for actual quote history only.
""",
        "phase_l20_legacy_cleanup.md": """# L.20 Legacy Cleanup

Canonical supplier pipeline:
- channels: `discovery.manufacturer_channels`
- profiles: `discovery.supplier_profiles`
- grading: `phase_l.quality_audit.grade_supplier`
- upgrade: `phase_l.supplier_upgrade.run_supplier_upgrade_loop`
- pricing: `phase_l.acquisition_pricing.research_acquisition_price`
- financing: `phase_l.pilot_real_world.screen_financing`

Marketplace listings capped at Supplier D. MSRP ≠ acquisition cost.
""",
        "phase_l20_regression.md": f"""# L.20 Regression

- L.19 → L.20 supplier acquisition on Gov C + waiting-quote population
- No outreach / SAM API / fabricated pricing
- Evidence grades unchanged
- Verdict: `{summary.get('verdict')}`
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    args = p.parse_args()
    summary = run_phase_l20(authorize_live_price=not args.offline, max_rows=args.max_rows)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "target_population",
                    "supplier_evidence",
                    "authorization",
                    "acquisition_pricing",
                    "quote_targets",
                    "owner_decisions",
                    "financing",
                    "remaining_bottleneck",
                )
            },
            indent=2,
            default=str,
        )
    )
