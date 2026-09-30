"""Phase L.8 — population-wide evidence recovery branches (no outreach)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
    generate_supplier_candidates,
)
from phase_l.quote_economics import (
    GOV_VALUE_COMPARABLE,
    GOV_VALUE_EXACT,
    GOV_VALUE_RANGE,
    GOV_VALUE_STRONG,
    GOV_VALUE_UNKNOWN,
    UOM_UNRESOLVED,
    _f,
    commercial_government_benchmark,
)
from phase_l.quote_readiness import (
    AUTHORIZATION_UNKNOWN,
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    NOT_AUTHORIZED,
    AUTHORIZATION_NOT_REQUIRED,
    classify_supplier_authorization,
    infer_quantity_from_text,
)

# Re-export for tests / callers
__all_auth__ = (
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    AUTHORIZATION_UNKNOWN,
    NOT_AUTHORIZED,
    AUTHORIZATION_NOT_REQUIRED,
)

BUILD = "20260927-m3-phase-l8-population-evidence-recovery"
ROOT = Path(__file__).resolve().parents[1]
BUYER_PATTERN_PATH = ROOT / "data" / "phase_l8_buyer_retrieval_patterns.json"
SOURCE_LEARNING_PATH = ROOT / "data" / "phase_l8_source_learning.json"

GOV_VALUE_RECOVERY = "GOV_VALUE_RECOVERY"
SUPPLIER_RECOVERY = "SUPPLIER_RECOVERY"
QUANTITY_RECOVERY = "QUANTITY_RECOVERY"
UOM_RECOVERY = "UOM_RECOVERY"
CONFIGURATION_RECOVERY = "CONFIGURATION_RECOVERY"
IDENTITY_RECOVERY = "IDENTITY_RECOVERY"

EXACT_GOV_VALUE = "EXACT_GOV_VALUE"
STRONG_GOV_VALUE = "STRONG_GOV_VALUE"
COMPARABLE_GOV_VALUE = "COMPARABLE_GOV_VALUE"
BUYER_HISTORICAL_VALUE = "BUYER_HISTORICAL_VALUE"
PRIOR_AWARDEE_SUPPLIER_LEAD = "PRIOR_AWARDEE_SUPPLIER_LEAD"
UOM_REQUIRES_MANUAL_REVIEW = "UOM_REQUIRES_MANUAL_REVIEW"
RECON_QUOTE_TARGET_ONLY = "RECON_QUOTE_TARGET_ONLY"

assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

_DOLLAR = re.compile(
    r"(?:USD\s*)?\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|\d+(?:\.\d{1,2})?)\s*(?:million|M\b)?",
    re.I,
)
_NSN = re.compile(r"\b(\d{4}[\-\s]?\d{2}[\-\s]?\d{3}[\-\s]?\d{4})\b")
_ATTACHMENT_PRIORITY = re.compile(
    r"bid\s*tab|tabulation|pricing|price\s*sheet|bid\s*form|schedule|line\s*item|"
    r"specification|equipment\s*list|quantity|quote|award|recommendation|council|board|proposal",
    re.I,
)

# Expanded category gov-value bands (Tier C recon) — labeled, never EXACT
_EXPANDED_BENCHMARKS: list[tuple[str, float, float, float, str]] = [
    (r"furniture|fixture|ff&e|office\s+furniture|classroom\s+furniture", 15000, 45000, 120000, "furniture_package_band"),
    (r"hvac|chiller|compressor|air\s+handler|rooftop\s+unit", 8000, 35000, 150000, "hvac_equipment_band"),
    (r"lubricant|fluid|oil|grease|antifreeze", 5000, 25000, 80000, "fleet_fluids_annual_band"),
    (r"brake\s+parts|oem\s+parts|vehicle\s+parts|automotive\s+parts", 5000, 20000, 75000, "vehicle_parts_annual_band"),
    (r"kubota|john\s+deere|tractor|mower|grounds\s+equipment", 15000, 40000, 90000, "grounds_equipment_band"),
    (r"school\s+bus|transit\s+bus|electric\s+bus", 80000, 150000, 400000, "bus_award_band"),
    (r"light\s+rail|rail\s+vehicle|streetcar", 200000, 500000, 2000000, "rail_vehicle_band"),
    (r"server|ups\s+unit|data\s+center", 3000, 12000, 80000, "it_server_band"),
    (r"laptop|desktop|workstation|chromebook", 600, 1200, 2500, "endpoint_device_band"),
    (r"printer|multifunction|copier", 800, 3500, 15000, "print_device_band"),
    (r"shelving|storage\s+equipment|material\s+handling", 2000, 15000, 60000, "storage_mh_band"),
    (r"generator|electrical\s+upgrade|switchgear", 10000, 50000, 200000, "electrical_equipment_band"),
    (r"camera|access\s+control|security\s+equipment", 3000, 20000, 100000, "security_systems_band"),
    (r"radio|communications?\s+monitor|viavi", 2000, 15000, 80000, "comms_test_band"),
    (r"dump\s+truck|1[\-\s]?ton|cab\s*&\s*chassis|f350|f[\-\s]?350", 45000, 65000, 95000, "medium_duty_truck_band"),
    (r"ambulance|ems\s+vehicle|fire\s+apparatus", 80000, 180000, 450000, "emergency_vehicle_band"),
    (r"sweeper|scrubber|street\s+sweeper", 40000, 90000, 250000, "sweeper_band"),
    (r"ev\s+charger|electric\s+vehicle\s+supply|evse", 5000, 25000, 100000, "evse_band"),
]


def _utc() -> str:
    return now_utc().isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def bump_source_learning(learning: dict[str, Any], *, family: str, kind: str, success: bool) -> None:
    learning.setdefault("by_family", {})
    rec = learning["by_family"].setdefault(family, {"gov": 0, "supplier": 0, "quantity": 0, "attempts": 0})
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    if success and kind in rec:
        rec[kind] = int(rec.get(kind) or 0) + 1
    learning["updated_at"] = _utc()


def classify_buyer_type(row: dict[str, Any]) -> str:
    blob = " ".join(str(x or "") for x in (row.get("agency"), row.get("buyer"), row.get("department"), row.get("title"))).lower()
    if re.search(r"\b(dept of defense|dla|gsa|nasa|federal|usaf|army|navy)\b", blob):
        return "federal"
    if re.search(r"\b(university|college|school\s+district|isd|usd)\b", blob):
        return "school" if "school" in blob or "isd" in blob or "usd" in blob else "university"
    if re.search(r"\b(transit|metro|mta|rail)\b", blob):
        return "transit"
    if re.search(r"\b(utility|water|power|electric)\b", blob):
        return "utility"
    if re.search(r"\b(county|parish)\b", blob):
        return "county"
    if re.search(r"\b(city|town|village|municipal)\b", blob):
        return "city"
    if re.search(r"\b(state|dot|department of)\b", blob):
        return "state"
    if re.search(r"\b(sourcewell|omnia|naspo|cooperative)\b", blob):
        return "cooperative"
    return "other"


def classify_product_category(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    blob = f"{row.get('title') or ''} {commercial.get('manufacturer') or ''} {commercial.get('model') or ''}".lower()
    rules = [
        (r"laptop|server|cisco|switch|ups|network|printer|computer|it\b", "IT"),
        (r"vehicle|truck|suv|ford|fleet|bus|van|police", "fleet"),
        (r"bobcat|excavator|forklift|loader|tractor|equipment|toolcat", "equipment"),
        (r"bearing|valve|filter|hose|mro|parts\b", "MRO"),
        (r"drill|grinder|welder|tool", "tools"),
        (r"furniture|fixture|shelving|office", "office_facility"),
        (r"lab|analyzer|calibration|scientific", "lab_test"),
        (r"radio|electronic|circuit|sensor", "electronics"),
        (r"ppe|helmet|safety|fall\s+protection", "safety"),
        (r"hvac|chiller|electrical", "specialty"),
    ]
    for pat, cat in rules:
        if re.search(pat, blob, re.I):
            return cat
    return "specialty"


def expanded_category_benchmark(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> dict[str, Any] | None:
    base = commercial_government_benchmark(row, commercial=commercial)
    if base:
        return base
    commercial = commercial or {}
    blob = f"{row.get('title') or ''} {commercial.get('model') or ''} {commercial.get('manufacturer') or ''}".lower()
    for pat, lo, mid, hi, label in _EXPANDED_BENCHMARKS:
        if re.search(pat, blob, re.I):
            return {
                "tier": "C",
                "state": GOV_VALUE_RANGE,
                "source": f"GOVERNMENT_CATEGORY_BENCHMARK_RECON:{label}",
                "unit_value": mid,
                "unit_value_low": lo,
                "unit_value_high": hi,
                "confidence": "APPROXIMATE",
                "final_award_value": False,
                "match_rationale": f"category_pattern:{label}",
                "quality_label": COMPARABLE_GOV_VALUE if lo != hi else STRONG_GOV_VALUE,
            }
    return None


def extract_stated_budget(row: dict[str, Any]) -> dict[str, Any] | None:
    for k in ("not_to_exceed", "budget", "estimated_value", "ceiling", "total_value", "award_amount"):
        v = _f(row.get(k))
        if v and v > 0:
            return {
                "tier": "A" if k == "not_to_exceed" else "B",
                "state": GOV_VALUE_EXACT if k == "not_to_exceed" else GOV_VALUE_STRONG,
                "source": k,
                "total_value": v,
                "confidence": "HIGH",
                "quality_label": EXACT_GOV_VALUE if k == "not_to_exceed" else STRONG_GOV_VALUE,
                "match_rationale": f"stated_field:{k}",
            }
    blob = f"{row.get('title') or ''} {row.get('description') or ''}"
    amounts = []
    for m in _DOLLAR.finditer(blob):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if "million" in m.group(0).lower() or m.group(0).rstrip().endswith("M"):
            val *= 1_000_000
        if 1000 <= val <= 50_000_000:
            amounts.append(val)
    if amounts:
        # Prefer mid-sized stated figures as total hints
        val = sorted(amounts)[len(amounts) // 2]
        return {
            "tier": "B",
            "state": GOV_VALUE_STRONG,
            "source": "text_dollar_extraction",
            "total_value": val,
            "confidence": "MEDIUM",
            "quality_label": STRONG_GOV_VALUE,
            "match_rationale": "dollar_amount_in_solicitation_text",
        }
    return None


def lookup_enrichment_cache_history(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> dict[str, Any] | None:
    commercial = commercial or {}
    try:
        from phase_l.enrichment import load_cache

        cache = load_cache()
        by_key = cache.get("by_key") or {}
    except Exception:
        return None
    keys = []
    nsn = row.get("nsn") or commercial.get("nsn")
    if nsn:
        keys.append(f"nsn:{str(nsn).replace(' ', '')}")
        # normalize dashes
        n = re.sub(r"[^\d]", "", str(nsn))
        if len(n) >= 13:
            keys.append(f"nsn:{n[:4]}-{n[4:6]}-{n[6:9]}-{n[9:13]}")
    m = _NSN.search(str(row.get("title") or ""))
    if m:
        keys.append(f"nsn:{m.group(1).replace(' ', '')}")
    for k in keys:
        for cand in (k, k.lower()):
            rec = by_key.get(cand)
            if not rec:
                continue
            hist = rec.get("history") or {}
            price = _f(hist.get("historical_award_unit_price"))
            if price and price > 0:
                return {
                    "tier": "A",
                    "state": GOV_VALUE_EXACT,
                    "source": "enrichment_cache_nsn_history",
                    "unit_value": price,
                    "confidence": "HIGH",
                    "quality_label": EXACT_GOV_VALUE,
                    "match_rationale": f"cache_key:{cand}",
                    "identity_match_type": "NSN",
                }
    return None


def buyer_historical_value(
    row: dict[str, Any],
    *,
    buyer_memory: dict[str, Any] | None = None,
    commercial: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    buyer_memory = buyer_memory or {}
    commercial = commercial or {}
    buyer = str(row.get("agency") or row.get("buyer") or "").strip().upper()
    if not buyer:
        return None
    rec = (buyer_memory.get("buyers") or {}).get(buyer) or {}
    by_product = rec.get("by_product") or {}
    # try model / mfr / title tokens
    keys = []
    for k in (commercial.get("model"), commercial.get("manufacturer"), commercial.get("mpn")):
        if k:
            keys.append(str(k).upper()[:80])
    title = str(row.get("title") or "").upper()
    if len(title) >= 12:
        keys.append(title[:60])
    for pk in keys:
        prod = by_product.get(pk)
        if not prod:
            continue
        samples = prod.get("samples") or []
        med = _f(prod.get("median_unit_price"))
        if med and med > 0:
            return {
                "tier": "B",
                "state": GOV_VALUE_STRONG,
                "source": BUYER_HISTORICAL_VALUE,
                "unit_value": med,
                "unit_value_low": _f(prod.get("min_unit_price")) or med * 0.9,
                "unit_value_high": _f(prod.get("max_unit_price")) or med * 1.1,
                "confidence": "MEDIUM",
                "quality_label": STRONG_GOV_VALUE,
                "match_rationale": f"buyer_product_memory:{pk}",
                "latest_paid": samples[-1] if samples else med,
                "median_paid": med,
                "minimum": prod.get("min_unit_price"),
                "maximum": prod.get("max_unit_price"),
                "sample_count": len(samples),
            }
    return None


def recover_government_value(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ordered gov-value recovery — strongest first. Never silently upgrades Tier C to Exact."""
    commercial = commercial or {}
    history = history or {}
    evidence: list[dict[str, Any]] = []
    sources_tried: list[str] = []

    # 1 stated budget
    sources_tried.append("stated_budget")
    e = extract_stated_budget(row)
    if e:
        evidence.append(e)

    # 2–3 history / cache exact
    sources_tried.append("history_award")
    hu = _f(history.get("historical_award_unit_price") or row.get("historical_award_unit_price"))
    if hu and hu > 0:
        evidence.append(
            {
                "tier": "A",
                "state": GOV_VALUE_EXACT,
                "source": "historical_award_unit_price",
                "unit_value": hu,
                "confidence": "HIGH",
                "quality_label": EXACT_GOV_VALUE,
                "match_rationale": "live_or_row_history",
                "identity_match_type": history.get("identity_match_type") or "EXACT",
            }
        )

    sources_tried.append("enrichment_cache")
    e = lookup_enrichment_cache_history(row, commercial)
    if e:
        evidence.append(e)

    # 4 buyer history
    sources_tried.append("buyer_history")
    e = buyer_historical_value(row, buyer_memory=buyer_memory, commercial=commercial)
    if e:
        evidence.append(e)

    # stage3 historical range
    hr = (stage3 or {}).get("historical_range") or {}
    lo, hi = _f(hr.get("low")), _f(hr.get("high"))
    if lo and hi and lo > 0:
        sources_tried.append("stage3_historical_range")
        conf = str(hr.get("confidence") or "")
        evidence.append(
            {
                "tier": "B" if conf in {"EXACT", "STRONG"} else "C",
                "state": GOV_VALUE_RANGE if lo != hi else GOV_VALUE_STRONG,
                "source": "stage3_historical_range",
                "unit_value": (lo + hi) / 2.0,
                "unit_value_low": lo,
                "unit_value_high": hi,
                "confidence": conf or "APPROXIMATE",
                "quality_label": STRONG_GOV_VALUE if conf in {"EXACT", "STRONG"} else COMPARABLE_GOV_VALUE,
                "match_rationale": "stage3_recon_range",
            }
        )

    # category benchmarks (Tier C)
    sources_tried.append("category_benchmark")
    e = expanded_category_benchmark(row, commercial)
    if e:
        evidence.append({**e, "quality_label": e.get("quality_label") or COMPARABLE_GOV_VALUE})

    if not evidence:
        return {
            "recovered": False,
            "branch": GOV_VALUE_RECOVERY,
            "state": GOV_VALUE_UNKNOWN,
            "evidence": [],
            "best": None,
            "sources_tried": sources_tried,
            "source_family": None,
        }

    # Prefer A then B then C
    best = None
    for tier in ("A", "B", "C"):
        for e in evidence:
            if e.get("tier") == tier and (e.get("unit_value") or e.get("total_value")):
                best = e
                break
        if best:
            break
    best = best or evidence[0]
    unit = _f(best.get("unit_value"))
    total = _f(best.get("total_value"))
    qty = _f(row.get("quantity"))
    if unit is None and total and qty and qty > 0:
        unit = total / qty

    fam = "category_benchmark" if "BENCHMARK" in str(best.get("source") or "") else str(best.get("source") or "gov")
    return {
        "recovered": True,
        "branch": GOV_VALUE_RECOVERY,
        "state": best.get("state") or GOV_VALUE_UNKNOWN,
        "tier": best.get("tier"),
        "unit_value": unit,
        "unit_low": _f(best.get("unit_value_low")) or unit,
        "unit_high": _f(best.get("unit_value_high")) or unit,
        "total_value": total,
        "quality_label": best.get("quality_label"),
        "match_rationale": best.get("match_rationale"),
        "source": best.get("source"),
        "source_family": fam,
        "confidence": best.get("confidence"),
        "final_award_value": best.get("tier") in {"A", "B"},
        "evidence": evidence,
        "best": best,
        "sources_tried": sources_tried,
    }


def attachment_inventory(row: dict[str, Any]) -> list[dict[str, Any]]:
    """List attachment clues without fetching (inventory-first)."""
    items = []
    for key in ("attachments", "resource_links", "documents", "files"):
        raw = row.get(key)
        if isinstance(raw, list):
            for a in raw:
                if isinstance(a, dict):
                    name = str(a.get("name") or a.get("filename") or a.get("url") or "")
                    url = a.get("url") or a.get("href")
                else:
                    name = str(a)
                    url = str(a) if str(a).startswith("http") else None
                items.append(
                    {
                        "name": name,
                        "url": url,
                        "priority": bool(_ATTACHMENT_PRIORITY.search(name)),
                    }
                )
    for key in ("document_url", "attachment_url", "bid_tab_url", "price_sheet_url"):
        if row.get(key):
            name = str(row.get(key))
            items.append({"name": name, "url": row.get(key), "priority": bool(_ATTACHMENT_PRIORITY.search(name))})
    # Sort priority first
    items.sort(key=lambda x: (not x.get("priority"), x.get("name") or ""))
    return items


def extract_quantity_from_text_blob(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    patterns = [
        (r"(?i)\bqty\.?\s*[:=]?\s*(\d+(?:\.\d+)?)", "qty_label", "EXACT"),
        (r"(?i)\bquantity\s*[:=]?\s*(\d+(?:\.\d+)?)", "quantity_label", "EXACT"),
        (r"(?i)\bclin\s*\d+[^\d]{0,40}?(\d+(?:\.\d+)?)\s*(?:ea|each|unit)", "clin_table", "EXACT"),
        (r"(?i)\b(\d+(?:\.\d+)?)\s*(?:each|ea\.?|units?|vehicles?|trucks?)\b", "count_uom", "EXACT"),
        (r"(?i)\bone\s*\(\s*1\s*\)", "one_paren", "EXACT"),
        (r"(?i)\bpurchase\s+of\s+(?:one|1)\b", "purchase_of_one", "EXACT"),
        (r"(?i)\bestimated\s+annual\s+(?:qty|quantity)\s*[:=]?\s*(\d+)", "estimated_annual", "ESTIMATED"),
        (r"(?i)\b(?:min|minimum)\s*(?:qty|order)?\s*[:=]?\s*(\d+)", "min_order", "RANGE"),
        (r"(?i)\b(?:base)\s*(?:qty|quantity)\s*[:=]?\s*(\d+)", "base_qty", "EXACT"),
        (r"(?i)\boption(?:al)?\s*(?:qty|quantity)\s*[:=]?\s*(\d+)", "option_qty", "EXACT"),
    ]
    for pat, source, conf in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        if m.lastindex:
            try:
                q = float(m.group(1))
            except (TypeError, ValueError):
                continue
        else:
            q = 1.0
        if q <= 0 or q > 1_000_000:
            continue
        return {"quantity": q, "source": source, "confidence": conf, "quality": conf}
    return None


def extract_quantity_from_table_text(text: str) -> dict[str, Any] | None:
    """Deterministic table-ish qty extraction from PDF/CSV-like text."""
    if not text:
        return None
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines[:80]):
        if re.search(r"(?i)\b(qty|quantity)\b", ln) and re.search(
            r"(?i)\b(item|description|unit|price|total|vendor|model)\b", ln
        ):
            header_idx = i
            break
    if header_idx is None:
        return extract_quantity_from_text_blob(text)
    # Parse following lines for leading qty numbers
    for ln in lines[header_idx + 1 : header_idx + 40]:
        m = re.match(r"^(\d+(?:\.\d+)?)\b", ln)
        if m:
            q = float(m.group(1))
            if 0 < q < 100000:
                return {"quantity": q, "source": "table_first_column", "confidence": "EXACT", "quality": "EXACT"}
        m2 = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(?:ea|each)\b", ln)
        if m2:
            return {
                "quantity": float(m2.group(1)),
                "source": "table_ea_cell",
                "confidence": "EXACT",
                "quality": "EXACT",
            }
    return extract_quantity_from_text_blob(text)


def recover_quantity(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    attachment_texts: list[str] | None = None,
) -> dict[str, Any]:
    commercial = commercial or {}
    sources_tried = ["row_quantity", "title_description", "attachments_inventory", "attachment_text"]
    q = _f(row.get("quantity"))
    if q and q > 0:
        return {
            "recovered": True,
            "branch": QUANTITY_RECOVERY,
            "quantity": q,
            "quality": "EXACT",
            "source": "row_quantity",
            "source_family": "structured_field",
            "unit_only": False,
        }

    blob = f"{row.get('title') or ''}\n{row.get('description') or ''}"
    hit = extract_quantity_from_text_blob(blob) or (
        {"quantity": infer_quantity_from_text(row), "source": "infer_title", "confidence": "EXACT", "quality": "EXACT"}
        if infer_quantity_from_text(row)
        else None
    )
    if hit and hit.get("quantity"):
        return {
            "recovered": True,
            "branch": QUANTITY_RECOVERY,
            "quantity": float(hit["quantity"]),
            "quality": hit.get("quality") or hit.get("confidence") or "EXACT",
            "source": hit.get("source"),
            "source_family": "solicitation_text",
            "unit_only": False,
            "sources_tried": sources_tried,
        }

    for text in attachment_texts or []:
        hit = extract_quantity_from_table_text(text) or extract_quantity_from_text_blob(text)
        if hit and hit.get("quantity"):
            return {
                "recovered": True,
                "branch": QUANTITY_RECOVERY,
                "quantity": float(hit["quantity"]),
                "quality": hit.get("quality") or "EXACT",
                "source": hit.get("source"),
                "source_family": "attachment_parse",
                "unit_only": False,
                "sources_tried": sources_tried,
            }

    # Unit-only reconnaissance when product is defined
    product_defined = bool(
        commercial.get("model")
        or commercial.get("mpn")
        or commercial.get("manufacturer")
        or re.search(
            r"\b(vehicle|truck|suv|ford|bobcat|dell|cisco|forklift|trailer|laptop|furniture|hvac)\b",
            str(row.get("title") or ""),
            re.I,
        )
    )
    if product_defined:
        return {
            "recovered": True,
            "branch": QUANTITY_RECOVERY,
            "quantity": None,
            "quality": "UNIT_ONLY",
            "source": "unit_basis_allowed",
            "source_family": "unit_only_recon",
            "unit_only": True,
            "sources_tried": sources_tried,
        }

    atts = attachment_inventory(row)
    return {
        "recovered": False,
        "branch": QUANTITY_RECOVERY,
        "quantity": None,
        "quality": "UNRESOLVED",
        "source": None,
        "source_family": None,
        "unit_only": False,
        "attachment_hints": atts[:10],
        "sources_tried": sources_tried,
    }


def normalize_uom_recovered(row: dict[str, Any], qty_info: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = str(row.get("uom") or row.get("unit_of_measure") or "").strip().lower()
    aliases = {
        "ea": "EACH",
        "each": "EACH",
        "unit": "UNIT",
        "lot": "LOT",
        "set": "SET",
        "kit": "KIT",
        "pack": "PACK",
        "case": "CASE",
        "pair": "PAIR",
        "vehicle": "VEHICLE",
        "veh": "VEHICLE",
        "system": "SYSTEM",
        "sys": "SYSTEM",
    }
    title = str(row.get("title") or "").lower()
    if not raw:
        if re.search(r"\b(vehicle|truck|suv|bus|van)\b", title):
            raw = "vehicle"
        elif re.search(r"\b(lot|lump\s+sum)\b", title):
            raw = "lot"
        elif re.search(r"\b(kit|set)\b", title):
            raw = "kit"
        elif qty_info and qty_info.get("unit_only"):
            raw = "each"
        elif qty_info and qty_info.get("quantity"):
            raw = "each"
    norm = aliases.get(raw, raw.upper() if raw else None)
    if not norm:
        return {
            "recovered": False,
            "branch": UOM_RECOVERY,
            "uom": None,
            "status": UOM_REQUIRES_MANUAL_REVIEW,
        }
    return {
        "recovered": True,
        "branch": UOM_RECOVERY,
        "uom": norm,
        "status": "UOM_RESOLVED",
        "source_family": "uom_normalization",
    }


def prior_awardee_supplier_lead(row: dict[str, Any], history: dict[str, Any] | None = None) -> dict[str, Any] | None:
    history = history or {}
    name = (
        row.get("prior_awardee")
        or row.get("incumbent")
        or history.get("awardee")
        or history.get("vendor_name")
        or history.get("recipient_name")
    )
    if not name:
        return None
    return {
        "name": str(name)[:120],
        "supplier_domain": None,
        "source_type": PRIOR_AWARDEE_SUPPLIER_LEAD,
        "authorized_status": AUTHORIZATION_UNKNOWN,
        "product_fit": "PRIOR_AWARD",
        "prior_awardee_lead": True,
        "still_active_unknown": True,
        "likely_reseller_unknown": True,
        "outreach_authorized": False,
        "note": "Discovery intelligence only — do not assume will sell to us",
    }


def category_supplier_seeds(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Supplier paths even without exact MPN — category/OEM seeds."""
    from urllib.parse import quote_plus

    commercial = commercial or {}
    cat = classify_product_category(row, commercial)
    mfr = str(commercial.get("manufacturer") or "").strip()
    model = str(commercial.get("model") or commercial.get("mpn") or "").strip()
    title_key = model or mfr or (row.get("title") or "")[:40]
    key = quote_plus(str(title_key)[:60])

    seeds_by_cat = {
        "IT": [
            ("dell.com", "OEM", AUTHORIZED_LIKELY),
            ("cdw.com", "DISTRIBUTOR", AUTHORIZED_LIKELY),
            ("cdw-g.com", "DISTRIBUTOR", AUTHORIZED_LIKELY),
            ("shi.com", "DISTRIBUTOR", AUTHORIZED_LIKELY),
            ("insight.com", "DISTRIBUTOR", AUTHORIZATION_UNKNOWN),
        ],
        "fleet": [
            ("ford.com", "OEM", AUTHORIZED_CONFIRMED if "ford" in (mfr or title_key).lower() else AUTHORIZED_LIKELY),
            ("sourcewell-mn.gov", "COOPERATIVE", AUTHORIZATION_NOT_REQUIRED),
            ("naspovaluepoint.org", "COOPERATIVE", AUTHORIZATION_NOT_REQUIRED),
            ("chevy.com", "OEM", AUTHORIZATION_UNKNOWN),
        ],
        "equipment": [
            ("bobcat.com", "OEM", AUTHORIZED_LIKELY),
            ("grainger.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
            ("machinerytrader.com", "DEALER", AUTHORIZATION_UNKNOWN),
            ("sourcewell-mn.gov", "COOPERATIVE", AUTHORIZATION_NOT_REQUIRED),
        ],
        "MRO": [
            ("grainger.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
            ("zoro.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
            ("mscdirect.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
            ("fastenal.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
        ],
        "office_facility": [
            ("staples.com", "RESELLER", AUTHORIZATION_NOT_REQUIRED),
            ("officedepot.com", "RESELLER", AUTHORIZATION_NOT_REQUIRED),
            ("grainger.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
        ],
        "specialty": [
            ("grainger.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
            ("zoro.com", "DISTRIBUTOR", AUTHORIZATION_NOT_REQUIRED),
        ],
    }
    seeds = list(seeds_by_cat.get(cat) or seeds_by_cat["specialty"])
    if mfr and "ford" in mfr.lower() and cat != "fleet":
        seeds = [("ford.com", "OEM", AUTHORIZED_LIKELY)] + seeds
    out = []
    for domain, stype, auth in seeds[:6]:
        # Confirm OEM when domain matches manufacturer
        if stype == "OEM" and mfr and mfr.lower().split()[0] in domain:
            auth = AUTHORIZED_CONFIRMED
        out.append(
            {
                "supplier_domain": domain,
                "name": domain,
                "source_type": stype,
                "authorized_status": auth,
                "authorization_state": auth,
                "product_fit": "FAMILY" if not model else "EXACT",
                "url": f"https://www.{domain}/search?q={key}",
                "contact_path": "public_web_only",
                "public_quote_path": True,
                "outreach_authorized": False,
                "category": cat,
            }
        )
    return out


def supplier_path_score(candidate: dict[str, Any]) -> dict[str, Any]:
    score = 0
    factors = []
    auth = classify_supplier_authorization(candidate)
    if auth == AUTHORIZED_CONFIRMED:
        score += 40
        factors.append("authorized_confirmed")
    elif auth == AUTHORIZED_LIKELY:
        score += 25
        factors.append("authorized_likely")
    elif auth == AUTHORIZATION_NOT_REQUIRED:
        score += 20
        factors.append("auth_not_required")
    elif auth == NOT_AUTHORIZED:
        score -= 15
        factors.append("not_authorized")
    fit = str(candidate.get("product_fit") or "").upper()
    if fit == "EXACT":
        score += 25
        factors.append("exact_fit")
    elif fit in {"FAMILY", "STRONG"}:
        score += 12
        factors.append("family_fit")
    if candidate.get("public_quote_path") or candidate.get("contact_path"):
        score += 8
        factors.append("quote_path")
    if candidate.get("government_sales") or "COOPERATIVE" in str(candidate.get("source_type") or "").upper():
        score += 10
        factors.append("gov_channel")
    if candidate.get("prior_awardee_lead"):
        score += 6
        factors.append("prior_awardee")
    if candidate.get("from_memory"):
        score += 10
        factors.append("m3_memory")
    return {**candidate, "authorization_state": auth, "SupplierPathScore": max(0, min(100, score)), "score_factors": factors}


def recover_suppliers(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commercial = commercial or {}
    base = generate_supplier_candidates(row=row, commercial=commercial, limit=6)
    # Enrichment: category seeds when base empty or sparse
    seeds = category_supplier_seeds(row, commercial)
    prior = prior_awardee_supplier_lead(row, history)
    merged = []
    seen = set()
    for c in (base or []) + (seeds or []) + ([prior] if prior else []):
        if not c:
            continue
        d = str(c.get("supplier_domain") or c.get("name") or "")
        if d and d not in seen:
            seen.add(d)
            merged.append(supplier_path_score(c))
    # Memory reuse
    if supplier_memory and commercial:
        fam = str(commercial.get("manufacturer") or commercial.get("model") or "").upper()
        for rec in supplier_memory.get("suppliers") or []:
            if fam and fam in str(rec.get("product_family") or "").upper():
                d = str(rec.get("supplier") or "")
                if d and d not in seen:
                    seen.add(d)
                    merged.append(
                        supplier_path_score(
                            {
                                "supplier_domain": d,
                                "name": d,
                                "source_type": rec.get("role") or "KNOWN",
                                "from_memory": True,
                                "product_fit": "FAMILY",
                                "outreach_authorized": False,
                            }
                        )
                    )
    merged.sort(key=lambda x: -(x.get("SupplierPathScore") or 0))
    for i, m in enumerate(merged):
        m["rank"] = i + 1
    return {
        "recovered": len(merged) > 0,
        "branch": SUPPLIER_RECOVERY,
        "suppliers": merged,
        "count": len(merged),
        "with_2plus": len(merged) >= 2,
        "with_3plus": len(merged) >= 3,
        "source_family": "oem_distributor_seeds" if merged else None,
        "prior_awardee_lead": bool(prior),
    }


def recover_configuration(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    brand_or_equal = bool(re.search(r"\b(or\s+equal|brand[\-\s]?name\s+or\s+equal|equivalent)\b", blob))
    opts = []
    for pat, label in (
        (r"\bwarranty\b", "warranty"),
        (r"\binstall", "installation"),
        (r"\bupfit|package|accessory", "upfit_or_accessories"),
        (r"\boption", "options"),
        (r"\bsupport|maintenance", "support"),
    ):
        if re.search(pat, blob):
            opts.append(label)
    return {
        "recovered": True,
        "branch": CONFIGURATION_RECOVERY,
        "base_model": commercial.get("model"),
        "manufacturer": commercial.get("manufacturer"),
        "brand_or_equal": brand_or_equal,
        "reference_product": commercial.get("model") or commercial.get("mpn"),
        "options_detected": opts,
        "bundled": bool(opts),
        "salient_characteristics": row.get("salient_characteristics") or [],
        "acceptable_substitution": (
            "exact reference OR compliant equivalent clearly identified" if brand_or_equal else "exact as specified"
        ),
        "compare_bare_to_configured": False if opts else None,
    }


def run_parallel_recovery(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    missing: set[str] | None = None,
) -> dict[str, Any]:
    """Run applicable recovery branches independently, then join."""
    missing = missing or {
        GOV_VALUE_RECOVERY,
        SUPPLIER_RECOVERY,
        QUANTITY_RECOVERY,
        UOM_RECOVERY,
        CONFIGURATION_RECOVERY,
    }
    out: dict[str, Any] = {"branches_run": sorted(missing), "joined_at": _utc()}
    if GOV_VALUE_RECOVERY in missing:
        out["gov"] = recover_government_value(
            row, commercial=commercial, history=history, buyer_memory=buyer_memory, stage3=stage3
        )
    if SUPPLIER_RECOVERY in missing:
        out["suppliers"] = recover_suppliers(
            row, commercial=commercial, history=history, supplier_memory=supplier_memory
        )
    if QUANTITY_RECOVERY in missing:
        out["quantity"] = recover_quantity(row, commercial=commercial)
    if UOM_RECOVERY in missing:
        out["uom"] = normalize_uom_recovered(row, out.get("quantity"))
    if CONFIGURATION_RECOVERY in missing:
        out["configuration"] = recover_configuration(row, commercial)
    return out
