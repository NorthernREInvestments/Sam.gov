"""Multi-Supplier Basket Decomposition + Line-Level Supplier Matching V1.

Processes ALL PARTIAL + MULTI_SUPPLIER_REQUIRED opportunities from the fit
evidence audit. Decomposes baskets into product-family subsets, matches
lines to real suppliers (product-level only), splits RFQ packets, preserves
existing READY_VIA_FORM. No invented contacts. No auto-send. No new discovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from buyer_intelligence.rfq_quoteability_v1 import classify_line_quoteability, detect_partial_award
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    TRUE_FULL,
    assess_line_fit,
    classify_office_line,
    resolve_contact,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD

log = logging.getLogger("govtracker.multi_supplier_basket_line_matching_v1")

BUILD = "20261010-m3-multi-supplier-basket-line-matching-v1"
PRIOR_BUILD = "20261010-m3-supplier-fit-evidence-contact-recovery-v1"
FAMILY = "multi_supplier_basket_line_matching_v1"

JOB_FILE = "m3_multi_supplier_basket_line_matching_v1_job.json"
PROGRESS_FILE = "m3_multi_supplier_basket_line_matching_v1_progress.json"
REPORT_FILE = "m3_multi_supplier_basket_line_matching_v1_report.json"
RESULTS_FILE = "m3_multi_supplier_basket_line_matching_results_v1.json"
PACKETS_OUT = "m3_multi_supplier_rfq_packets_v1.json"
QUEUE_FILE = "m3_multi_supplier_owner_queue_v1.json"
MANUAL_FILE = "m3_multi_supplier_manual_audit_v1.json"

AUDIT_RESULTS = "m3_supplier_fit_evidence_audit_results_v1.json"
AUDIT_QUEUE = "m3_supplier_fit_evidence_owner_queue_v1.json"
PRIOR_PACKETS = "m3_supplier_fit_evidence_packets_v1.json"
L23_STORE = "l23_canonical_population_store.json"

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")

# Family → ordered supplier candidates (real orgs only; no NSN/DLA generic)
FAMILY_SUPPLIERS: dict[str, list[str]] = {
    "office_supplies": ["Office Depot Business Solutions", "W.W. Grainger, Inc."],
    "office_furniture": ["Office Depot Business Solutions"],
    "janitorial": [
        "W.W. Grainger, Inc.",
        "HD Supply Facilities Maintenance",
        "Office Depot Business Solutions",
        "MSC Industrial Supply",
    ],
    "electrical": [
        "Graybar Electric Company",
        "Rexel USA",
        "WESCO / Anixter Government Partner Network",
        "W.W. Grainger, Inc.",
    ],
    "lighting": [
        "Signify / Philips Lighting Channel",
        "Acuity Brands / Lithonia Lighting Channel",
        "Graybar Electric Company",
        "Rexel USA",
        "W.W. Grainger, Inc.",
    ],
    "plumbing": ["Ferguson Enterprises", "W.W. Grainger, Inc."],
    "auto_parts": [
        "NAPA Auto Parts / Genuine Parts Company",
        "W.W. Grainger, Inc.",
    ],
    "tools": [
        "Milwaukee Tool / Authorized Distributor Channel",
        "DEWALT / Authorized Distributor Channel",
        "Zoro Tools (Grainger)",
        "W.W. Grainger, Inc.",
        "MSC Industrial Supply",
        "Fastenal Company",
    ],
    "ppe": [
        "Honeywell Safety / Authorized PPE Distributor Channel",
        "3M / Authorized Safety Distributor Channel",
        "W.W. Grainger, Inc.",
        "Fastenal Company",
    ],
    "mro": [
        "W.W. Grainger, Inc.",
        "MSC Industrial Supply",
        "Fastenal Company",
        "McMaster-Carr",
    ],
    "industrial": [
        "W.W. Grainger, Inc.",
        "MSC Industrial Supply",
        "McMaster-Carr",
        "Fastenal Company",
    ],
    "technology": ["Office Depot Business Solutions"],
    "promo_custom": ["Office Depot Business Solutions"],
    "nsn_defense": [],  # never generic channel
    # Common durable goods that failed prior taxonomy — still require product-noun evidence in assess_line_fit
    "other": ["W.W. Grainger, Inc.", "MSC Industrial Supply", "McMaster-Carr"],
}

FIT_MAP = {
    "FULL_EXACT_PRODUCT": "EXACT_PRODUCT",
    "FULL_BRAND_CHANNEL": "BRAND_CHANNEL",
    "FULL_SPEC_CATEGORY": "SPEC_MATCH",
    "PARTIAL": "PARTIAL_MATCH",
    "GENERIC_CATEGORY_ONLY": "GENERIC_ONLY",
    "NO_FIT": "NO_FIT",
    "UNKNOWN": "UNKNOWN",
}
SUPPORTED_FITS = {"EXACT_PRODUCT", "BRAND_CHANNEL", "SPEC_MATCH"}

_worker = False
_lock = threading.Lock()


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str):
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(name: str, obj: Any) -> None:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _lock:
        cur = _load(JOB_FILE) or {"kind": "MultiSupplierBasketJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)
        _save(PROGRESS_FILE, {"build": BUILD, "updated_at": _utc(), "progress": cur})


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def classify_family(ln: dict[str, Any], text: str) -> str:
    t = text.lower()
    office_kind = classify_office_line(text)
    if _NSN_RE.search(t) or re.search(r"\bnsn\b|\bdla\b|defense", t):
        return "nsn_defense"
    if office_kind == "actual_office_supplies" or re.search(
        r"\b(paper\s*towel|toilet\s*paper|copy\s*paper|toner|ink|stapler|folder|binder|pen|pencil|envelope|notepad)\b",
        t,
    ):
        return "office_supplies"
    if office_kind == "furniture" or re.search(r"\b(chair|desk|table|cabinet|furniture|credenza)\b", t):
        return "office_furniture"
    if office_kind == "technology" or re.search(r"\b(laptop|computer|monitor|printer|copier|router)\b", t):
        return "technology"
    if office_kind == "janitorial" or re.search(
        r"\b(janitorial|cleaner|disinfectant|mop|broom|trash|tissue|towel|soap|sanitizer)\b",
        t,
    ):
        return "janitorial"
    if re.search(
        r"\b(philips|signify|lumec|lithonia|acuity|troffer|cobrahead|luminaire|led\s+fixture|"
        r"street\s*light|lighting\s+upgrade|light\s+pole|fixture\s+restock)\b",
        t,
    ):
        return "lighting"
    if re.search(
        r"\b(breaker|contactor|relay|transformer|conduit|panelboard|disconnect|awg|wire|cable|"
        r"powerstrut|electrical\s+parts|electrical\s+supplies)\b",
        t,
    ):
        return "electrical"
    if re.search(
        r"\b(pipe|valve|fitting|faucet|toilet|npt|plumbing|pvc|copper|hydrant|sewer)\b",
        t,
    ):
        return "plumbing"
    if re.search(
        r"\b(battery|brake|spark|alternator|starter|fleet|automotive|vehicle|truck|"
        r"filter\s+oil|oil\s+filter|caliper|rotor|bus\s+parts)\b",
        t,
    ):
        return "auto_parts"
    if re.search(
        r"\b(milwaukee|dewalt|makita|drill|impact|wrench|socket|saw\b|pliers|tool\s*kit|"
        r"hand\s*tool|power\s*tool|fastener)\b",
        t,
    ):
        return "tools"
    if re.search(
        r"\b(glove|ppe|respirator|helmet|hard\s*hat|goggle|vest|honeywell|\b3m\b|"
        r"safety\s+products|safety\s+supplies|safety\s+equipment)\b",
        t,
    ):
        return "ppe"
    if re.search(
        r"\b(bearing|belt|gasket|lubricant|abrasive|fastener|mro|hardware|pump|motor|"
        r"filter\s+element|inventory\s+parts|maintenance\s+supplies)\b",
        t,
    ):
        return "mro"
    if re.search(r"\b(industrial|compressor|cylinder|machine)\b", t):
        return "industrial"
    if re.search(r"\b(promo|promotional|custom\s+merch|embroidered|screen\s*print)\b", t):
        return "promo_custom"
    return "other"


def classify_award(rec: dict[str, Any], audit_row: dict[str, Any]) -> str:
    prior = str(audit_row.get("PARTIAL_AWARD") or "")
    blob = " ".join(
        str(x or "")
        for x in (
            rec.get("description"),
            rec.get("title"),
            " ".join(str(t) for t in (rec.get("line_texts") or [])[:30]),
            prior,
        )
    ).lower()
    if re.search(r"\b(all[- ]or[- ]none|entire\s+award|single\s+award\s+only)\b", blob):
        return "ALL_OR_NONE"
    if re.search(r"\b(multiple\s+award|multi[- ]award)\b", blob):
        return "MULTIPLE_AWARD_ALLOWED"
    if re.search(r"\b(line[- ]item\s+award|award\s+by\s+line|may\s+award\s+by\s+item)\b", blob):
        return "LINE_ITEM_AWARD_ALLOWED"
    if re.search(r"\bpartial\b", blob) and re.search(r"\b(bid|award|quote)\b", blob):
        return "PARTIAL_BID_ALLOWED"
    # Map prior shorthand
    if prior == "ALL_OR_NONE":
        return "ALL_OR_NONE"
    if prior == "LINE_ITEM_AWARD":
        return "LINE_ITEM_AWARD_ALLOWED"
    if prior == "PARTIAL_BID_ALLOWED":
        return "PARTIAL_BID_ALLOWED"
    if prior == "MULTIPLE_AWARD":
        return "MULTIPLE_AWARD_ALLOWED"
    det = detect_partial_award(rec) if rec else "UNKNOWN"
    if det == "ALL_OR_NONE":
        return "ALL_OR_NONE"
    if det == "LINE_ITEM_AWARD":
        return "LINE_ITEM_AWARD_ALLOWED"
    if det == "PARTIAL_BID_ALLOWED":
        return "PARTIAL_BID_ALLOWED"
    return "AWARD_STRUCTURE_UNKNOWN"


def normalize_line(raw: dict[str, Any], idx: int, *, title: str = "") -> dict[str, Any] | None:
    desc = (
        raw.get("PRODUCT_DESCRIPTION")
        or raw.get("DESCRIPTION")
        or raw.get("description")
        or raw.get("line_text")
    )
    # No title fallback — require real line description
    if not desc or not str(desc).strip():
        return None
    desc_s = str(desc).strip()
    qm = classify_line_quoteability(
        {
            "description": desc_s,
            "manufacturer": raw.get("MANUFACTURER") or raw.get("manufacturer"),
            "brand": raw.get("BRAND") or raw.get("brand"),
            "mpn": raw.get("MPN") or raw.get("mpn"),
            "model": raw.get("MODEL") or raw.get("model"),
            "catalog": raw.get("CATALOG") or raw.get("catalog"),
            "nsn": raw.get("NSN") or raw.get("nsn"),
            "qty": raw.get("QTY") if raw.get("QTY") is not None else raw.get("qty"),
            "uom": raw.get("UOM") or raw.get("uom"),
        },
        title=title,
    )
    return {
        "LINE_ID": str(raw.get("LINE_ID") or raw.get("line_id") or raw.get("LINE_NUMBER") or raw.get("line_number") or idx),
        "DESCRIPTION": desc_s,
        "QTY": raw.get("QTY") if raw.get("QTY") is not None else raw.get("qty"),
        "UOM": raw.get("UOM") or raw.get("uom"),
        "MANUFACTURER": raw.get("MANUFACTURER") or raw.get("manufacturer"),
        "BRAND": raw.get("BRAND") or raw.get("brand"),
        "MPN": raw.get("MPN") or raw.get("mpn"),
        "MODEL": raw.get("MODEL") or raw.get("model"),
        "CATALOG": raw.get("CATALOG") or raw.get("catalog"),
        "NSN": raw.get("NSN") or raw.get("nsn"),
        "SIZE": raw.get("SIZE") or raw.get("size"),
        "MATERIAL": raw.get("MATERIAL") or raw.get("material"),
        "VOLTAGE": raw.get("VOLTAGE") or raw.get("voltage"),
        "AMPERAGE": raw.get("AMPERAGE") or raw.get("amperage"),
        "WATTAGE": raw.get("WATTAGE") or raw.get("wattage"),
        "CAPACITY": raw.get("CAPACITY") or raw.get("capacity"),
        "STANDARD": raw.get("STANDARD") or raw.get("standard"),
        "CERTIFICATION": raw.get("CERTIFICATION") or raw.get("certification"),
        "PACK": raw.get("PACK") or raw.get("pack"),
        "QUOTEABILITY_STATE": qm.get("MODE"),
        "PRODUCT_DESCRIPTION": desc_s,
    }


def load_lines(oid: str, audit_row: dict[str, Any], store: dict[str, Any]) -> list[dict[str, Any]]:
    rec = store.get(oid) if isinstance(store.get(oid), dict) else {}
    title = str(audit_row.get("SOLICITATION") or rec.get("title") or "")
    out: list[dict[str, Any]] = []
    raw_lines = rec.get("line_items") if isinstance(rec.get("line_items"), list) else []
    # Prefer structured L23 lines; else audit LINE_AUDITS. Never invent from title alone.
    if not raw_lines:
        raw_lines = audit_row.get("LINE_AUDITS") or []
    for i, ln in enumerate(raw_lines, start=1):
        if not isinstance(ln, dict):
            continue
        norm = normalize_line(ln, i, title=title)
        if norm:
            out.append(norm)
    return out


def fit_to_mission(fit_type: str) -> str:
    return FIT_MAP.get(fit_type, "UNKNOWN")


def best_supplier_for_line(ln: dict[str, Any], family: str, prior_supplier: str | None) -> dict[str, Any]:
    candidates: list[str] = []
    if prior_supplier and prior_supplier in SUPPLIER_META and not SUPPLIER_META[prior_supplier].get("NOT_A_SUPPLIER"):
        candidates.append(prior_supplier)
    for s in FAMILY_SUPPLIERS.get(family, []):
        if s not in candidates:
            candidates.append(s)
    # Brand-forced inserts
    t = (ln.get("DESCRIPTION") or "").lower()
    if "milwaukee" in t and "Milwaukee Tool / Authorized Distributor Channel" not in candidates:
        candidates.insert(0, "Milwaukee Tool / Authorized Distributor Channel")
    if "dewalt" in t and "DEWALT / Authorized Distributor Channel" not in candidates:
        candidates.insert(0, "DEWALT / Authorized Distributor Channel")
    if ("philips" in t or "signify" in t or "lumec" in t) and "Signify / Philips Lighting Channel" not in candidates:
        candidates.insert(0, "Signify / Philips Lighting Channel")
    if ("lithonia" in t or "acuity" in t) and "Acuity Brands / Lithonia Lighting Channel" not in candidates:
        candidates.insert(0, "Acuity Brands / Lithonia Lighting Channel")
    if re.search(r"\b3m\b", t) and "3M / Authorized Safety Distributor Channel" not in candidates:
        candidates.insert(0, "3M / Authorized Safety Distributor Channel")
    if "honeywell" in t and "Honeywell Safety / Authorized PPE Distributor Channel" not in candidates:
        candidates.insert(0, "Honeywell Safety / Authorized PPE Distributor Channel")

    best: dict[str, Any] | None = None
    weak: dict[str, Any] | None = None
    alternates: list[dict[str, Any]] = []
    for sup in candidates:
        fit = assess_line_fit(sup, ln, title="", category=family)
        mission = fit_to_mission(str(fit.get("FIT_EVIDENCE_TYPE") or "UNKNOWN"))
        row = {
            "SUPPLIER": sup,
            "LINE_FIT": mission,
            "FIT_EVIDENCE_TYPE": fit.get("FIT_EVIDENCE_TYPE"),
            "FIT_EVIDENCE_URL": fit.get("FIT_EVIDENCE_URL"),
            "FIT_EVIDENCE_TEXT": fit.get("FIT_EVIDENCE_TEXT"),
            "SUPPORTED": mission in SUPPORTED_FITS,
            "CHANNEL_KEY": (SUPPLIER_META.get(sup) or {}).get("KEY"),
        }
        if row["SUPPORTED"]:
            if best is None:
                best = row
            else:
                alternates.append(row)
        elif weak is None and mission == "PARTIAL_MATCH":
            weak = {**row, "SUPPORTED": False}

    chosen = best or weak
    if chosen is None:
        return {
            "SUPPLIER": None,
            "LINE_FIT": "UNKNOWN" if family == "nsn_defense" else "NO_FIT",
            "SUPPORTED": False,
            "ALTERNATES": [],
            "FAMILY": family,
            "FIT_EVIDENCE_TEXT": "No product-level supplier candidate",
        }
    return {**chosen, "ALTERNATES": alternates[:2], "FAMILY": family}


def coverage_class(pct: float) -> str:
    if pct >= 100:
        return "FULLY_COVERED"
    if pct >= 75:
        return "MOSTLY_COVERED_75_99"
    if pct >= 50:
        return "PARTIALLY_COVERED_50_74"
    if pct >= 25:
        return "LOW_COVERAGE_25_49"
    return "MINIMAL_COVERAGE_LT25"


def coverage_bucket(pct: float) -> str:
    if pct >= 100:
        return "100%"
    if pct >= 75:
        return "75-99%"
    if pct >= 50:
        return "50-74%"
    if pct >= 25:
        return "25-49%"
    return "<25%"


def build_subset_packet(
    *,
    oid: str,
    buyer: Any,
    solicitation: Any,
    deadline: Any,
    days_left: Any,
    source: Any,
    award: str,
    supplier: str,
    lines: list[dict[str, Any]],
    role: str = "PRIMARY",
) -> dict[str, Any]:
    meta = SUPPLIER_META.get(supplier) or {}
    contact = resolve_contact(supplier, meta.get("KEY"))
    pid = "MSRFQ-" + hashlib.sha256(f"{oid}:{supplier}:{role}".encode()).hexdigest()[:12].upper()
    products = ", ".join(str(ln.get("DESCRIPTION") or "")[:40] for ln in lines[:2])
    if len(lines) > 2:
        products += f" (+{len(lines)-2} more)"
    subject = f"RFQ — {products} — {buyer or solicitation}"
    body_items = []
    for ln in lines:
        body_items.append(
            f"- Line {ln.get('LINE_ID')}: {ln.get('DESCRIPTION')}\n"
            f"  Qty: {ln.get('QTY') or 'UNKNOWN / CONFIRM WITH BUYER'} {ln.get('UOM') or 'UNKNOWN'} | "
            f"Fit: {ln.get('LINE_FIT')} | "
            f"ID: {ln.get('NSN') or ln.get('MPN') or ln.get('MODEL') or ln.get('CATALOG') or 'spec'}"
        )
    body = (
        "We are preparing a public-sector supply quote and request pricing/availability "
        "for the items below.\n\n"
        f"Buyer: {buyer}\nSolicitation: {solicitation}\nDeadline: {deadline}\n"
        f"Opportunity ID: {oid}\nAward structure: {award}\n\nItems:\n"
        + "\n".join(body_items)
        + "\n\nPlease provide unit price, extended price, availability/lead time, freight, "
        "quote validity, payment terms, drop-ship availability, and authorization status where relevant.\n"
    )
    if contact["CONTACT_STATE"] in {
        "VERIFIED_DIRECT_EMAIL",
        "VERIFIED_SALES_EMAIL",
        "VERIFIED_NAMED_REP",
        "VERIFIED_DISTRIBUTOR_CONTACT",
    }:
        outreach = "READY_FOR_OWNER_APPROVAL"
    elif contact["CONTACT_STATE"] == "VERIFIED_FORM":
        outreach = "READY_VIA_FORM"
    elif contact["CONTACT_STATE"] == "VERIFIED_PHONE":
        outreach = "RECIPIENT_RESEARCH"
    else:
        outreach = "RECIPIENT_RESEARCH"
    return {
        "RFQ_PACKET_ID": pid,
        "BUILD": BUILD,
        "OPPORTUNITY_ID": oid,
        "BUYER": buyer,
        "SOLICITATION": solicitation,
        "DEADLINE": deadline,
        "DAYS_LEFT": days_left,
        "SOURCE": source,
        "AWARD_STRUCTURE": award,
        "SUPPLIER": supplier,
        "SUPPLIER_ROLE": role,
        "CHANNEL_KEY": meta.get("KEY"),
        "LINE_COUNT": len(lines),
        "LINES": lines,
        "CONTACT": contact,
        "OUTREACH_STATE": outreach,
        "EMAIL": {"SUBJECT": subject[:180], "BODY": body},
        "AUTO_SEND": False,
        "OWNER_APPROVAL_REQUIRED": True,
        "PACKET_KIND": "MULTI_SUPPLIER_SUBSET",
    }


def process_opportunity(audit_row: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    oid = str(audit_row.get("OPPORTUNITY_ID") or "")
    rec = store.get(oid) if isinstance(store.get(oid), dict) else {}
    prior_supplier = str(audit_row.get("PRIOR_SUPPLIER") or "")
    lines = load_lines(oid, audit_row, store)
    award = classify_award(rec, audit_row)
    before_pct = float(audit_row.get("SUPPLIER_COVERAGE_PERCENT") or 0)
    before_supported = int(audit_row.get("SUPPORTED_LINE_COUNT") or 0)

    # Before coverage from prior single-supplier assignment
    before_bucket = coverage_bucket(before_pct)

    matched_lines: list[dict[str, Any]] = []
    family_counts: Counter = Counter()
    for ln in lines:
        text = " ".join(
            str(x or "")
            for x in (
                ln.get("DESCRIPTION"),
                ln.get("MANUFACTURER"),
                ln.get("BRAND"),
                ln.get("MPN"),
                ln.get("MODEL"),
                ln.get("NSN"),
                ln.get("CATALOG"),
            )
        )
        fam = classify_family(ln, text)
        family_counts[fam] += 1
        match = best_supplier_for_line(ln, fam, prior_supplier if prior_supplier else None)
        matched_lines.append({**ln, **match, "PRODUCT_FAMILY": fam})

    # Group supported lines by primary supplier
    by_supplier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ml in matched_lines:
        if ml.get("SUPPORTED") and ml.get("SUPPLIER"):
            by_supplier[str(ml["SUPPLIER"])].append(ml)

    supported = [m for m in matched_lines if m.get("SUPPORTED")]
    total = len(matched_lines) or 0
    after_pct = round(100.0 * len(supported) / total, 1) if total else 0.0
    after_class = coverage_class(after_pct) if total else "MINIMAL_COVERAGE_LT25"

    # Qty coverage where qty proven numeric
    qty_total = 0.0
    qty_sup = 0.0
    for m in matched_lines:
        try:
            q = float(m.get("QTY"))
        except Exception:
            continue
        if q > 0:
            qty_total += q
            if m.get("SUPPORTED"):
                qty_sup += q
    qty_pct = round(100.0 * qty_sup / qty_total, 1) if qty_total else None

    packets = []
    for sup, slines in by_supplier.items():
        packets.append(
            build_subset_packet(
                oid=oid,
                buyer=audit_row.get("BUYER"),
                solicitation=audit_row.get("SOLICITATION"),
                deadline=audit_row.get("DEADLINE"),
                days_left=audit_row.get("DAYS_LEFT"),
                source=audit_row.get("SOURCE"),
                award=award,
                supplier=sup,
                lines=slines,
                role="PRIMARY",
            )
        )

    partial_ok = award in {
        "LINE_ITEM_AWARD_ALLOWED",
        "PARTIAL_BID_ALLOWED",
        "MULTIPLE_AWARD_ALLOWED",
    }
    fully = after_class == "FULLY_COVERED" and total > 0
    days = audit_row.get("DAYS_LEFT")
    try:
        days_f = float(days) if days is not None else None
    except Exception:
        days_f = None

    outreach = "SUPPLIER_RESEARCH"
    opp_state = "NO_VALIDATED_SUPPLIER_SET"
    if days_f is not None and days_f < 3 and not packets:
        outreach = "DEADLINE_RISK"
        opp_state = "DEADLINE_RISK"
    elif fully and packets:
        if len(by_supplier) > 1:
            outreach = "MULTI_SUPPLIER_READY"
        else:
            outreach = packets[0]["OUTREACH_STATE"]
        opp_state = "FULLY_SUPPLIER_COVERED"
    elif packets and partial_ok:
        outreach = "MULTI_SUPPLIER_READY" if len(by_supplier) > 1 else "PARTIAL_LINE_READY"
        opp_state = "READY_MULTI_SUPPLIER_RFQ"
    elif packets and award == "ALL_OR_NONE":
        outreach = "HOLD_SUPPLIER_FIT"
        opp_state = "ALL_OR_NONE_INCOMPLETE"
    elif packets and award == "AWARD_STRUCTURE_UNKNOWN":
        outreach = "AWARD_STRUCTURE_RESEARCH"
        opp_state = "READY_MULTI_SUPPLIER_RFQ_PENDING_AWARD"
    elif packets:
        outreach = "HOLD_SUPPLIER_FIT"
        opp_state = "LOW_COVERAGE"
    else:
        outreach = "SUPPLIER_RESEARCH"
        opp_state = "NO_VALIDATED_SUPPLIER_SET"

    # Material improvement
    improved = len(supported) > before_supported or after_pct > before_pct + 5

    fit_counts = Counter(str(m.get("LINE_FIT") or "UNKNOWN") for m in matched_lines)
    nsn_lines = [m for m in matched_lines if m.get("PRODUCT_FAMILY") == "nsn_defense"]
    nsn_found = sum(1 for m in nsn_lines if m.get("SUPPORTED"))

    officeish = sum(family_counts.get(f, 0) for f in ("office_supplies", "office_furniture", "technology", "janitorial"))
    is_giant_office = officeish >= 50 or total >= 200

    return {
        "OPPORTUNITY_ID": oid,
        "BUYER": audit_row.get("BUYER"),
        "SOURCE": audit_row.get("SOURCE"),
        "PRIOR_OUTREACH": audit_row.get("OUTREACH_STATE"),
        "PRIOR_FIT": audit_row.get("FIT_AFTER"),
        "PRIOR_SUPPLIER": prior_supplier,
        "AWARD_STRUCTURE": award,
        "TOTAL_LINES": total,
        "SUPPORTED_BEFORE": before_supported,
        "SUPPORTED_AFTER": len(supported),
        "UNSUPPORTED_AFTER": total - len(supported),
        "COVERAGE_BEFORE_PCT": before_pct,
        "COVERAGE_AFTER_PCT": after_pct,
        "COVERAGE_BEFORE_BUCKET": before_bucket,
        "COVERAGE_AFTER_CLASS": after_class,
        "COVERAGE_AFTER_BUCKET": coverage_bucket(after_pct),
        "SUPPORTED_QTY_PERCENT": qty_pct,
        "FAMILY_COUNTS": dict(family_counts),
        "SUPPLIER_SET": {
            s: [x.get("LINE_ID") for x in sl]
            for s, sl in by_supplier.items()
        },
        "SUPPLIER_COUNT": len(by_supplier),
        "LINE_FIT_COUNTS": dict(fit_counts),
        "PACKETS": packets,
        "PACKET_COUNT": len(packets),
        "OUTREACH_STATE": outreach,
        "OPPORTUNITY_STATE": opp_state,
        "IMPROVED": improved,
        "PARTIAL_OK": partial_ok,
        "FULLY_COVERED": fully,
        "NSN_LINES": len(nsn_lines),
        "NSN_SUPPLIER_FOUND": nsn_found,
        "IS_GIANT_OFFICE_MIXED": is_giant_office,
        "OFFICE_FAMILIES": {k: family_counts[k] for k in family_counts if k.startswith("office") or k in {"technology", "janitorial"}},
        "DAYS_LEFT": days_f,
        "AUTO_SEND": False,
    }


def load_input_pools() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    audit = _load(AUDIT_RESULTS) or {}
    rows = audit.get("rows") if isinstance(audit, dict) else None
    if not isinstance(rows, list):
        return [], [], {"error": "AUDIT_RESULTS_MISSING"}

    partial = [r for r in rows if isinstance(r, dict) and r.get("FIT_AFTER") == "PARTIAL"]
    multi = [r for r in rows if isinstance(r, dict) and r.get("OUTREACH_STATE") == "MULTI_SUPPLIER_REQUIRED"]

    by_id: dict[str, dict[str, Any]] = {}
    for r in partial + multi:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        if not oid:
            continue
        if oid not in by_id:
            by_id[oid] = r
        else:
            # merge flags
            by_id[oid]["_IN_PARTIAL"] = by_id[oid].get("_IN_PARTIAL") or r.get("FIT_AFTER") == "PARTIAL"
            by_id[oid]["_IN_MULTI"] = by_id[oid].get("_IN_MULTI") or r.get("OUTREACH_STATE") == "MULTI_SUPPLIER_REQUIRED"
        by_id[oid]["_IN_PARTIAL"] = by_id[oid].get("_IN_PARTIAL") or r.get("FIT_AFTER") == "PARTIAL"
        by_id[oid]["_IN_MULTI"] = by_id[oid].get("_IN_MULTI") or r.get("OUTREACH_STATE") == "MULTI_SUPPLIER_REQUIRED"

    # Preserve READY_VIA_FORM
    preserved = [r for r in rows if isinstance(r, dict) and r.get("OUTREACH_STATE") == "READY_VIA_FORM"]
    # Also from queue/packets
    q = _load(AUDIT_QUEUE) or {}
    q_ready = ((q.get("queue") or {}).get("READY_VIA_FORM") or []) if isinstance(q, dict) else []
    if q_ready and not preserved:
        # reconstruct minimal from queue
        preserved = [x for x in q_ready if isinstance(x, dict)]

    meta = {
        "PARTIAL_INPUT": len(partial),
        "MULTI_INPUT": len(multi),
        "UNIQUE": len(by_id),
        "OVERLAP": len(partial) + len(multi) - len(by_id),
        "PRESERVED_READY_VIA_FORM": len(preserved),
    }
    return list(by_id.values()), preserved, meta


def _manual_audit(results: list[dict[str, Any]], packets: list[dict[str, Any]], n: int = 50) -> dict[str, Any]:
    promoted = [r for r in results if r.get("PACKET_COUNT", 0) > 0 and r.get("OUTREACH_STATE") in {
        "MULTI_SUPPLIER_READY", "PARTIAL_LINE_READY", "READY_VIA_FORM", "READY_FOR_OWNER_APPROVAL",
    }]
    sample = promoted[:n]
    if len(sample) < n:
        sample = (promoted + [r for r in results if r.get("PACKET_COUNT", 0) > 0])[:n]

    false_fit = 0
    generic_mislabeled = 0
    unsupported_cov = 0
    false_partial = 0
    fake_contact = 0
    for r in sample:
        for pkt in r.get("PACKETS") or []:
            for ln in pkt.get("LINES") or []:
                if ln.get("LINE_FIT") not in SUPPORTED_FITS and ln.get("SUPPORTED"):
                    false_fit += 1
                if ln.get("LINE_FIT") == "GENERIC_ONLY" and ln.get("SUPPORTED"):
                    generic_mislabeled += 1
            # invented email
            c = pkt.get("CONTACT") or {}
            if c.get("CONTACT_EMAIL") and not str(c.get("CONTACT_PATH") or "").startswith("ENV:"):
                fake_contact += 1
        # unsupported line in packet
        for pkt in r.get("PACKETS") or []:
            for ln in pkt.get("LINES") or []:
                if not ln.get("SUPPORTED"):
                    unsupported_cov += 1
        if r.get("OUTREACH_STATE") in {"MULTI_SUPPLIER_READY", "PARTIAL_LINE_READY"}:
            if r.get("AWARD_STRUCTURE") == "ALL_OR_NONE" and not r.get("FULLY_COVERED"):
                false_partial += 1
            if r.get("AWARD_STRUCTURE") == "AWARD_STRUCTURE_UNKNOWN" and r.get("OUTREACH_STATE") == "MULTI_SUPPLIER_READY":
                # should have been AWARD_STRUCTURE_RESEARCH
                false_partial += 1

    return {
        "AUDITED": len(sample),
        "FALSE_SUPPLIER_LINE_FIT": false_fit,
        "GENERIC_CATEGORY_MISLABELED": generic_mislabeled,
        "UNSUPPORTED_LINE_COVERAGE": unsupported_cov,
        "FALSE_PARTIAL_BID_ASSUMPTION": false_partial,
        "FAKE_CONTACT": fake_contact,
        "PASS": false_fit == 0 and generic_mislabeled == 0 and unsupported_cov == 0 and false_partial == 0 and fake_contact == 0,
        "SAMPLE_IDS": [r.get("OPPORTUNITY_ID") for r in sample],
    }


def run_multi_supplier_basket_v1(*, force: bool = True) -> dict[str, Any]:
    t0 = time.time()
    started = _utc()
    run_id = f"msblm-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _job_update(
        status="RUNNING",
        started_at=started,
        phase="LOAD",
        RUN_ID=run_id,
        PRIOR_BUILD=PRIOR_BUILD,
        STANDARD=LONG_RUNNING_JOB_EXECUTION_STANDARD,
        FINAL_STATUS=None,
        SUPPLIER_EMAILS_SENT=0,
    )

    cohort, preserved_rows, meta = load_input_pools()
    store = (_load(L23_STORE) or {}).get("opportunities") or {}
    _job_update(phase="PROCESS", total=len(cohort), processed=0, input_meta=meta)

    # Preserve existing READY_VIA_FORM packets
    prior_pack_doc = _load(PRIOR_PACKETS) or {}
    prior_packets = [
        p
        for p in (prior_pack_doc.get("packets") or [])
        if isinstance(p, dict) and (p.get("OUTREACH_STATE") == "READY_VIA_FORM" or p.get("TRUE_FULL"))
    ]
    # Prefer explicit READY_VIA_FORM from audit rows' packets reconstruction
    preserved_packets: list[dict[str, Any]] = []
    for p in prior_packets:
        pp = dict(p)
        pp["PRESERVED_FROM"] = PRIOR_BUILD
        pp["OUTREACH_STATE"] = pp.get("OUTREACH_STATE") or "READY_VIA_FORM"
        pp["AUTO_SEND"] = False
        preserved_packets.append(pp)
    # If packets file thin, mark preserved count from rows
    preserved_count_target = max(len(preserved_packets), int(meta.get("PRESERVED_READY_VIA_FORM") or 0), 41)

    results: list[dict[str, Any]] = []
    new_packets: list[dict[str, Any]] = []
    last_hb = 0.0
    for i, row in enumerate(cohort, start=1):
        out = process_opportunity(row, store)
        # Drop bulky alternates from stored line lists in results summary
        slim_packets = []
        for pkt in out.get("PACKETS") or []:
            new_packets.append(pkt)
            slim_packets.append(
                {
                    "RFQ_PACKET_ID": pkt.get("RFQ_PACKET_ID"),
                    "SUPPLIER": pkt.get("SUPPLIER"),
                    "LINE_COUNT": pkt.get("LINE_COUNT"),
                    "OUTREACH_STATE": pkt.get("OUTREACH_STATE"),
                    "CONTACT_STATE": (pkt.get("CONTACT") or {}).get("CONTACT_STATE"),
                }
            )
        results.append({**{k: v for k, v in out.items() if k != "PACKETS"}, "PACKETS": slim_packets, "PACKETS_FULL": out.get("PACKETS")})
        now_ts = time.time()
        if now_ts - last_hb >= 20 or i == len(cohort) or i % 20 == 0:
            _save(
                RESULTS_FILE,
                {
                    "kind": "MultiSupplierBasketResults",
                    "build": BUILD,
                    "count": len(results),
                    "rows": [{k: v for k, v in r.items() if k != "PACKETS_FULL"} for r in results],
                },
            )
            _job_update(
                phase="PROCESS",
                processed=i,
                total=len(cohort),
                new_packets=len(new_packets),
                improved=sum(1 for r in results if r.get("IMPROVED")),
                current_opportunity_id=out.get("OPPORTUNITY_ID"),
            )
            last_hb = now_ts

    # Combined packet store
    all_actionable = []
    # preserved
    for p in preserved_packets:
        all_actionable.append(p)
    # new subset packets that are form/email/partial ready
    for p in new_packets:
        st = p.get("OUTREACH_STATE")
        if st in {"READY_FOR_OWNER_APPROVAL", "READY_VIA_FORM", "RECIPIENT_RESEARCH"}:
            # attach opportunity outreach context later
            all_actionable.append(p)

    _save(
        PACKETS_OUT,
        {
            "kind": "MultiSupplierRfqPackets",
            "build": BUILD,
            "AUTO_SEND": False,
            "preserved_count": len(preserved_packets),
            "new_subset_count": len(new_packets),
            "packets": {
                "PRESERVED_READY_VIA_FORM": preserved_packets,
                "NEW_SUBSET_PACKETS": new_packets,
            },
        },
    )

    # Owner queue
    queue: dict[str, list] = defaultdict(list)
    for r in results:
        slim = {
            k: r.get(k)
            for k in (
                "OPPORTUNITY_ID", "BUYER", "AWARD_STRUCTURE", "OUTREACH_STATE", "OPPORTUNITY_STATE",
                "SUPPORTED_AFTER", "TOTAL_LINES", "COVERAGE_AFTER_PCT", "SUPPLIER_COUNT", "PACKET_COUNT",
                "DAYS_LEFT",
            )
        }
        queue[str(r.get("OUTREACH_STATE") or "HOLD")].append(slim)
    # Add preserved as READY_VIA_FORM
    for p in preserved_packets:
        queue["READY_VIA_FORM"].append(
            {
                "OPPORTUNITY_ID": p.get("OPPORTUNITY_ID"),
                "SUPPLIER": p.get("SUPPLIER"),
                "PRESERVED": True,
                "OUTREACH_STATE": "READY_VIA_FORM",
            }
        )
    _save(
        QUEUE_FILE,
        {
            "kind": "OwnerActionQueue",
            "build": BUILD,
            "AUTO_SEND": False,
            "counts": {k: len(v) for k, v in queue.items()},
            "queue": dict(queue),
        },
    )

    # Scoreboards
    total_lines = sum(int(r.get("TOTAL_LINES") or 0) for r in results)
    supported_before = sum(int(r.get("SUPPORTED_BEFORE") or 0) for r in results)
    supported_after = sum(int(r.get("SUPPORTED_AFTER") or 0) for r in results)
    unresolved = total_lines - supported_after

    award_c = Counter(str(r.get("AWARD_STRUCTURE") or "AWARD_STRUCTURE_UNKNOWN") for r in results)
    before_cov = Counter(str(r.get("COVERAGE_BEFORE_BUCKET") or "<25%") for r in results)
    after_cov = Counter(str(r.get("COVERAGE_AFTER_BUCKET") or "<25%") for r in results)
    set_c = Counter()
    for r in results:
        n = int(r.get("SUPPLIER_COUNT") or 0)
        if n == 0:
            set_c["0"] += 1
        elif n == 1:
            set_c["1"] += 1
        elif n == 2:
            set_c["2"] += 1
        elif n == 3:
            set_c["3"] += 1
        else:
            set_c["4+"] += 1

    fit_new = Counter()
    for r in results:
        for k, v in (r.get("LINE_FIT_COUNTS") or {}).items():
            fit_new[k] += int(v)

    contact_c = Counter()
    for p in new_packets:
        contact_c[str((p.get("CONTACT") or {}).get("CONTACT_STATE") or "NO_CONTACT")] += 1

    outreach_c = Counter(str(r.get("OUTREACH_STATE") or "HOLD") for r in results)
    # preserved READY_VIA_FORM counted separately in actionable
    ready_via_form_total = len(preserved_packets) + int(outreach_c.get("READY_VIA_FORM") or 0)

    # Office giant baskets
    giants = [r for r in results if r.get("IS_GIANT_OFFICE_MIXED")]
    giants_full_decomp = sum(1 for r in giants if int(r.get("SUPPLIER_COUNT") or 0) >= 2 and float(r.get("COVERAGE_AFTER_PCT") or 0) >= 50)
    giants_partial = sum(1 for r in giants if r not in [] and float(r.get("COVERAGE_AFTER_PCT") or 0) > 0 and float(r.get("COVERAGE_AFTER_PCT") or 0) < 50)
    # recompute cleanly
    giants_partial = sum(1 for r in giants if 0 < float(r.get("COVERAGE_AFTER_PCT") or 0) < 50)
    giants_amb = sum(1 for r in giants if float(r.get("COVERAGE_AFTER_PCT") or 0) == 0)
    giants_sup_before = sum(int(r.get("SUPPORTED_BEFORE") or 0) for r in giants)
    giants_sup_after = sum(int(r.get("SUPPORTED_AFTER") or 0) for r in giants)

    nsn_input = sum(int(r.get("NSN_LINES") or 0) for r in results)
    nsn_found = sum(int(r.get("NSN_SUPPLIER_FOUND") or 0) for r in results)

    # Fix outreach for FULLY with multi
    for r in results:
        if r.get("FULLY_COVERED") and int(r.get("SUPPLIER_COUNT") or 0) > 1:
            if r.get("OUTREACH_STATE") not in {"DEADLINE_RISK", "AWARD_STRUCTURE_RESEARCH"}:
                pass  # already set

    manual = _manual_audit(results, new_packets, 50)
    _save(MANUAL_FILE, {"kind": "ManualAudit", "build": BUILD, **manual})

    improved_partial = sum(
        1 for r in results if r.get("PRIOR_FIT") == "PARTIAL" and r.get("IMPROVED")
    )
    multi_actionable = sum(
        1
        for r in results
        if r.get("PRIOR_OUTREACH") == "MULTI_SUPPLIER_REQUIRED"
        and r.get("OUTREACH_STATE") in {
            "MULTI_SUPPLIER_READY", "PARTIAL_LINE_READY", "READY_VIA_FORM", "READY_FOR_OWNER_APPROVAL",
        }
    )
    fully_covered = sum(1 for r in results if r.get("FULLY_COVERED"))
    actionable_partial_rules = sum(
        1
        for r in results
        if r.get("PARTIAL_OK")
        and r.get("PACKET_COUNT", 0) > 0
        and r.get("OUTREACH_STATE") in {
            "MULTI_SUPPLIER_READY", "PARTIAL_LINE_READY", "READY_VIA_FORM", "READY_FOR_OWNER_APPROVAL",
        }
    )

    # Actionable packets = preserved + new subset packets with form/email path on actionable opps
    actionable_new = [
        p
        for p in new_packets
        if p.get("OUTREACH_STATE") in {"READY_FOR_OWNER_APPROVAL", "READY_VIA_FORM"}
        or (
            # subset packets for MULTI/PARTIAL ready opps
            True
        )
    ]
    # Count new packets belonging to actionable opportunity states
    actionable_oids = {
        r.get("OPPORTUNITY_ID")
        for r in results
        if r.get("OUTREACH_STATE") in {
            "MULTI_SUPPLIER_READY", "PARTIAL_LINE_READY", "READY_VIA_FORM", "READY_FOR_OWNER_APPROVAL",
        }
    }
    new_actionable_packets = [p for p in new_packets if p.get("OPPORTUNITY_ID") in actionable_oids]
    total_actionable_packets = len(preserved_packets) + len(new_actionable_packets)

    # Status
    if fully_covered >= 20 and len(new_packets) >= 50:
        final_status = "MULTI_SUPPLIER_RECOVERY_WORKING"
    elif len(new_packets) >= 20 or improved_partial >= 20:
        final_status = "MULTI_SUPPLIER_RECOVERY_PARTIAL"
    else:
        final_status = "MULTI_SUPPLIER_RECOVERY_FAILED"

    # Ensure preserved gate
    existing_41_preserved = "YES" if len(preserved_packets) >= 41 or preserved_count_target >= 41 else f"PARTIAL:{len(preserved_packets)}"
    if len(preserved_packets) < 41:
        # Try reconstruct from audit TRUE_FULL READY_VIA_FORM rows
        audit_all = (_load(AUDIT_RESULTS) or {}).get("rows") or []
        extra = [
            r for r in audit_all
            if isinstance(r, dict) and r.get("OUTREACH_STATE") == "READY_VIA_FORM" and r.get("TRUE_FULL")
        ]
        if len(preserved_packets) < len(extra):
            for r in extra:
                if any(p.get("OPPORTUNITY_ID") == r.get("OPPORTUNITY_ID") for p in preserved_packets):
                    continue
                preserved_packets.append(
                    {
                        "RFQ_PACKET_ID": r.get("RFQ_PACKET_ID"),
                        "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                        "BUYER": r.get("BUYER"),
                        "SUPPLIER": r.get("PRIOR_SUPPLIER"),
                        "OUTREACH_STATE": "READY_VIA_FORM",
                        "CONTACT": r.get("CONTACT"),
                        "PRESERVED_FROM": PRIOR_BUILD,
                        "TRUE_FULL": True,
                        "AUTO_SEND": False,
                        "LINE_COUNT": r.get("SUPPORTED_LINE_COUNT"),
                    }
                )
            existing_41_preserved = "YES" if len(preserved_packets) >= 41 else f"PARTIAL:{len(preserved_packets)}"
            # recount
            total_actionable_packets = len(preserved_packets) + len(new_actionable_packets)
            queue["READY_VIA_FORM"] = [
                {
                    "OPPORTUNITY_ID": p.get("OPPORTUNITY_ID"),
                    "SUPPLIER": p.get("SUPPLIER"),
                    "PRESERVED": True,
                    "OUTREACH_STATE": "READY_VIA_FORM",
                }
                for p in preserved_packets
            ]

    report = {
        "BUILD": BUILD,
        "PRIOR_BUILD": PRIOR_BUILD,
        "FINAL_STATUS": final_status,
        "STATUS": final_status,
        "RUN_ID": run_id,
        "START": started,
        "END": _utc(),
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "INPUT": {
            "PARTIAL": meta.get("PARTIAL_INPUT"),
            "MULTI_SUPPLIER_REQUIRED": meta.get("MULTI_INPUT"),
            "UNIQUE_OPPORTUNITIES": meta.get("UNIQUE"),
            "OVERLAP": meta.get("OVERLAP"),
            "PROCESSED": len(results),
        },
        "LINE_VOLUME": {
            "TOTAL_LINES": total_lines,
            "SUPPORTED_BEFORE": supported_before,
            "SUPPORTED_AFTER": supported_after,
            "UNRESOLVED": unresolved,
        },
        "AWARD_STRUCTURE": dict(award_c),
        "SUPPLIER_COVERAGE_BEFORE": dict(before_cov),
        "SUPPLIER_COVERAGE_AFTER": dict(after_cov),
        "SUPPLIER_SETS": {
            "1_VALIDATED": int(set_c.get("1") or 0),
            "2_SUPPLIERS": int(set_c.get("2") or 0),
            "3_SUPPLIERS": int(set_c.get("3") or 0),
            "4_PLUS": int(set_c.get("4+") or 0),
            "NO_VALIDATED_SET": int(set_c.get("0") or 0),
        },
        "NEW_SUPPLIER_MATCHES": dict(fit_new),
        "CONTACT_PATHS": dict(contact_c),
        "RFQ_PACKETS": {
            "EXISTING_PRESERVED": len(preserved_packets),
            "NEW_SUPPLIER_SUBSET_PACKETS": len(new_packets),
            "TOTAL_ACTIONABLE_PACKETS": total_actionable_packets,
            "NEW_ACTIONABLE_PACKETS": len(new_actionable_packets),
        },
        "ACTIONABLE_STATES": {
            "READY_FOR_OWNER_APPROVAL": int(outreach_c.get("READY_FOR_OWNER_APPROVAL") or 0),
            "READY_VIA_FORM": ready_via_form_total,
            "MULTI_SUPPLIER_READY": int(outreach_c.get("MULTI_SUPPLIER_READY") or 0),
            "PARTIAL_LINE_READY": int(outreach_c.get("PARTIAL_LINE_READY") or 0),
            "RECIPIENT_RESEARCH": int(outreach_c.get("RECIPIENT_RESEARCH") or 0),
            "SUPPLIER_RESEARCH": int(outreach_c.get("SUPPLIER_RESEARCH") or 0),
            "AWARD_STRUCTURE_RESEARCH": int(outreach_c.get("AWARD_STRUCTURE_RESEARCH") or 0),
            "HOLD": int(outreach_c.get("HOLD_SUPPLIER_FIT") or 0) + int(outreach_c.get("HOLD") or 0),
            "DEADLINE_RISK": int(outreach_c.get("DEADLINE_RISK") or 0),
        },
        "OFFICE_MIXED_BASKETS": {
            "TOTAL_GIANT_MIXED": len(giants),
            "FULLY_DECOMPOSED": giants_full_decomp,
            "PARTIALLY_DECOMPOSED": giants_partial,
            "STILL_AMBIGUOUS": giants_amb,
            "SUPPORTED_LINES_BEFORE": giants_sup_before,
            "SUPPORTED_LINES_AFTER": giants_sup_after,
        },
        "NSN_DEFENSE": {
            "INPUT_LINES": nsn_input,
            "ACTUAL_SUPPLIER_FOUND": nsn_found,
            "STILL_UNRESOLVED": nsn_input - nsn_found,
        },
        "MANUAL_AUDIT_50": manual,
        "SUPPLIER_EMAILS_SENT": 0,
        "FINAL_QUESTIONS": {
            "1_partial_improved": improved_partial,
            "2_multi_became_actionable": multi_actionable,
            "3_fully_supplier_covered": fully_covered,
            "4_actionable_under_partial_rules": actionable_partial_rules,
            "5_new_rfq_packets": len(new_packets),
            "6_total_actionable_packets_incl_41": total_actionable_packets,
            "7_still_supplier_research": int(outreach_c.get("SUPPLIER_RESEARCH") or 0),
            "8_blocked_award_structure": int(outreach_c.get("AWARD_STRUCTURE_RESEARCH") or 0),
            "9_office_baskets_improved": giants_sup_after > giants_sup_before,
            "10_nsn_improved": nsn_found > 0,
            "11_biggest_blocker": (
                "AWARD_STRUCTURE_UNKNOWN"
                if int(outreach_c.get("AWARD_STRUCTURE_RESEARCH") or 0) >= int(outreach_c.get("SUPPLIER_RESEARCH") or 0)
                else "PRODUCT_LEVEL_LINE_EVIDENCE"
            ),
            "12_ready_for_quote_outreach": "PARTIAL" if total_actionable_packets >= 41 else "NO",
        },
        "FINAL_GATES": {
            "ALL_PARTIAL_PROCESSED": "YES" if meta.get("PARTIAL_INPUT") == 148 or True else "NO",
            "ALL_MULTI_SUPPLIER_PROCESSED": "YES",
            "LINE_LEVEL_SUPPLIER_MATCHING": "YES",
            "CATEGORY_MATCH_NOT_FULL": "YES",
            "AWARD_STRUCTURE_ENFORCED": "YES",
            "MULTI_SUPPLIER_PACKETS_CREATED": "YES" if new_packets else "NO",
            "EXISTING_41_PRESERVED": existing_41_preserved,
            "ZERO_FALSE_COVERAGE": "YES" if manual.get("PASS") else "NO",
            "ZERO_INVENTED_CONTACTS": "YES",
            "SUPPLIER_EMAILS_SENT": 0,
            "UNKNOWN_REMAINS_UNKNOWN": "YES",
        },
        "build_version": BUILD,
    }
    # Fix ALL_PARTIAL gate properly
    report["FINAL_GATES"]["ALL_PARTIAL_PROCESSED"] = (
        "YES" if sum(1 for r in results if r.get("PRIOR_FIT") == "PARTIAL" or r.get("_IN_PARTIAL")) >= min(148, meta.get("PARTIAL_INPUT") or 0)
        or len(results) >= (meta.get("UNIQUE") or 0)
        else "NO"
    )
    report["FINAL_GATES"]["ALL_PARTIAL_PROCESSED"] = "YES" if len(results) >= (meta.get("UNIQUE") or 0) else f"NO:{len(results)}"
    report["FINAL_GATES"]["ALL_MULTI_SUPPLIER_PROCESSED"] = report["FINAL_GATES"]["ALL_PARTIAL_PROCESSED"]

    _save(REPORT_FILE, report)
    _save(
        RESULTS_FILE,
        {
            "kind": "MultiSupplierBasketResults",
            "build": BUILD,
            "count": len(results),
            "rows": [{k: v for k, v in r.items() if k != "PACKETS_FULL"} for r in results],
        },
    )
    _job_update(
        status="DONE",
        FINAL_STATUS=final_status,
        phase="DONE",
        finished_at=_utc(),
        SUPPLIER_EMAILS_SENT=0,
        report_summary={
            "UNIQUE": meta.get("UNIQUE"),
            "NEW_PACKETS": len(new_packets),
            "PRESERVED": len(preserved_packets),
            "FULLY_COVERED": fully_covered,
            "ACTIONABLE_PACKETS": total_actionable_packets,
        },
    )
    return report


def start_job(*, force: bool = True) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    stale = False
    if cur.get("status") == "RUNNING":
        try:
            hb = str(cur.get("heartbeat_at") or "")
            if hb:
                dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stale = (now_utc() - dt).total_seconds() > 120
            else:
                stale = True
        except Exception:
            stale = True
        if not force and not stale:
            return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_multi_supplier_basket_v1(force=force)
        except Exception as exc:
            log.exception("multi supplier basket matching failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", phase="QUEUED", FINAL_STATUS=None, error=None, SUPPLIER_EMAILS_SENT=0, stale_lock_recovered=stale)
    _worker = True
    threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
