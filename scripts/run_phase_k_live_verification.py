"""Phase K — live verification of the four Phase J quote-ready deals only."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import now_utc
from deep_deal_economics import maximum_allowable_supplier_cost
from eligibility_gate import evaluate_eligibility_gate
from phase_j.history_reconciliation import reconcile_awards
from phase_j.product_identity import build_product_identity
from sam_live_fallback import (
    LIVE_SOURCE_UNAVAILABLE,
    STALE_CACHE_ONLY,
    retrieve_sam_live,
)

OUT = ROOT / "artifacts" / "phase_k"

# Research order per Phase K prompt
CANDIDATES = [
    {
        "rank_hint": 1,
        "label": "Pipefitter toolkit",
        "nsn": "5180-00-596-1509",
        "notice_id": "40c00954331b4d67953ad235dc2242b5",
        "sol_hint": "BA015",
        "phase_j_max": 114120.88,
        "phase_j_revenue": 124120.88,
    },
    {
        "rank_hint": 2,
        "label": "Turbine support",
        "nsn": "2840-00-411-8852",
        "notice_id": "4d3c6e21c5a7447f990287bc326e35aa",
        "sol_hint": "SPRTA1-26-Q-0119",
        "phase_j_max": 178890.0,
        "phase_j_revenue": 188890.0,
        "mpn_hint": "6870409",
    },
    {
        "rank_hint": 3,
        "label": "Diesel engine",
        "nsn": "2815-01-536-9262",
        "notice_id": "acfd277bec9244128730c2fddb160918",
        "sol_hint": "A090",
        "phase_j_max": 308014.4,
        "phase_j_revenue": 318014.4,
    },
    {
        "rank_hint": 4,
        "label": "Transmission kit",
        "nsn": "2520-01-682-2226",
        "notice_id": "339022b9462c483ab59130995cc97fa7",
        "sol_hint": "W912CH-26-B-A011",
        "phase_j_max": 1942080.0,
        "phase_j_revenue": 1952080.0,
    },
]

_QTY_PATTERNS = [
    re.compile(r"\b(?:quantity|qty|quan\.?)\s*[:#]?\s*([\d,]+(?:\.\d+)?)\b", re.I),
    re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(EA|EACH|KT|KIT|SE|SET|BX|PK)\b", re.I),
    re.compile(r"\bCLIN\s*\d+[^\n]{0,80}?(?:qty|quantity)\s*[:#]?\s*([\d,]+)", re.I | re.S),
]
_UOM_RE = re.compile(r"\b(EA|EACH|KT|KIT|SE|SET|BX|BOX|PK|PACK|PR|PAIR)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def fetch_sam_opportunity(notice_id: str) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    import httpx

    from api_budget import can_spend_sam, record_sam_usage
    from sam_client import SAM_SEARCH_URL

    key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "error": "SAM_GOV_API_KEY_missing"}
    if not can_spend_sam(1):
        return {"ok": False, "error": "sam_budget"}

    # Prefer noticeid filter; fall back to raw get by searching recent with sol
    params = {
        "api_key": key,
        "noticeid": notice_id,
        "limit": 1,
        "offset": 0,
    }
    with httpx.Client(timeout=45.0) as client:
        resp = client.get(SAM_SEARCH_URL, params=params)
        record_sam_usage(1)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP_{resp.status_code}", "body": resp.text[:300]}
        data = resp.json() or {}
        rows = data.get("opportunitiesData") or []
        if not rows:
            # Try without noticeid — some API versions want noticeId casing
            params2 = {**params, "postedFrom": "01/01/2025", "postedTo": now_utc().strftime("%m/%d/%Y"), "active": "yes"}
            del params2["noticeid"]
            # Can't find without noticeid easily; return empty
            return {"ok": True, "found": False, "raw": data, "LIVE_SAM_CALLS": 1}
        raw = rows[0]
        return {"ok": True, "found": True, "raw": raw, "LIVE_SAM_CALLS": 1}


def fetch_noticedesc(notice_id: str) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    import httpx

    key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    url = f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={notice_id}"
    params = {"api_key": key} if key else {}
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, params=params)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "body": None}
        data = resp.json() if resp.content else {}
        desc = (data.get("description") if isinstance(data, dict) else None) or ""
        return {"ok": bool(desc), "body": str(desc), "raw_keys": list(data.keys()) if isinstance(data, dict) else []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "body": None}


def parse_qty_uom(text: str) -> dict[str, Any]:
    qty = None
    uom = None
    provenance = None
    for rx in _QTY_PATTERNS:
        m = rx.search(text or "")
        if not m:
            continue
        try:
            raw = m.group(1)
            qty = float(str(raw).replace(",", ""))
            provenance = m.group(0)[:80]
            if m.lastindex and m.lastindex >= 2 and m.group(2):
                uom = m.group(2).upper()
                if uom == "EACH":
                    uom = "EA"
                if uom in {"KIT"}:
                    uom = "KT"
                if uom in {"SET"}:
                    uom = "SE"
                if uom in {"BOX"}:
                    uom = "BX"
                if uom in {"PACK"}:
                    uom = "PK"
            break
        except (TypeError, ValueError):
            continue
    if not uom:
        mu = _UOM_RE.search(text or "")
        if mu:
            uom = mu.group(1).upper()
            if uom == "EACH":
                uom = "EA"
            if uom == "KIT":
                uom = "KT"
    return {
        "quantity": qty,
        "uom": uom,
        "confirmed": qty is not None and uom is not None,
        "provenance": provenance,
        "status": "CONFIRMED" if (qty is not None and uom is not None) else "LIVE_QTY_UOM_UNCONFIRMED",
    }


def extract_compliance_flags(text: str) -> dict[str, Any]:
    t = text or ""
    flags = {
        "fat": bool(re.search(r"\bfirst\s+article|\bFAT\b", t, re.I)),
        "jcp": bool(re.search(r"\bJCP\b|DDForm\s*2345|DD2345", t, re.I)),
        "source_approval": bool(re.search(r"source\s+approval|approved\s+source|QPL|qualified\s+products", t, re.I)),
        "mil_std_packaging": bool(re.search(r"MIL[\-\s]?STD[\-\s]?2073|MIL[\-\s]?STD[\-\s]?129", t, re.I)),
        "export_control": bool(re.search(r"export\s+control|ITAR|EAR\b", t, re.I)),
        "inspection": bool(re.search(r"inspection|acceptance", t, re.I)),
    }
    score = sum(1 for v in flags.values() if v)
    if flags["source_approval"] or flags["fat"] or flags["jcp"]:
        complexity = "HIGH"
    elif score >= 2:
        complexity = "MODERATE"
    elif score >= 1:
        complexity = "MODERATE"
    else:
        complexity = "LOW"
    # Aviation turbine / engine always elevate
    if re.search(r"turbine|aircraft|aviation|engine,\s*diesel|transmission\s+kit", t, re.I):
        if complexity == "LOW":
            complexity = "MODERATE"
    return {"flags": flags, "execution_complexity": complexity}


def load_phase_j_history(nsn: str) -> list[dict[str, Any]]:
    path = ROOT / "artifacts" / "phase_j" / "reconciliation_latest.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    for q in data.get("quote_ready") or []:
        if q.get("nsn") == nsn:
            recon = q.get("reconciliation") or {}
            # Rebuild award-like dicts from reconciliations
            out = []
            for r in recon.get("reconciliations") or []:
                if not r.get("drives_economics"):
                    continue
                c = r.get("candidate_history") or {}
                up = r.get("unit_price") or {}
                out.append(
                    {
                        "award_id": c.get("award_id"),
                        "award_date": c.get("award_date"),
                        "days_ago": c.get("days_ago"),
                        "recipient_name": c.get("awardee"),
                        "description": c.get("description"),
                        "award_amount": c.get("amount") or up.get("raw_total"),
                    }
                )
            return out
    return []


def verify_one(cand: dict[str, Any]) -> dict[str, Any]:
    nid = cand["notice_id"]
    print(f"[phase_k] {cand['label']} {cand['nsn']} notice={nid}", flush=True)

    # Multi-source live retrieval: API preferred, public SAM detail/attachments on failure
    live = retrieve_sam_live(
        notice_id=nid,
        solicitation_number=cand.get("sol_hint"),
        fetch_attachments=True,
        max_attachments=2,
    )
    # Legacy personal-API probes only when fallback did not already confirm live facts
    if live.get("live_verified"):
        sam = {"ok": False, "found": False, "raw": None, "skipped": "live_fallback_confirmed"}
        desc = {"ok": False, "body": None, "skipped": "live_fallback_confirmed"}
    else:
        sam = fetch_sam_opportunity(nid)
        desc = fetch_noticedesc(nid)
    raw = (sam.get("raw") or {}) if sam.get("found") else {}
    title = str(live.get("title") or raw.get("title") or cand["label"])
    body = str(live.get("description_excerpt") or desc.get("body") or "")
    blob = f"{title}\n{body}\n{json.dumps(raw, default=str)[:5000]}"

    rdue = live.get("response_deadline") or raw.get("responseDeadLine") or raw.get("responseDeadline")
    open_status = live.get("open_status")
    confirmation = live.get("confirmation") or LIVE_SOURCE_UNAVAILABLE

    live_status = "UNKNOWN"
    if confirmation not in {LIVE_SOURCE_UNAVAILABLE, STALE_CACHE_ONLY} and open_status == "OPEN":
        live_status = "OPEN"
    elif confirmation not in {LIVE_SOURCE_UNAVAILABLE, STALE_CACHE_ONLY} and open_status in {
        "ARCHIVED",
        "CANCELLED",
    }:
        live_status = str(open_status)
    elif live.get("live_verified"):
        live_status = f"LIVE_CONFIRMED:{confirmation}"
    elif sam.get("found"):
        live_status = "OPEN_OR_LISTED"
    elif desc.get("ok"):
        live_status = "DESCRIPTION_RETRIEVABLE"
    elif live.get("api_failure"):
        live_status = f"API_{live.get('api_failure')}_FALLBACK_{confirmation}"
    elif sam.get("error"):
        live_status = f"FETCH_ERROR:{sam.get('error')}"

    qty = live.get("qty_uom") or parse_qty_uom(blob)
    if not qty.get("confirmed"):
        q2 = parse_qty_uom(blob)
        if q2.get("confirmed"):
            qty = q2

    identity = build_product_identity(
        {"title": title, "description": body, "nsn": cand["nsn"]},
        text=blob,
    )
    elig = evaluate_eligibility_gate(
        {
            "title": title,
            "description": body,
            "typeOfSetAside": live.get("set_aside") or raw.get("typeOfSetAside"),
            "typeOfSetAsideDescription": raw.get("typeOfSetAsideDescription"),
            "solicitation_number": live.get("solicitation_number")
            or raw.get("solicitationNumber")
            or cand.get("sol_hint"),
        },
        text=blob,
    )
    compliance = extract_compliance_flags(blob)
    awards = load_phase_j_history(cand["nsn"])
    recon = reconcile_awards(identity, awards)

    # Economics only if qty confirmed
    hist_unit = None
    for r in recon.get("reconciliations") or []:
        up = (r.get("unit_price") or {}).get("derived_unit_price")
        if up:
            hist_unit = float(up)
            break
    # Fallback: lot / hist qty
    if hist_unit is None and awards:
        for a in awards:
            m = re.search(r"QTY:\s*([\d,]+)|([\d,]+)\s*EA", str(a.get("description") or ""), re.I)
            amt = a.get("award_amount")
            if m and amt:
                try:
                    qh = float((m.group(1) or m.group(2)).replace(",", ""))
                    if qh > 0:
                        hist_unit = float(amt) / qh
                        break
                except (TypeError, ValueError):
                    pass

    economics: dict[str, Any] = {
        "recalculated": False,
        "blocker": None,
        "phase_j_max_supplier_cost": cand["phase_j_max"],
        "historical_unit_price": hist_unit,
    }
    if not qty.get("confirmed"):
        economics["blocker"] = "LIVE_QTY_UOM_UNCONFIRMED"
        economics["max_delivered_supplier_cost"] = None
        economics["max_unit_supplier_cost"] = None
    elif hist_unit is None:
        economics["blocker"] = "NO_USABLE_HISTORICAL_UNIT_PRICE"
        economics["projected_revenue"] = None
    else:
        live_qty = float(qty["quantity"])
        projected = hist_unit * live_qty
        max_cost = maximum_allowable_supplier_cost(expected_revenue=projected)
        max_del = max_cost.get("maximum_allowable_supplier_cost")
        economics.update(
            {
                "recalculated": True,
                "live_quantity": live_qty,
                "live_uom": qty.get("uom"),
                "historical_unit_price": hist_unit,
                "projected_revenue": round(projected, 2),
                "max_delivered_supplier_cost": max_del,
                "max_unit_supplier_cost": round(float(max_del) / live_qty, 2) if max_del and live_qty else None,
                "margins": {
                    "15pct_unit": round(hist_unit * 0.85, 2),
                    "20pct_unit": round(hist_unit * 0.80, 2),
                    "25pct_unit": round(hist_unit * 0.75, 2),
                },
                "note": "Projected revenue = historical unit × live qty; AGED history — not current market proof.",
            }
        )

    # Final state — API failure alone must not force VERIFY_LIVE_DATA when fallback live
    expired = False
    if open_status in {"ARCHIVED", "CANCELLED"}:
        expired = True
    elif rdue and cand["nsn"] == "2840-00-411-8852":
        # Turbine historically due mid-September; flag if deadline string is past
        if re.search(r"2026-09-1[0-4]|September\s+1[0-4],\s*2026", str(rdue), re.I):
            expired = True

    if confirmation in {LIVE_SOURCE_UNAVAILABLE, STALE_CACHE_ONLY} and not live.get("live_verified"):
        final = "VERIFY_LIVE_DATA"
    elif expired:
        final = "EXPIRED_DURING_VERIFICATION"
    elif elig.get("actionable_for_quote_or_bid") is not True:
        final = "NOT_EXECUTABLE" if elig.get("overall_status") == "NOT_CURRENTLY_ELIGIBLE" else "VERIFY_LIVE_DATA"
    elif not qty.get("confirmed"):
        final = "VERIFY_LIVE_DATA"
    elif identity.get("identity_level") not in {"EXACT", "STRONG"}:
        final = "VERIFY_IDENTITY"
    elif compliance["execution_complexity"] == "PROHIBITIVE":
        final = "NOT_EXECUTABLE"
    elif economics.get("blocker") == "NO_USABLE_HISTORICAL_UNIT_PRICE":
        final = "VERIFY_LIVE_DATA"
    elif compliance["flags"].get("source_approval") and cand["nsn"].startswith("2840"):
        final = "VERIFY_SOURCE_APPROVAL"
    elif economics.get("recalculated"):
        # Live evidence can clear the retrieval gate; business readiness still
        # requires eligibility actionable — preserved above. Do not auto-promote
        # quote-ready solely because fallback retrieved facts.
        final = "VERIFY_LIVE_DATA"
    else:
        final = "VERIFY_LIVE_DATA"

    # Hard rule: qty unconfirmed never quote-ready
    if not qty.get("confirmed") and final == "READY_FOR_QUOTE_OUTREACH":
        final = "VERIFY_LIVE_DATA"

    # Preserve Phase K business demotions when live confirms known blockers in text
    if re.search(r"HATZ|authorized\s+distributor", body, re.I) and cand["nsn"].startswith("2815"):
        final = "VERIFY_SOURCE_APPROVAL"
    if re.search(r"Distribution\s+(Statement\s+)?D|TDP.*\bD\b", body, re.I) and cand["nsn"].startswith("2520"):
        if final not in {"EXPIRED_DURING_VERIFICATION"}:
            final = "NOT_EXECUTABLE"
    if re.search(r"Initial\s+Product\s+Inspection|TDP.*distribution\s+code", body, re.I) and cand[
        "nsn"
    ].startswith("5180"):
        if final not in {"EXPIRED_DURING_VERIFICATION", "NOT_EXECUTABLE"}:
            final = "VERIFY_SUPPLIER_CHANNEL"

    return {
        "label": cand["label"],
        "nsn": cand["nsn"],
        "notice_id": nid,
        "listing_url": live.get("listing_url") or f"https://sam.gov/opp/{nid}/view",
        "live_status": live_status,
        "live_confirmation": confirmation,
        "live_retrieval": {
            "api_failure": live.get("api_failure"),
            "confirmation": confirmation,
            "live_verified": live.get("live_verified"),
            "attempts": live.get("attempts"),
            "conflicts": live.get("conflicts"),
            "attachment_names": live.get("attachment_names"),
            "discovery_coverage": live.get("discovery_coverage"),
            "field_provenance": {
                "deadline": (live.get("fields") or {}).get("response_deadline"),
                "quantity": (live.get("fields") or {}).get("quantity"),
                "uom": (live.get("fields") or {}).get("uom"),
                "open_status": (live.get("fields") or {}).get("open_status"),
            },
        },
        "sam_found": bool(sam.get("found")),
        "solicitation_number": live.get("solicitation_number")
        or raw.get("solicitationNumber")
        or cand.get("sol_hint"),
        "agency": raw.get("fullParentPathName") or raw.get("department"),
        "notice_type": raw.get("type") or raw.get("baseType"),
        "response_deadline": rdue,
        "set_aside": live.get("set_aside")
        or raw.get("typeOfSetAsideDescription")
        or raw.get("typeOfSetAside"),
        "title": title,
        "noticedesc_ok": bool(desc.get("ok")),
        "noticedesc_chars": len(body),
        "noticedesc_excerpt": body[:1200],
        "qty_uom": qty,
        "identity": identity,
        "eligibility": {
            "overall_status": elig.get("overall_status"),
            "actionable": elig.get("actionable_for_quote_or_bid"),
            "blocking_reason": elig.get("blocking_reason"),
            "plain": elig.get("plain"),
        },
        "compliance": compliance,
        "history": {
            "usable_count": recon.get("usable_count"),
            "history_class": recon.get("history_class"),
            "revenue_basis": recon.get("revenue_basis"),
            "awards": awards,
        },
        "economics": economics,
        "final_state": final,
        "sam_error": sam.get("error"),
        "generated_at": _utc(),
    }


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    results = [verify_one(c) for c in CANDIDATES]
    _write("live_verification.json", {"kind": "PhaseKLiveVerification", "results": results})
    print(json.dumps([{k: r.get(k) for k in ("label", "nsn", "live_status", "final_state", "qty_uom", "response_deadline", "solicitation_number")} for r in results], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
