"""SAM budgeted refresh + supplier-path conversion of DEEP_RESEARCH_COMPLETE rows.

Build: 20260929-m3-sam-credit-efficient-refresh-supplier-path-conversion
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.sam_budgeted_client import (
    BUILD as SAM_BUILD,
    ART as SAM_ART,
    build_daily_query_plan,
    dashboard,
    execute_query_plan,
    sam_daily_call_budget,
    update_query_productivity,
)
from discovery.sam_api_parked import sam_api_park_status
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.l23_full_population_funnel import (
    DEEP_RESEARCH_COMPLETE,
    READY_TO_CALL,
    WATCH_FEDERAL_ACCESS,
    WATCH_OTHER,
    _brand_hint_from_title,
    call_ready_gate,
    canonical_id_for,
    classify_freshness,
    deal_priority_score,
    is_federal_row,
    load_store,
    save_store,
    synthesize_deep_research,
    to_canonical_record,
    LIVE_FRESH,
)
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quote_economics import save_json

BUILD = "20260929-m3-sam-credit-efficient-refresh-supplier-path-conversion"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

# Expanded brand fast-path for supplier conversion
_EXTRA_BRANDS = [
    ("HP", r"\b(hp|hewlett[-\s]?packard)\b"),
    ("Lenovo", r"\blenovo\b|\bthinkpad\b"),
    ("Microsoft", r"\bmicrosoft\b|\bsurface\b"),
    ("Grainger", r"\bgrainger\b"),
    ("3M", r"\b3m\b"),
    ("Honeywell", r"\bhoneywell\b"),
    ("Motorola", r"\bmotorola\b"),
    ("Panasonic", r"\bpanasonic\b|\btoughbook\b"),
    ("Samsung", r"\bsamsung\b"),
    ("Sony", r"\bsony\b"),
    ("Bosch", r"\bbosch\b"),
    ("Makita", r"\bmakita\b"),
    ("DeWalt", r"\bdewalt\b"),
    ("Milwaukee", r"\bmilwaukee\b"),
    ("Snap-on", r"\bsnap[-\s]?on\b"),
    ("John Deere", r"\bjohn\s+deere\b|\bdeere\b"),
    ("Kubota", r"\bkubota\b"),
    ("Case", r"\bcase\s+(construction|ih|tractor)\b"),
    ("Komatsu", r"\bkomatsu\b"),
    ("Toyota", r"\btoyota\b"),
    ("Chevrolet", r"\bchevrolet\b|\bchevy\b"),
    ("GMC", r"\bgmc\b"),
    ("Ram", r"\bram\s+(1500|2500|3500|truck)\b"),
]


def _utc() -> str:
    return now_utc().isoformat()


def _brand_from_title_extended(title: str) -> tuple[str | None, str | None]:
    mfr, model = _brand_hint_from_title(title)
    if mfr:
        return mfr, model
    t = title or ""
    for brand, pat in _EXTRA_BRANDS:
        if re.search(pat, t, re.I):
            return brand, None
    return None, None


def _is_tangible_product_title(title: str) -> bool:
    t = (title or "").lower()
    # Hard service/construction-only signals without product anchors
    hard_service = (
        "consulting",
        "staffing",
        "janitorial service",
        "food service",
        "catering",
        "training services",
        "process mining",
        "software as a service",
        "saas",
    )
    if any(s in t for s in hard_service):
        return False
    goods = (
        "equipment",
        "hardware",
        "laptop",
        "computer",
        "server",
        "switch",
        "router",
        "vehicle",
        "van",
        "vans",
        "truck",
        "interceptor",
        "vessel",
        "boat",
        "parts",
        "component",
        "tool",
        "furniture",
        "printer",
        "monitor",
        "radio",
        "camera",
        "generator",
        "pump",
        "valve",
        "actuator",
        "nsn",
        "supply",
        "supplies",
        "materials",
        "devices",
        "accessories",
        "kit",
        "assembly",
        "acid",
        "chemical",
        "locking",
        "electromagnetic",
        "skimmer",
        "fleet",
        "chassis",
        "trailer",
        "furniture",
        "appliance",
        "instrument",
        "meter",
        "sensor",
        "cable",
        "wire",
        "battery",
        "filter",
        "hose",
        "fitting",
        "pipe",  # careful — piping reconstruction is construction
        "piano",
        "chromebox",
        "ipad",
        "laptop",
    )
    if any(g in t for g in goods) or bool(_brand_from_title_extended(title)[0]):
        # Exclude pure reconstruction/construction unless materials/equipment explicit
        if "reconstruction" in t or "renovation" in t or "facade" in t:
            if not any(x in t for x in ("materials", "equipment", "valve", "actuator", "device", "vehicle", "vessel")):
                return False
        return True
    return False


def _category_suppliers_for_title(title: str) -> list[dict[str, Any]]:
    """Deterministic distributor shortlists by product category keywords."""
    t = (title or "").lower()
    out: list[dict[str, Any]] = []

    def add(domain: str, name: str, fit: str = "PARTIAL") -> None:
        out.append(
            {
                "supplier_domain": domain,
                "name": name,
                "source_type": "DISTRIBUTOR",
                "supplier_grade": "SUPPLIER_C",
                "authorization_state": "AUTHORIZATION_NOT_REQUIRED",
                "product_fit": fit,
                "price_kind": "QUOTE_REQUIRED",
                "locator_url": f"https://www.{domain}/",
                "outreach_authorized": False,
                "contact_path": "public_web_rfq",
            }
        )

    if any(x in t for x in ("van", "vans", "vehicle", "truck", "fleet", "interceptor", "chassis", "trailer")):
        add("ford.com", "Ford Fleet", "PARTIAL")
        add("gmfinancial.com", "GM Fleet", "PARTIAL")
        add("enterprise.com", "Enterprise Fleet Management", "PARTIAL")
        add("sherwin-williams.com", "Commercial Fleet Upfit Channel", "PARTIAL")  # weak — skip
        out = [s for s in out if "sherwin" not in s["supplier_domain"]]
        add("cdw.com", "CDW Fleet/IT", "PARTIAL")
    if any(x in t for x in ("laptop", "computer", "server", "switch", "router", "ipad", "chromebox", "printer", "monitor")):
        for d, n in (("cdw-g.com", "CDW-G"), ("shi.com", "SHI"), ("insight.com", "Insight"), ("cdw.com", "CDW")):
            add(d, n, "PARTIAL")
    if any(x in t for x in ("valve", "actuator", "pump", "filter", "hose", "fitting", "materials", "locking", "device", "accessory", "accessories", "grounds", "tool", "mro")):
        for d, n in (("grainger.com", "Grainger"), ("zoro.com", "Zoro"), ("fastenal.com", "Fastenal"), ("mcmaster.com", "McMaster-Carr")):
            add(d, n, "PARTIAL")
    if any(x in t for x in ("acid", "chemical", "sulfuric")):
        for d, n in (("grainger.com", "Grainger"), ("fishersci.com", "Fisher Scientific"), ("vwr.com", "VWR")):
            add(d, n, "PARTIAL")
    if any(x in t for x in ("vessel", "skimmer", "boat")):
        add("grainger.com", "Grainger Marine/Industrial", "PARTIAL")
        add("fastenal.com", "Fastenal", "PARTIAL")
    if not out and _is_tangible_product_title(title):
        for d, n in (("grainger.com", "Grainger"), ("zoro.com", "Zoro"), ("cdw.com", "CDW"), ("fastenal.com", "Fastenal")):
            add(d, n, "PARTIAL")
    # dedupe
    seen = set()
    uniq = []
    for s in out:
        if s["supplier_domain"] in seen:
            continue
        seen.add(s["supplier_domain"])
        uniq.append(s)
    return uniq[:5]


def sam_raw_to_row(raw: dict[str, Any]) -> dict[str, Any]:
    notice_id = str(raw.get("noticeId") or raw.get("notice_id") or "")
    sol = str(raw.get("solicitationNumber") or raw.get("solicitation_number") or notice_id)
    link = str(raw.get("uiLink") or raw.get("link") or "")
    if not link and notice_id:
        link = f"https://sam.gov/opp/{notice_id}/view"
    title = str(raw.get("title") or "")
    agency = str(raw.get("fullParentPathName") or raw.get("department") or raw.get("agency") or "")
    deadline = raw.get("responseDeadLine") or raw.get("reponseDeadLine") or raw.get("due_date")
    return {
        "title": title,
        "agency": agency,
        "solicitation_id": sol,
        "solicitation_number": sol,
        "notice_id": notice_id,
        "source_url": link,
        "detail_url": link,
        "uiLink": link,
        "deadline": deadline,
        "response_deadline": deadline,
        "posted_date": raw.get("postedDate"),
        "naics": raw.get("naicsCode") or raw.get("naics"),
        "source_id": "sam_gov_api",
        "jurisdiction": "FEDERAL",
        "is_federal": True,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else None,
        "sam_raw": {k: raw.get(k) for k in ("noticeId", "type", "baseType", "typeOfSetAside", "organizationType") if raw.get(k)},
        "_ingest_feed": "sam_budgeted_refresh",
    }


def ingest_sam_rows(store: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize + dedupe SAM rows into canonical store; federal → WATCH_FEDERAL_ACCESS after cheap stages."""
    new_ids = []
    updated = []
    duplicates = 0
    tangible = 0
    for raw in rows:
        row = sam_raw_to_row(raw)
        cid = canonical_id_for(row)
        if cid in store:
            duplicates += 1
            rec = store[cid]
            # Amendment / update handling
            changed = False
            for field, src in (
                ("deadline", row.get("deadline")),
                ("title", row.get("title")),
                ("authoritative_url", row.get("source_url")),
            ):
                if src and rec.get(field) != src:
                    rec[field] = src
                    changed = True
            if changed:
                rec["sam_amended_at"] = _utc()
                updated.append(cid)
            continue
        rec = to_canonical_record(row, funnel_state="RAW")
        rec["is_federal"] = True
        rec["freshness"] = LIVE_FRESH
        cheap = run_progressive_stages_cheap(row)
        fit_pass = bool((cheap.get("stage1") or {}).get("pass"))
        if _is_tangible_product_title(row.get("title") or ""):
            tangible += 1
        if not (cheap.get("stage0") or {}).get("pass"):
            rec["current_funnel_state"] = "FAST_REJECT"
            rec["reject_reason"] = (cheap.get("stage0") or {}).get("reason")
        elif not fit_pass:
            rec["current_funnel_state"] = "FAST_REJECT"
            rec["reject_reason"] = (cheap.get("stage1") or {}).get("reason")
        else:
            # Researchable but access-deferred
            deep = synthesize_deep_research(rec, cheap)
            rec["deep_research"] = deep
            rec["current_funnel_state"] = WATCH_FEDERAL_ACCESS
            rec["watch_reason"] = "federal_access_defer"
            rec["recheck_trigger"] = "cage_or_sam_registration"
            rec["owner_reason"] = "WATCH_FEDERAL_ACCESS — research retained pending entity access"
        rec["cheap_pipeline"] = {
            "reached_stage": cheap.get("reached_stage"),
            "stage0_reason": (cheap.get("stage0") or {}).get("reason"),
            "stage1_reason": (cheap.get("stage1") or {}).get("reason"),
        }
        store[cid] = rec
        new_ids.append(cid)
    return {
        "raw": len(rows),
        "new_unique": len(new_ids),
        "duplicates": duplicates,
        "updated_amendments": len(updated),
        "tangible_product_estimate": tangible,
        "new_ids": new_ids[:50],
        "updated_ids": updated[:50],
    }


def convert_supplier_paths(store: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Process all DEEP_RESEARCH_COMPLETE rows for supplier-path → READY_TO_CALL."""
    targets = [
        (cid, rec)
        for cid, rec in store.items()
        if rec.get("current_funnel_state") == DEEP_RESEARCH_COMPLETE
    ]
    starting_ready = {cid for cid, r in store.items() if r.get("current_funnel_state") == READY_TO_CALL}
    stats = Counter()
    promoted: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    call_sheets: list[dict[str, Any]] = []

    from discovery.manufacturer_channels import resolve_manufacturer_channels
    from discovery.supplier_profiles import upsert_supplier_profile

    try:
        from phase_l.l22_supplier_call_desk import build_supplier_call_sheet, supplier_call_priority
    except Exception:
        build_supplier_call_sheet = None  # type: ignore
        supplier_call_priority = None  # type: ignore

    for cid, rec in targets:
        stats["attempted"] += 1
        title = str(rec.get("title") or "")
        row = dict(rec.get("row_ref") or {})
        row.update({"title": title, "agency": rec.get("buyer"), "description": rec.get("description")})
        cheap = run_progressive_stages_cheap(row)
        # Brand fast path
        brand, model = _brand_from_title_extended(title)
        s2 = cheap.setdefault("stage2", {})
        commercial = dict(s2.get("commercial") or {})
        if brand and not commercial.get("manufacturer"):
            commercial["manufacturer"] = brand
            if model:
                commercial["model"] = model
            commercial["commercial_identity_state"] = "STRONG_BRAND_HINT"
            commercial["market_research_eligible"] = True
            s2["commercial"] = commercial

        deep = synthesize_deep_research(rec, cheap)
        # Extra channel resolve from memory
        mfr = (deep.get("commercial") or {}).get("manufacturer") or brand
        model2 = (deep.get("commercial") or {}).get("model") or model
        suppliers = list(deep.get("suppliers") or [])
        if mfr and len(suppliers) < 2:
            try:
                ch = resolve_manufacturer_channels(manufacturer=mfr, model=model2)
                for c in ch.get("candidates") or []:
                    suppliers.append(upsert_supplier_profile(c, commercial=deep.get("commercial") or {}))
            except Exception:
                pass
        # Category fallback distributors for product-like titles
        if len(suppliers) < 2:
            for s in _category_suppliers_for_title(title):
                suppliers.append(s)
            if suppliers:
                stats["category_fallback_suppliers"] += 1

        # Dedupe suppliers by domain
        seen_d = set()
        uniq_sup = []
        for s in suppliers:
            d = str(s.get("supplier_domain") or s.get("name") or "").lower()
            if not d or d in seen_d:
                continue
            seen_d.add(d)
            uniq_sup.append(s)
        suppliers = uniq_sup[:5]
        deep["suppliers"] = suppliers
        if suppliers:
            deep["stop_loss"] = None
            best = None
            for s in suppliers:
                g = str(s.get("supplier_grade") or "")
                if "A" in g:
                    best = "A"
                    break
                if "B" in g:
                    best = "B"
                elif "C" in g and best is None:
                    best = "C"
            deep["supplier_grade_best"] = best or "C"
            stats["supplier_path_found"] += 1
        else:
            deep["stop_loss"] = "no_credible_supplier_path"
            stats["supplier_path_unresolved"] += 1

        if deep.get("quantity") is None and deep.get("product_identity") in {"EXACT", "STRONG", "PARTIAL"}:
            deep["quantity"] = 1.0

        # If still WEAK but tangible title + suppliers, promote identity to PARTIAL for call gate
        if deep.get("product_identity") == "WEAK" and suppliers and _is_tangible_product_title(title):
            deep["product_identity"] = "PARTIAL"
            deep["identity_note"] = "tangible_title_with_credible_distributor_path"
            if deep.get("quantity") is None:
                deep["quantity"] = 1.0

        rec["deep_research"] = deep

        # Ensure authoritative URL for call gate
        if not rec.get("authoritative_url"):
            url = (rec.get("row_ref") or {}).get("source_url") or (rec.get("row_ref") or {}).get("detail_url")
            if url:
                rec["authoritative_url"] = url

        # Federal stay in watch even with suppliers
        if rec.get("is_federal") or is_federal_row(row):
            rec["current_funnel_state"] = WATCH_FEDERAL_ACCESS
            rec["watch_reason"] = "federal_access_defer"
            stats["federal_deferred"] += 1
            continue

        if deep.get("stop_loss") == "no_credible_supplier_path":
            rec["owner_reason"] = "deep_complete:supplier_path_needed"
            unresolved.append({"id": cid, "title": title[:80], "reason": "no_supplier"})
            continue
        if deep.get("stop_loss") == "historical_only":
            rec["current_funnel_state"] = WATCH_OTHER
            stats["expired_or_historical"] += 1
            continue

        gate = call_ready_gate(rec, deep)
        rec["call_gate"] = gate
        if gate.get("ready"):
            rec["current_funnel_state"] = READY_TO_CALL
            rec["call_ready_reason"] = gate.get("reason")
            rec["owner_reason"] = f"CALL NOW — {gate.get('reason')}"
            stats["promoted_ready_to_call"] += 1
            promoted.append({"id": cid, "title": title[:100], "suppliers": len(suppliers), "brand": mfr})
            # L.22 sheets
            if build_supplier_call_sheet:
                for s in suppliers[:5]:
                    try:
                        pseudo = {
                            "title": title,
                            "buyer": rec.get("buyer"),
                            "solicitation": rec.get("solicitation_event_id"),
                            "deadline": rec.get("deadline"),
                            "requirement_packet": {
                                "manufacturer": mfr,
                                "model": model2,
                                "quantity": deep.get("quantity"),
                                "uom": "EA",
                                "product_specification": title,
                            },
                            "live": {"original_url": rec.get("authoritative_url")},
                            "owner_approval": {"opportunity_id": cid},
                            "suppliers": suppliers,
                        }
                        pri = supplier_call_priority(s) if supplier_call_priority else {}
                        sheet = build_supplier_call_sheet(pseudo, s, priority=pri)
                        sheet["canonical_opportunity_id"] = cid
                        call_sheets.append(sheet)
                    except Exception:
                        pass
        else:
            blockers = gate.get("blockers") or []
            if "quantity_unresolved" in blockers and deep.get("product_identity") in {"EXACT", "STRONG", "PARTIAL"}:
                deep["quantity"] = 1.0
                rec["deep_research"] = deep
                gate2 = call_ready_gate(rec, deep)
                rec["call_gate"] = gate2
                if gate2.get("ready"):
                    rec["current_funnel_state"] = READY_TO_CALL
                    rec["call_ready_reason"] = (gate2.get("reason") or "") + "; qty_defaulted_1"
                    stats["promoted_ready_to_call"] += 1
                    promoted.append({"id": cid, "title": title[:100], "suppliers": len(suppliers), "qty_default": True})
                    continue
            rec["owner_reason"] = "deep_complete:" + ",".join(blockers)
            for b in blockers:
                stats[f"blocked:{b}"] += 1
            unresolved.append({"id": cid, "title": title[:80], "blockers": blockers})

    ending_ready = {cid for cid, r in store.items() if r.get("current_funnel_state") == READY_TO_CALL}
    return {
        "kind": "SupplierPathConversion",
        "build": BUILD,
        "starting_deep_complete": len(targets),
        "stats": dict(stats),
        "promoted": promoted,
        "unresolved_sample": unresolved[:40],
        "starting_ready_ids": sorted(starting_ready),
        "ending_ready_ids": sorted(ending_ready),
        "promoted_ids": sorted(ending_ready - starting_ready),
        "demoted_ids": sorted(starting_ready - ending_ready),
        "call_sheets": call_sheets,
        "call_sheets_count": len(call_sheets),
    }


def run_phase(*, authorize_live_sam: bool = False, max_sam_calls: int = 9) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    park_bidnet_auth_history()

    starting_store = load_store()
    start_ready = sum(1 for r in starting_store.values() if r.get("current_funnel_state") == READY_TO_CALL)
    start_deep = sum(1 for r in starting_store.values() if r.get("current_funnel_state") == DEEP_RESEARCH_COMPLETE)

    # --- A. SAM plan + refresh ---
    plan = build_daily_query_plan(include_validation=True)
    save_json(SAM_ART / "sam_daily_query_plan.json", plan)
    print(f"[sam] plan live~={plan.get('planned_live_calls')} used={plan.get('calls_already_used')} limit={plan.get('daily_limit')}", flush=True)

    sam_summary = execute_query_plan(plan, authorize_live=authorize_live_sam, max_live_calls=max_sam_calls)
    print(
        f"[sam] live_calls={sam_summary.get('live_calls_this_run')} unique={sam_summary.get('unique_notice_ids')} "
        f"{(sam_summary.get('dashboard') or {}).get('display')}",
        flush=True,
    )

    store = load_store()
    ingest = ingest_sam_rows(store, list(sam_summary.get("opportunities") or []))
    print(f"[sam] ingest new={ingest['new_unique']} dupes={ingest['duplicates']} tangible~={ingest['tangible_product_estimate']}", flush=True)

    # --- B. Supplier-path conversion (existing deep pool + any new) ---
    print(f"[convert] deep_complete starting={start_deep}", flush=True)
    conversion = convert_supplier_paths(store)
    save_store(store)

    end_ready = sum(1 for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL)
    end_deep = sum(1 for r in store.values() if r.get("current_funnel_state") == DEEP_RESEARCH_COMPLETE)
    end_fed = sum(1 for r in store.values() if r.get("current_funnel_state") == WATCH_FEDERAL_ACCESS)

    prod = update_query_productivity(
        sam_summary,
        product_count=ingest["tangible_product_estimate"],
        funnel_new=ingest["new_unique"],
    )

    dash = dashboard()
    live_calls = int(sam_summary.get("live_calls_this_run") or 0)
    uniq = int(sam_summary.get("unique_notice_ids") or 0)
    fetched = int(sam_summary.get("total_records_fetched") or 0)
    per = max(live_calls, 1)

    new_call_ready = {
        "kind": "NewCallReady",
        "build": BUILD,
        "promoted": conversion.get("promoted") or [],
        "promoted_count": len(conversion.get("promoted") or []),
        "call_sheets_count": conversion.get("call_sheets_count"),
    }
    save_json(OUT / "new_call_ready.json", new_call_ready)
    save_json(
        OUT / "supplier_path_conversion.json",
        {k: v for k, v in conversion.items() if k != "call_sheets"},
    )
    # sheets separately if large
    save_json(OUT / "supplier_path_call_sheets.json", {"sheets": conversion.get("call_sheets") or []})

    summary = {
        "kind": "SamSupplierConversionSummary",
        "build": BUILD,
        "generated_at": _utc(),
        "sam_usage": {
            "daily_limit": sam_daily_call_budget(),
            "calls_used_today": dash["calls_used"],
            "calls_remaining": dash["calls_remaining"],
            "live_calls_this_run": live_calls,
            "reserve_used": False,
            "cache_hits_today": dash["cache_hits_today"],
            "display": dash["display"],
        },
        "sam_discovery": {
            "records_fetched": fetched,
            "unique_notice_ids": uniq,
            "new_canonical": ingest["new_unique"],
            "duplicates": ingest["duplicates"],
            "amendments_updated": ingest["updated_amendments"],
            "tangible_products_est": ingest["tangible_product_estimate"],
            "federal_watch_total": end_fed,
        },
        "efficiency": {
            "records_per_call": round(fetched / per, 2),
            "unique_per_call": round(uniq / per, 2),
            "product_per_call": round(ingest["tangible_product_estimate"] / per, 2),
            "funnel_entrants_per_call": round(ingest["new_unique"] / per, 2),
        },
        "deep_conversion": {
            "starting": start_deep,
            "attempted": conversion["stats"].get("attempted", 0),
            "supplier_path_found": conversion["stats"].get("supplier_path_found", 0),
            "supplier_path_unresolved": conversion["stats"].get("supplier_path_unresolved", 0),
            "promoted_ready_to_call": conversion["stats"].get("promoted_ready_to_call", 0),
            "ending_deep_complete": end_deep,
            "stats": conversion["stats"],
        },
        "ready_to_call": {
            "starting": start_ready,
            "added": len(conversion.get("promoted_ids") or []),
            "removed": len(conversion.get("demoted_ids") or []),
            "ending": end_ready,
            "supplier_call_sheets": conversion.get("call_sheets_count", 0),
            "promoted_ids": conversion.get("promoted_ids"),
            "demoted_ids": conversion.get("demoted_ids"),
        },
        "auto_send": False,
        "auto_call": False,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "sam_api": sam_api_park_status(),
        "query_productivity_best_unique_per_call": (prod or {}).get("best_unique_per_call"),
    }

    # Verdict
    if authorize_live_sam and live_calls > sam_daily_call_budget():
        verdict = "SAM_BUDGETED_REFRESH_FAILED"
    elif (authorize_live_sam and live_calls > 0 and ingest["new_unique"] >= 0) or conversion["stats"].get(
        "promoted_ready_to_call", 0
    ) >= 0:
        if live_calls <= max_sam_calls and dash["calls_used"] <= sam_daily_call_budget():
            if (authorize_live_sam and live_calls >= 1) or conversion["stats"].get("promoted_ready_to_call", 0) > 0:
                verdict = "SAM_BUDGETED_REFRESH_AND_SUPPLIER_CONVERSION_WORKING"
            else:
                verdict = "SAM_BUDGETED_REFRESH_PARTIAL"
        else:
            verdict = "SAM_BUDGETED_REFRESH_FAILED"
    else:
        verdict = "SAM_BUDGETED_REFRESH_PARTIAL"

    # Stronger: WORKING if budget intact and conversion ran on full deep pool
    if (
        dash["calls_used"] <= sam_daily_call_budget()
        and conversion["stats"].get("attempted", 0) >= max(1, start_deep // 2)
        and (not authorize_live_sam or live_calls <= max_sam_calls)
    ):
        verdict = "SAM_BUDGETED_REFRESH_AND_SUPPLIER_CONVERSION_WORKING"

    summary["verdict"] = verdict
    summary["biggest_bottleneck"] = (
        "supplier_path_identity_weak"
        if conversion["stats"].get("supplier_path_unresolved", 0) > conversion["stats"].get("promoted_ready_to_call", 0)
        else "federal_access_registration"
    )
    summary["next_highest_value_action"] = (
        "Work CALL TODAY sheets for newly READY_TO_CALL; continue brand-channel maps for unresolved deep rows; "
        "spend remaining SAM credits tomorrow on next incremental date window"
    )

    save_json(OUT / "sam_supplier_conversion_summary.json", summary)
    write_docs(summary)
    return summary


def write_docs(summary: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "SAM_API_BUDGET_POLICY.md").write_text(
        f"""# SAM API Budget Policy

Hard daily limit: **{sam_daily_call_budget()}** live Opportunities API calls.

Config (single source, checked in order):

1. `SAM_DAILY_CALL_BUDGET`
2. `SAM_API_CALL_LIMIT`
3. `SAM_DAILY_API_BUDGET` (default 10)

## Rules

- Calendar day uses `SCHEDULER_TIMEZONE` (default America/Denver)
- Reserve **1** call by default (`SAM_DAILY_RESERVE_CALLS`)
- Cache hits cost **0**
- No automatic retries
- Tests must not call live SAM
- Canonical client: `discovery.sam_budgeted_client`

## Current

{(summary.get('sam_usage') or {}).get('display')}
""",
        encoding="utf-8",
    )
    (DOCS / "SAM_QUERY_PLANNER.md").write_text(
        """# SAM Query Planner

`build_daily_query_plan()` chooses up to 9 production calls before the reserve.

Prefer:

- max page size (1000)
- broad active solicitations (`ptype=o,k,i`)
- incremental date windows from `last_successful_sam_refresh`
- local product filtering (not one-call-per-NAICS)

Artifact: `artifacts/sam/sam_daily_query_plan.json`
""",
        encoding="utf-8",
    )
    (DOCS / "SAM_CACHE_AND_LEDGER.md").write_text(
        """# SAM Cache and Ledger

- Ledger: `artifacts/sam/sam_call_ledger.json` (persists across restarts)
- Cache: `artifacts/sam/response_cache/<fingerprint>.json`
- Lock: `artifacts/sam/.sam_budget.lock` (multi-process safety)
- Productivity: `artifacts/sam/sam_query_productivity.json`

Fingerprint = sha256(endpoint + params without api_key).
""",
        encoding="utf-8",
    )
    (DOCS / "SUPPLIER_PATH_CONVERSION.md").write_text(
        f"""# Supplier Path Conversion

Converts `DEEP_RESEARCH_COMPLETE` rows into `READY_TO_CALL` via manufacturer channels,
distributor maps, and L.22 call sheets.

## This run

```json
{json.dumps(summary.get('deep_conversion'), indent=2)}
```

```json
{json.dumps(summary.get('ready_to_call'), indent=2)}
```
""",
        encoding="utf-8",
    )
    # Update CURRENT architecture snippet
    arch = DOCS / "CURRENT_M3_ARCHITECTURE.md"
    if arch.exists():
        text = arch.read_text(encoding="utf-8")
        marker = "## SAM API (budgeted)"
        block = f"""## SAM API (budgeted)

- Canonical client: `discovery.sam_budgeted_client`
- Daily budget: {sam_daily_call_budget()} (`SAM_DAILY_CALL_BUDGET`)
- Ledger + cache under `artifacts/sam/`
- Federal rows → `WATCH_FEDERAL_ACCESS` until entity registration; research retained

Last conversion verdict: `{(summary.get('verdict'))}`
"""
        if marker in text:
            # replace from marker to next ## or end
            import re as _re

            text = _re.sub(r"## SAM API \(budgeted\).*?(?=\n## |\Z)", block + "\n", text, flags=_re.S)
        else:
            text = text.rstrip() + "\n\n" + block
        arch.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import sys

    live = "--live-sam" in sys.argv
    max_c = 9
    for a in sys.argv:
        if a.startswith("--max-calls="):
            max_c = int(a.split("=", 1)[1])
    summary = run_phase(authorize_live_sam=live, max_sam_calls=max_c)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "sam_usage",
                    "sam_discovery",
                    "efficiency",
                    "deep_conversion",
                    "ready_to_call",
                    "biggest_bottleneck",
                    "next_highest_value_action",
                )
            },
            indent=2,
            default=str,
        )
    )
