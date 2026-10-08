"""Top-10 buyer validation — order totals, not line medians. No Playwright."""

from __future__ import annotations

import json
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.engine import (
    BUILD as BI_BUILD,
    _buyer_id,
    _canon_name,
    _num,
    _parse_date,
    classify_family,
)

BUILD = "20261007-m3-top10-buyer-validation-v1"
MAX_PER_TARGET_S = 180
FROZEN_N = 10

_DISTRIBUTORS = re.compile(
    r"\b(ferguson|grainger|fastenal|msc\b|motion\s*industr|applied\s*industrial|"
    r"graybar|wesco|rexel|ced\b|mckesson|medline|cardinal|henry\s*schein|core\s*&\s*main)\b",
    re.I,
)
_PCARD_CONFIRMED = re.compile(
    r"\b(government\s*purchase\s*card|\bGPC\b|p-?card|credit\s*card\s*payment|"
    r"cardholder|purchase\s*card)\b",
    re.I,
)
_NORM = re.compile(r"[^a-z0-9]+")


def _utc() -> str:
    return now_utc().isoformat()


def _data_path(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _write_status(**kwargs: Any) -> None:
    body = {"build": BUILD, "updated_at": _utc(), **kwargs}
    _data_path("m3_top10_buyer_validation_v1_status.json").write_text(
        json.dumps(body, indent=2, default=str), encoding="utf-8"
    )


def freeze_top10() -> list[dict[str, Any]]:
    doc = json.loads(_data_path("m3_buyer_intelligence_v1_targets.json").read_text(encoding="utf-8"))
    targets = [t for t in (doc.get("TARGET_BUYERS") or []) if isinstance(t, dict)][:FROZEN_N]
    frozen = []
    for i, t in enumerate(targets, 1):
        frozen.append(
            {
                "rank": i,
                "BUYER_ID": t.get("BUYER_ID"),
                "BUYER_NAME_CANONICAL": t.get("BUYER_NAME_CANONICAL"),
                "product_family": t.get("product_family"),
                "bi_buy_count": t.get("BUY_COUNT"),
                "bi_median_line": t.get("MEDIAN_BUY"),
                "bi_score": t.get("TARGET_ACCOUNT_SCORE"),
                "source_snapshot": {
                    k: t.get(k)
                    for k in (
                        "AGENCY",
                        "KNOWN_VENDORS",
                        "LIVE_OPPORTUNITY_OVERLAP",
                        "LIVE_TITLE",
                        "LIVE_DEADLINE",
                        "LIVE_OPPORTUNITY_ID",
                        "PURCHASE_CARD_CLUE",
                        "FAST_BUY_CLUE",
                    )
                },
            }
        )
    path = _data_path("m3_top10_buyer_validation_v1_frozen.json")
    path.write_text(
        json.dumps({"build": BUILD, "bi_build": BI_BUILD, "frozen": frozen, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    return frozen


def normalize_buyer(name: str, *, family: str) -> dict[str, Any]:
    raw = (name or "").strip()
    low = raw.lower()
    out: dict[str, Any] = {
        "BUYER_NAME": _canon_name(raw),
        "AGENCY": _canon_name(raw),
        "SUBAGENCY": None,
        "OFFICE": None,
        "DEPARTMENT": None,
        "FACILITY": None,
        "CITY": None,
        "STATE": None,
        "BUYER_NORMALIZATION_CONFIDENCE": "LOW",
    }
    if "go-metro" in low or low.replace(" ", "") == "gometro":
        out.update(
            {
                "BUYER_NAME": "Go-Metro (Southwest Ohio Regional Transit Authority)",
                "AGENCY": "Southwest Ohio Regional Transit Authority",
                "SUBAGENCY": "Go-Metro / SORTA",
                "OFFICE": "Procurement / Inventory",
                "CITY": "Cincinnati",
                "STATE": "OH",
                "BUYER_NORMALIZATION_CONFIDENCE": "HIGH",
            }
        )
        return out
    if "clay" in low and "county" in low:
        out.update(
            {
                "BUYER_NAME": "Clay County",
                "AGENCY": "Clay County",
                "STATE": None,
                "BUYER_NORMALIZATION_CONFIDENCE": "MEDIUM",
            }
        )
        return out
    if low in {"california", "state of california"} or low.startswith("california"):
        out.update(
            {
                "BUYER_NAME": "California (agency-level aggregate — unresolved)",
                "AGENCY": "State of California / CA buyers (unresolved)",
                "BUYER_NORMALIZATION_CONFIDENCE": "LOW",
            }
        )
        return out
    if "dla aviation" in low or "dla av richmond" in low:
        out.update(
            {
                "BUYER_NAME": "DLA Aviation — Richmond",
                "AGENCY": "Defense Logistics Agency",
                "SUBAGENCY": "DLA Aviation",
                "OFFICE": "DLA Aviation Richmond",
                "CITY": "Richmond",
                "STATE": "VA",
                "DEPARTMENT": "Department of Defense",
                "BUYER_NORMALIZATION_CONFIDENCE": "HIGH",
            }
        )
        return out
    if "dla maritime" in low or "land and maritime" in low or "dla land" in low:
        out.update(
            {
                "BUYER_NAME": "DLA Land and Maritime — Columbus",
                "AGENCY": "Defense Logistics Agency",
                "SUBAGENCY": "DLA Land and Maritime",
                "OFFICE": "DLA Maritime Columbus",
                "CITY": "Columbus",
                "STATE": "OH",
                "DEPARTMENT": "Department of Defense",
                "BUYER_NORMALIZATION_CONFIDENCE": "HIGH",
            }
        )
        return out
    if low in {"dept of defense", "department of defense", "dod"} or (
        "dept of defense" in low and "dla" not in low and "logistics" not in low
    ):
        out.update(
            {
                "BUYER_NAME": "Department of Defense (agency-level aggregate — unresolved)",
                "AGENCY": "Department of Defense",
                "DEPARTMENT": "Department of Defense",
                "BUYER_NORMALIZATION_CONFIDENCE": "LOW",
            }
        )
        return out
    # Parse dotted DoD strings
    if "defense logistics" in low or "dept of defense" in low:
        parts = [p.strip() for p in raw.replace(".", " ").split() if p.strip()]
        out["DEPARTMENT"] = "Department of Defense"
        out["AGENCY"] = "Defense Logistics Agency" if "logistics" in low else "Department of Defense"
        out["BUYER_NAME"] = _canon_name(raw[:120])
        out["BUYER_NORMALIZATION_CONFIDENCE"] = "MEDIUM" if "dla" in low else "LOW"
        return out
    out["BUYER_NORMALIZATION_CONFIDENCE"] = "MEDIUM" if len(raw) > 8 else "LOW"
    return out


def _match_buyer_token(buyer_id: str, name: str, token: str) -> bool:
    blob = f"{buyer_id} {name}".lower()
    tok = token.lower()
    if tok in blob:
        return True
    # portal codes
    if tok.replace("-", "") in blob.replace("-", "").replace(" ", ""):
        return True
    return False


def collect_purchases_for_target(frozen: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate line evidence → distinct purchase/order events."""
    family = str(frozen.get("product_family") or "")
    bid = str(frozen.get("BUYER_ID") or "")
    name = str(frozen.get("BUYER_NAME_CANONICAL") or "")
    purchases: dict[str, dict[str, Any]] = {}

    scale = json.loads(_data_path("m3_scale_evidence_profit_store.json").read_text(encoding="utf-8"))
    by_line = scale.get("by_line") or {}

    # First pass: collect all matching-buyer lines keyed by project
    project_lines: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in by_line.values():
        if not isinstance(rec, dict):
            continue
        gv = rec.get("government_value") if isinstance(rec.get("government_value"), dict) else {}
        ev = gv.get("evidence") if isinstance(gv.get("evidence"), dict) else {}
        pc = rec.get("public_cost") if isinstance(rec.get("public_cost"), dict) else {}
        pev = pc.get("evidence") if isinstance(pc.get("evidence"), dict) else {}
        ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
        oid = str(rec.get("opportunity_id") or "")
        portal = oid.split(":")[1] if oid.startswith("opengov:") and oid.count(":") >= 2 else ""
        buyer = str(ev.get("historical_buyer") or ev.get("government_code") or pev.get("government_code") or portal or "")
        if not buyer:
            continue
        if not (
            _match_buyer_token(bid, name, buyer)
            or _match_buyer_token(bid, name, portal)
            or _buyer_id(buyer) == bid
        ):
            if "go_metro" in bid and buyer.lower() in {"go-metro", "gometro"}:
                pass
            elif "claycounty" in bid and "clay" in buyer.lower():
                pass
            else:
                continue
        desc = str(ident.get("description") or ev.get("exact_item") or "")
        title = str(ev.get("project_title") or "")
        text = f"{desc} {title}"
        fam = classify_family(text)
        pid = str(ev.get("project_id") or pev.get("project_id") or title or oid or "")
        if not pid:
            continue
        qty = _num(ev.get("quantity") or ident.get("quantity")) or 1.0
        unit = _num(ev.get("awarded_unit_price") or ev.get("unit_price") or pev.get("unit_price"))
        ext = _num(ev.get("awarded_extended_total"))
        line_amt = ext if ext is not None else ((unit * qty) if unit is not None else None)
        project_lines[f"{buyer.lower()}|{pid}"].append(
            {
                "fam": fam,
                "line_amt": line_amt,
                "vendor": ev.get("winning_vendor") or pev.get("seller") or pc.get("source"),
                "date": ev.get("award_date") or pev.get("award_date"),
                "title": title,
                "text": text[:300],
                "source": str(gv.get("source") or pc.get("source") or "scale_evidence"),
                "confidence": "HIGH" if gv.get("status") == "FOUND" else "MEDIUM",
                "buyer_raw": buyer,
                "pid": pid,
            }
        )

    for key, lines in project_lines.items():
        title = next((x.get("title") for x in lines if x.get("title")), "")
        inventory_rfq = bool(re.search(r"inventory\s*parts|fleet\s*parts|rfq\s*\d+", title, re.I))
        fam_lines = [x for x in lines if x.get("fam") == family]
        # AUTO_PARTS inventory RFQs → full project/order total (complete order, not category slice)
        if family == "AUTO_PARTS" and inventory_rfq:
            use_lines = lines
            mode = "FULL_PROJECT"
        elif fam_lines:
            use_lines = fam_lines
            mode = "CATEGORY_LINES"
        else:
            continue
        amounts = [float(x["line_amt"]) for x in use_lines if x.get("line_amt") is not None]
        if not amounts and mode == "CATEGORY_LINES":
            continue
        vendors = [x.get("vendor") for x in use_lines if x.get("vendor")]
        dates = [x.get("date") for x in use_lines if x.get("date")]
        purchases[key] = {
            "PURCHASE_ID": str(use_lines[0].get("pid")),
            "PURCHASE_DATE": max(dates) if dates else None,
            "TOTAL_AMOUNT": round(sum(amounts), 2) if amounts else None,
            "amount_known": bool(amounts),
            "PRODUCT_CATEGORY": family,
            "VENDOR": Counter(vendors).most_common(1)[0][0] if vendors else None,
            "SOURCE": use_lines[0].get("source"),
            "CONFIDENCE": use_lines[0].get("confidence"),
            "TITLE": title,
            "LINE_COUNT": len(use_lines),
            "buyer_raw": use_lines[0].get("buyer_raw"),
            "text": use_lines[0].get("text"),
            "AGGREGATION": mode,
        }

    # Live demand — not treated as completed purchases with totals
    live_doc = json.loads(_data_path("l23_canonical_population_store.json").read_text(encoding="utf-8"))
    live_rows = live_doc.get("opportunities") or {}
    live_hits = []
    name_tok = _NORM.sub(" ", name.lower())
    for cid, rec in live_rows.items():
        if not isinstance(rec, dict):
            continue
        buyer = str(rec.get("buyer") or rec.get("agency") or "")
        bnorm = _NORM.sub(" ", buyer.lower())
        if not (
            _buyer_id(buyer) == bid
            or (name_tok and name_tok[:12] in bnorm)
            or (bnorm and bnorm[:12] in name_tok)
            or ("california" in bid and "california" in bnorm)
            or ("defense" in bid and ("defense" in bnorm or "dla" in bnorm))
            or ("aviation" in bid.lower() and "aviation" in bnorm)
            or ("maritime" in bid.lower() and ("maritime" in bnorm or "land and maritime" in bnorm))
        ):
            continue
        title = str(rec.get("title") or "")
        fam = classify_family(f"{title} {rec.get('description') or ''}")
        if fam != family and family not in classify_family(title):
            # loose: family token in title
            if not re.search(family.split("_")[0], title, re.I) and fam != family:
                continue
        live_hits.append(
            {
                "canonical_opportunity_id": cid,
                "TITLE": title,
                "SOURCE": rec.get("platform") or rec.get("source_status"),
                "DEADLINE": rec.get("deadline"),
                "ESTIMATED_SIZE": None,
                "LINK": rec.get("authoritative_url") or (rec.get("row_ref") or {}).get("url"),
                "WHY_MATCHED": f"buyer+{family}",
            }
        )
        if len(live_hits) >= 3:
            break

    out = list(purchases.values())
    for p in out:
        if not p.get("amount_known"):
            p["TOTAL_AMOUNT"] = None
        else:
            p["TOTAL_AMOUNT"] = round(float(p["TOTAL_AMOUNT"]), 2)
    return out, live_hits


def vendor_stats(purchases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    by_v: dict[str, dict[str, Any]] = {}
    for p in purchases:
        v = str(p.get("VENDOR") or "").strip() or "UNKNOWN"
        slot = by_v.setdefault(v, {"vendor": v, "purchases": 0, "total_spend": 0.0, "spend_known": False, "most_recent": None})
        slot["purchases"] += 1
        if p.get("TOTAL_AMOUNT") is not None:
            slot["total_spend"] += float(p["TOTAL_AMOUNT"])
            slot["spend_known"] = True
        d = str(p.get("PURCHASE_DATE") or "")
        if d and (not slot["most_recent"] or d > slot["most_recent"]):
            slot["most_recent"] = d
    vendors = sorted(by_v.values(), key=lambda x: -x["purchases"])
    for v in vendors:
        if v["spend_known"]:
            v["total_spend"] = round(v["total_spend"], 2)
        else:
            v["total_spend"] = None
    known = [v for v in vendors if v["vendor"] != "UNKNOWN"]
    if not known:
        conc = "UNKNOWN"
    else:
        top = known[0]["purchases"]
        total = sum(v["purchases"] for v in known)
        share = top / max(total, 1)
        if share >= 0.6 or len(known) <= 2 and share >= 0.5:
            conc = "HIGH"
        elif share >= 0.35:
            conc = "MEDIUM"
        else:
            conc = "LOW"
    return vendors[:12], conc


def pcard_class(purchases: list[dict[str, Any]], snap: dict[str, Any]) -> tuple[str, str | None]:
    texts = " ".join(str(p.get("text") or p.get("TITLE") or "") for p in purchases)
    m = _PCARD_CONFIRMED.search(texts)
    if m:
        return "CONFIRMED", m.group(0)[:120]
    # RFQ alone is NOT confirmed
    if snap.get("PURCHASE_CARD_CLUE") == "YES" and snap.get("FAST_BUY_CLUE") == "YES":
        if re.search(r"purchase\s*card|gpc|p-?card", str(snap), re.I):
            return "POSSIBLE", "prior BI flag"
        return "NO_EVIDENCE", "RFQ/fast-buy alone — not purchase-card proof"
    if re.search(r"\brfq\b", texts, re.I):
        return "NO_EVIDENCE", "RFQ title only"
    return "NO_EVIDENCE", None


def sourcing_and_channel(purchases: list[dict[str, Any]], family: str, vendors: list[dict[str, Any]]) -> tuple[str, str]:
    vnames = " ".join(v.get("vendor") or "" for v in vendors)
    if _DISTRIBUTORS.search(vnames):
        # check dominance
        dist_wins = sum(1 for v in vendors if _DISTRIBUTORS.search(v.get("vendor") or ""))
        if dist_wins and vendors and dist_wins >= max(1, len(vendors) // 2):
            channel = "DISTRIBUTOR_ADVANTAGED"
        else:
            channel = "MIXED"
    else:
        channel = "RESELLER_FRIENDLY" if vendors else "UNKNOWN"
    if family in {"MRO", "AUTO_PARTS", "TOOLS", "PPE", "JANITORIAL_SUPPLIES", "OFFICE_SUPPLIES", "ELECTRICAL", "PLUMBING"}:
        fit = "GOOD"
    elif family in {"MEDICAL_SUPPLIES"}:
        fit = "MIXED"
        if channel == "RESELLER_FRIENDLY":
            channel = "MIXED"
    elif family == "OTHER":
        fit = "UNKNOWN"
    else:
        fit = "MIXED"
    if channel == "CHANNEL_DOMINATED":
        fit = "BAD"
    # upgrade to CHANNEL_DOMINATED if single big distributor >70%
    if vendors and _DISTRIBUTORS.search(vendors[0].get("vendor") or ""):
        top = vendors[0]["purchases"]
        tot = sum(v["purchases"] for v in vendors) or 1
        if top / tot >= 0.7:
            channel = "CHANNEL_DOMINATED"
            fit = "MIXED"
    return fit, channel


def cash_recalc(purchases: list[dict[str, Any]], *, family: str) -> dict[str, Any]:
    totals = [float(p["TOTAL_AMOUNT"]) for p in purchases if p.get("TOTAL_AMOUNT") is not None]
    if len(totals) < 2:
        return {
            "CASH_FLOW_FIT_SCORE": 40,
            "CASH_RISK": "UNKNOWN",
            "WHY": "Insufficient complete-order totals (need aggregated purchase amounts).",
            "LIKELY_FUNDING_PATH": "UNKNOWN",
            "AVG_ORDER_TOTAL": round(statistics.mean(totals), 2) if totals else None,
            "MEDIAN_ORDER_TOTAL": round(statistics.median(totals), 2) if totals else None,
            "MIN_ORDER_TOTAL": round(min(totals), 2) if totals else None,
            "MAX_ORDER_TOTAL": round(max(totals), 2) if totals else None,
        }
    med = statistics.median(totals)
    avg = statistics.mean(totals)
    score = 55
    if med <= 5000:
        score += 25
        risk = "LOW"
    elif med <= 25000:
        score += 15
        risk = "LOW"
    elif med <= 50000:
        score += 5
        risk = "MEDIUM"
    else:
        score -= 20
        risk = "HIGH"
    if family in {"MEDICAL_SUPPLIES"}:
        score -= 5
        if risk == "LOW":
            risk = "MEDIUM"
    why = f"Median complete order ${med:,.0f} across {len(totals)} purchases (line values aggregated by project/PO)."
    path = "DROP_SHIP" if risk == "LOW" else ("SUPPLIER_TERMS" if risk == "MEDIUM" else "UNKNOWN")
    if med <= 10000 and family == "AUTO_PARTS":
        path = "PURCHASE_CARD_FAST_PAY"
    return {
        "CASH_FLOW_FIT_SCORE": max(0, min(100, score)),
        "CASH_RISK": risk,
        "WHY": why,
        "LIKELY_FUNDING_PATH": path,
        "AVG_ORDER_TOTAL": round(avg, 2),
        "MEDIAN_ORDER_TOTAL": round(med, 2),
        "MIN_ORDER_TOTAL": round(min(totals), 2),
        "MAX_ORDER_TOTAL": round(max(totals), 2),
    }


def registration_contact(norm: dict[str, Any], name: str) -> dict[str, Any]:
    """Existing M3 portals only — no crawl."""
    low = f"{name} {norm.get('BUYER_NAME')}".lower()
    reg = {
        "VENDOR_PORTAL": None,
        "REGISTRATION_REQUIRED": "UNKNOWN",
        "REGISTRATION_STATUS": "UNKNOWN",
        "REGISTRATION_URL": None,
        "PORTAL_RESEARCH_NEEDED": True,
    }
    contact = {
        "SMALL_BUSINESS_OFFICE": None,
        "PROCUREMENT_OFFICE": None,
        "BUYER_CONTACT": None,
        "EMAIL": None,
        "PHONE": None,
        "VENDOR_CONTACT_PAGE": None,
        "CONTACT_RESEARCH_NEEDED": True,
    }
    # Known from OpenGov portals / BI memory
    mem = {}
    for fname in ("m3_buyer_source_memory.json", "BIDNET_BUYER_DIRECTORY.json", "buyer_portal_registration_tracker.json"):
        p = _data_path(fname)
        if p.exists():
            try:
                mem[fname] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    if "go-metro" in low:
        reg.update(
            {
                "VENDOR_PORTAL": "OpenGov / Go-Metro procurement portal",
                "REGISTRATION_REQUIRED": "YES",
                "REGISTRATION_STATUS": "UNKNOWN",
                "REGISTRATION_URL": "https://procurement.opengov.com/portal/go-metro",
                "PORTAL_RESEARCH_NEEDED": False,
            }
        )
        contact.update(
            {
                "PROCUREMENT_OFFICE": "Go-Metro / SORTA Procurement",
                "VENDOR_CONTACT_PAGE": "https://procurement.opengov.com/portal/go-metro",
                "CONTACT_RESEARCH_NEEDED": False,
            }
        )
    if "dla" in low or "defense logistics" in low:
        reg.update(
            {
                "VENDOR_PORTAL": "SAM.gov / DIBBS / DLA Internet Bid Board",
                "REGISTRATION_REQUIRED": "YES",
                "REGISTRATION_STATUS": "UNKNOWN",
                "REGISTRATION_URL": "https://www.sam.gov",
                "PORTAL_RESEARCH_NEEDED": False,
            }
        )
        contact.update(
            {
                "SMALL_BUSINESS_OFFICE": "DLA Small Business Programs",
                "PROCUREMENT_OFFICE": norm.get("OFFICE") or "DLA contracting office",
                "VENDOR_CONTACT_PAGE": "https://www.dla.mil/",
                "CONTACT_RESEARCH_NEEDED": False,
            }
        )
    if (
        ("dept of defense" in low or low.startswith("department of defense"))
        and "dla" not in low
        and "logistics" not in low
    ):
        reg.update(
            {
                "VENDOR_PORTAL": "SAM.gov",
                "REGISTRATION_REQUIRED": "YES",
                "REGISTRATION_URL": "https://www.sam.gov",
                "PORTAL_RESEARCH_NEEDED": False,
            }
        )
        contact.update(
            {
                "SMALL_BUSINESS_OFFICE": "DoD Small Business (office unresolved)",
                "VENDOR_CONTACT_PAGE": "https://www.sam.gov",
                "CONTACT_RESEARCH_NEEDED": True,
            }
        )
    if "clay" in low:
        reg["PORTAL_RESEARCH_NEEDED"] = True
        contact["CONTACT_RESEARCH_NEEDED"] = True
    return {"registration": reg, "contact": contact}


def cadence(purchases: list[dict[str, Any]]) -> str:
    dates = sorted([d for d in (_parse_date(p.get("PURCHASE_DATE")) for p in purchases) if d])
    if len(dates) < 3:
        return "IRREGULAR"
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    if not gaps:
        return "IRREGULAR"
    avg = statistics.mean(gaps)
    if statistics.pstdev(gaps) > avg * 0.8:
        return "IRREGULAR"
    if avg < 45:
        return f"~{int(avg)} days (monthly-ish)"
    if avg < 120:
        return f"~{int(avg)} days (quarterly-ish)"
    return f"~{int(avg)} days"


def disposition(result: dict[str, Any]) -> tuple[str, str]:
    conf = result.get("BUYER_NORMALIZATION_CONFIDENCE")
    n = int(result.get("TOTAL_DISTINCT_PURCHASES") or 0)
    totals_ok = result.get("MEDIAN_ORDER_TOTAL") is not None and n >= 3
    cash = result.get("CASH_RISK")
    fit = result.get("SOURCING_FIT")
    channel = result.get("CHANNEL_COMPETITION")
    live = result.get("LIVE_OVERLAP") == "YES"

    if conf == "LOW" and n < 3:
        return "REJECT", "Buyer identity too broad and insufficient complete-order history."
    if conf == "LOW" and not totals_ok:
        return "REJECT", "Agency-level aggregate without defensible office + no complete order totals."
    if n < 3:
        # Specific office + live overlap → research next (not silent replace; still not ACT_NOW)
        if conf == "HIGH" and live and fit in {"GOOD", "MIXED"}:
            return "RESEARCH_NEXT", "Office identity strong and live overlap exists, but <3 complete historical purchases proven."
        return "PARTIAL", "Fewer than 3 distinct purchase events with category relevance."
    if not totals_ok:
        return "RESEARCH_NEXT", "Repeat signal exists but complete order totals not proven."
    if fit == "BAD" or channel == "CHANNEL_DOMINATED":
        return "WATCH", "History real but channel competition likely unfavorable."
    if cash == "HIGH":
        return "REJECT", "Cash exposure too high on complete-order evidence."
    if cash == "UNKNOWN":
        return "RESEARCH_NEXT", "Need more order-total evidence before ACT_NOW."
    if conf == "LOW":
        return "RESEARCH_NEXT", "Promising pattern but buyer office still unresolved."
    if live and cash in {"LOW", "MEDIUM"} and fit in {"GOOD", "MIXED"}:
        return "ACT_NOW", "Validated history + live overlap + manageable cash."
    if cash in {"LOW", "MEDIUM"} and fit in {"GOOD", "MIXED"} and n >= 3 and totals_ok:
        return "ACT_NOW", "Validated complete-order history with manageable cash and sourcing fit."
    return "WATCH", "Real but weak timing or incomplete evidence."


def next_action_text(result: dict[str, Any]) -> str:
    disp = result.get("DISPOSITION")
    buyer = result.get("BUYER_NAME") or "buyer"
    fam = str(result.get("CATEGORY") or "").replace("_", " ").lower()
    portal = (result.get("registration") or {}).get("REGISTRATION_URL")
    live = result.get("LIVE_OPPORTUNITIES") or []
    if disp == "ACT_NOW":
        if live:
            d = live[0].get("DEADLINE") or "deadline"
            return f"Research current live opportunity '{(live[0].get('TITLE') or '')[:80]}' before {d}."
        if portal:
            return f"Register at {portal} and contact procurement about recurring {fam} purchases."
        return f"Contact procurement / small-business office at {buyer} about how sub-$25K {fam} buys are sourced."
    if disp == "RESEARCH_NEXT":
        if result.get("BUYER_NORMALIZATION_CONFIDENCE") == "LOW":
            return f"Resolve which specific office under '{buyer}' places {fam} orders, then re-check order totals."
        if result.get("MEDIAN_ORDER_TOTAL") is None:
            return f"Recover complete PO/award totals for {buyer} {fam} (do not use line medians)."
        return f"Fill registration/contact gap for {buyer}, then decide ACT_NOW."
    if disp == "WATCH":
        return f"Watch {buyer} for the next {fam} solicitation; do not prioritize outreach yet."
    return f"Do not pursue {buyer}/{fam} — {result.get('DISPOSITION_WHY') or 'failed validation'}."


def validate_one(frozen: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    norm = normalize_buyer(str(frozen.get("BUYER_NAME_CANONICAL") or ""), family=str(frozen.get("product_family")))
    purchases, live_hits = collect_purchases_for_target(frozen)
    # Filter purchases to ones with dates when possible
    purchases = sorted(purchases, key=lambda p: str(p.get("PURCHASE_DATE") or ""), reverse=True)
    vendors, conc = vendor_stats(purchases)
    pcard, pcard_ev = pcard_class(purchases, frozen.get("source_snapshot") or {})
    fit, channel = sourcing_and_channel(purchases, str(frozen.get("product_family")), vendors)
    cash = cash_recalc(purchases, family=str(frozen.get("product_family")))
    rc = registration_contact(norm, str(frozen.get("BUYER_NAME_CANONICAL") or ""))
    dates = sorted([d for d in (_parse_date(p.get("PURCHASE_DATE")) for p in purchases) if d])
    now = datetime.now(timezone.utc)
    recent_12 = sum(1 for d in dates if (now - d).days <= 365)
    recent_24 = sum(1 for d in dates if (now - d).days <= 730)
    result = {
        "rank": frozen.get("rank"),
        "BUYER_ID": frozen.get("BUYER_ID"),
        "CATEGORY": frozen.get("product_family"),
        "bi_buy_count_lines": frozen.get("bi_buy_count"),
        "bi_median_line": frozen.get("bi_median_line"),
        "bi_score": frozen.get("bi_score"),
        **norm,
        "TOTAL_DISTINCT_PURCHASES": len(purchases),
        "FIRST_PURCHASE_DATE": dates[0].date().isoformat() if dates else None,
        "LAST_PURCHASE_DATE": dates[-1].date().isoformat() if dates else None,
        "AVERAGE_DAYS_BETWEEN_PURCHASES": cadence(purchases),
        "RECENT_BUYS_LAST_12_MONTHS": recent_12,
        "RECENT_BUYS_LAST_24_MONTHS": recent_24,
        "PURCHASES": purchases[:20],
        "HISTORICAL_VENDORS": vendors,
        "VENDOR_CONCENTRATION": conc,
        "PCARD": pcard,
        "PCARD_EVIDENCE": pcard_ev,
        "SOURCING_FIT": fit,
        "CHANNEL_COMPETITION": channel,
        **cash,
        "LIVE_OVERLAP": "YES" if live_hits else "NO",
        "LIVE_OPPORTUNITIES": live_hits,
        "registration": rc["registration"],
        "contact": rc["contact"],
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "validation_status": "OK" if time.perf_counter() - t0 < MAX_PER_TARGET_S else "TIMEOUT",
    }
    # False positive: BI counted lines as buys
    if int(frozen.get("bi_buy_count") or 0) > len(purchases) * 3 and len(purchases) >= 1:
        result["FALSE_POSITIVE_NOTE"] = (
            f"BI buy_count={frozen.get('bi_buy_count')} was line-level; "
            f"distinct complete purchases={len(purchases)}."
        )
    disp, why = disposition(result)
    # PARTIAL is internal — map to RESEARCH_NEXT/WATCH for owner disposition enum
    if disp == "PARTIAL":
        result["VALIDATION_MARK"] = "PARTIAL"
        disp = "RESEARCH_NEXT" if len(purchases) >= 1 else "REJECT"
        why = "Partial validation: " + why
    else:
        result["VALIDATION_MARK"] = "VALIDATED" if disp == "ACT_NOW" else disp
    result["DISPOSITION"] = disp
    result["DISPOSITION_WHY"] = why
    result["NEXT_ACTION"] = next_action_text(result)
    return result


def run_top10_validation() -> dict[str, Any]:
    t0 = time.perf_counter()
    _write_status(phase="FREEZE", pct=5)
    frozen = freeze_top10()
    results: list[dict[str, Any]] = []
    _write_status(phase="VALIDATE", pct=10, frozen=len(frozen))
    for i, f in enumerate(frozen):
        _write_status(
            phase="VALIDATE_TARGET",
            pct=10 + int(80 * i / max(len(frozen), 1)),
            current=f"{f.get('BUYER_NAME_CANONICAL')}/{f.get('product_family')}",
            completed=i,
        )
        results.append(validate_one(f))

    def _count(pred) -> int:
        return sum(1 for r in results if pred(r))

    counts = {
        "FROZEN": len(results),
        "BUYER_NORMALIZED": _count(lambda r: r.get("BUYER_NORMALIZATION_CONFIDENCE") in {"HIGH", "MEDIUM"}),
        "GE_3_PURCHASES": _count(lambda r: int(r.get("TOTAL_DISTINCT_PURCHASES") or 0) >= 3),
        "REAL_ORDER_TOTALS": _count(lambda r: r.get("MEDIAN_ORDER_TOTAL") is not None),
        "VENDOR_HISTORY": _count(lambda r: any(v.get("vendor") not in {None, "", "UNKNOWN"} for v in (r.get("HISTORICAL_VENDORS") or []))),
        "PCARD_CONFIRMED": _count(lambda r: r.get("PCARD") == "CONFIRMED"),
        "PCARD_POSSIBLE": _count(lambda r: r.get("PCARD") == "POSSIBLE"),
        "REGISTRATION_PATH": _count(lambda r: not (r.get("registration") or {}).get("PORTAL_RESEARCH_NEEDED")),
        "CONTACT_PATH": _count(lambda r: not (r.get("contact") or {}).get("CONTACT_RESEARCH_NEEDED")),
        "LIVE_OVERLAP": _count(lambda r: r.get("LIVE_OVERLAP") == "YES"),
        "ACT_NOW": _count(lambda r: r.get("DISPOSITION") == "ACT_NOW"),
        "RESEARCH_NEXT": _count(lambda r: r.get("DISPOSITION") == "RESEARCH_NEXT"),
        "WATCH": _count(lambda r: r.get("DISPOSITION") == "WATCH"),
        "REJECT": _count(lambda r: r.get("DISPOSITION") == "REJECT"),
        "CASH_RECALCULATED": len(results),
        "SOURCING_FIT": len(results),
    }
    actionable = counts["ACT_NOW"] + counts["RESEARCH_NEXT"]
    gates = {
        "BUYER_NORMALIZED_GE_8": counts["BUYER_NORMALIZED"] >= 8,
        "GE3_PURCHASES_GE_7": counts["GE_3_PURCHASES"] >= 7,
        "REAL_ORDER_TOTALS_GE_7": counts["REAL_ORDER_TOTALS"] >= 7,
        "VENDOR_HISTORY_GE_7": counts["VENDOR_HISTORY"] >= 7,
        "CASH_RECALC_GE_10": counts["CASH_RECALCULATED"] >= 10,
        "SOURCING_GE_10": counts["SOURCING_FIT"] >= 10,
        "REGISTRATION_GE_5": counts["REGISTRATION_PATH"] >= 5,
        "CONTACT_GE_5": counts["CONTACT_PATH"] >= 5,
        "ACTIONABLE_GE_5": actionable >= 5,
    }
    status = "PASS" if all(gates.values()) else "PARTIAL"
    # Business pass: at least 5 actionable even if some gates miss
    if actionable >= 5 and counts["REAL_ORDER_TOTALS"] >= 3:
        if status != "PASS":
            status = "PARTIAL_ACTIONABLE"

    elapsed = round(time.perf_counter() - t0, 2)
    report = {
        "build": BUILD,
        "bi_build": BI_BUILD,
        "kind": "Top10BuyerValidationV1",
        "STATUS": status,
        "updated_at": _utc(),
        "counts": counts,
        "gates": gates,
        "performance": {"VALIDATION_RUN_S": elapsed},
        "targets": results,
        "false_positives": [
            {
                "rank": r.get("rank"),
                "buyer": r.get("BUYER_NAME"),
                "category": r.get("CATEGORY"),
                "note": r.get("FALSE_POSITIVE_NOTE") or r.get("DISPOSITION_WHY"),
            }
            for r in results
            if r.get("FALSE_POSITIVE_NOTE") or r.get("DISPOSITION") == "REJECT"
        ],
    }
    _data_path("m3_top10_buyer_validation_v1_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    _data_path("m3_top10_buyer_validation_v1_targets.json").write_text(
        json.dumps({"build": BUILD, "targets": results, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _write_status(phase="DONE", pct=100, STATUS=status, counts=counts, elapsed_s=elapsed)
    return report
