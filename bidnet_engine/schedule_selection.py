"""PRODUCT_CANARY_SELECTION_GATE — schedule-backed tangible product only."""

from __future__ import annotations

import re
from typing import Any

from bidnet_downstream.models import PRODUCT_CLASSES
from bidnet_engine.money_path import _days_remaining

# Hard exclusions for this canary (service/construction dominated scopes).
_HARD_EXCLUDE = re.compile(
    r"\b("
    r"roof(?:ing|er)?|re-?roof|"
    r"repair(?:s|ing)?|maintenance|preventive\s+maintenance|"
    r"install(?:ation|ing)?|"
    r"construction|excavation|paving|demolition|remodel|"
    r"landscap(?:e|ing)|groundskeeping|"
    r"janitorial\s+service|custodial\s+service|"
    r"equipment\s+rental|chair\s+rental|rental\s+of|"
    r"inspection|calibration|consulting|training|engineering\s+service|"
    r"catalog\s+discount|percentage[- ]off|percent\s+off\s+list|"
    r"indefinite\s+delivery|idiq\s+catalog|"
    r"service\s+and\s+repair|labor[\-\s]?only|"
    r"transmission\s+repair|automotive\s+repair\s+and|"
    r"remote\s+support|crm\s+tool|relationship\s+management"
    r")\b",
    re.I,
)

_EXCLUDE_REASON = [
    ("ROOF", re.compile(r"\b(roof(?:ing|er)?|re-?roof)\b", re.I)),
    ("REPAIR", re.compile(r"\b(repair(?:s|ing)?|service\s+and\s+repair)\b", re.I)),
    ("MAINTENANCE", re.compile(r"\b(maintenance|preventive\s+maintenance)\b", re.I)),
    # Installation as primary scope (not "supply only" / incidental install)
    (
        "INSTALL",
        re.compile(
            r"(?:"
            r"\binstall(?:ation|ing)?\b|"
            r"\bupfitting\b|"
            r"minimum\s+equipment\s+list\s+installation|"
            r"\bMEL\s+installation\b"
            r")",
            re.I,
        ),
    ),
    ("CONSTRUCTION", re.compile(r"\b(construction|excavation|paving|demolition)\b", re.I)),
    ("CATALOG_DISCOUNT", re.compile(r"\b(catalog\s+discount|percentage[- ]off|percent\s+off\s+list)\b", re.I)),
    ("SERVICE", re.compile(
        r"\b("
        r"janitorial\s+service|custodial|landscap|rental|inspection|calibration|"
        r"consulting|training|engineering\s+service|groundskeeping|"
        r"veterinary\s+services?|professional\s+services?|"
        r"services?\s+including|service\s+contract|"
        r"and\s+services|on[\-\s]?call\s+services?|"
        r"parts,?\s+and\s+services"
        r")\b",
        re.I,
    )),
]

# Supply-dominant overrides: install/service words may appear but tangible supply is primary
_SUPPLY_DOMINANT = re.compile(
    r"\b(supply\s+only|commodit(?:y|ies)|parts?\s+only|equipment\s+only|"
    r"purchase\s+of\s+(?:equipment|supplies|materials)|furnish(?:\s+only)?)\b",
    re.I,
)

_SCHEDULE_NAME = re.compile(
    r"\b("
    r"bid\s*schedule|pricing\s*schedule|price\s*schedule|pricing\s*sheet|"
    r"line[\-\s]?item\s*schedule|item\s*list|material\s*list|bill\s*of\s*materials|\bbom\b|"
    r"equipment\s*list|supply\s*list|quote\s*sheet|itemized\s*bid|"
    r"bid\s*form|price\s*sheet|cost\s*sheet|unit\s*price"
    r")\b",
    re.I,
)
_SCHEDULE_EXT = re.compile(r"\.(xlsx?|csv|docx?)\b", re.I)
_BODY_LINE = re.compile(
    r"(?:item|clin|line)\s*[#:]?\s*\d+.{{0,80}}(?:qty|quantity|ea\b|each|uom)",
    re.I,
)
_MPN = re.compile(r"\b(?:P/?N|MPN|PART\s*#?)\s*[:#]?\s*[A-Z0-9][A-Z0-9\-./]{2,}\b", re.I)
_MODEL = re.compile(r"\bMODEL\s*[:#]?\s*[A-Z0-9][A-Z0-9\-./]{2,}\b", re.I)
_NSN = re.compile(r"\b\d{4}[- ]?\d{2}[- ]?\d{3}[- ]?\d{4}\b")
_SKU = re.compile(r"\bSKU\s*[:#]?\s*[A-Z0-9][A-Z0-9\-./]{2,}\b", re.I)
_PREFERRED = re.compile(
    r"\b(tool|office\s+suppl|ppe|glove|lighting|led|electrical\s+suppl|"
    r"plumb(?:ing)?\s+item|janitorial\s+suppl|paper\s+product|medical\s+suppl|"
    r"part(?:s)?\b|mro|furniture|commercial\s+equipment|supply\s+only|"
    r"brand[\-\s]?or[\-\s]?equal|or\s+equal)\b",
    re.I,
)


def _doc_blob(row: dict[str, Any], store_row: dict[str, Any] | None = None) -> str:
    parts: list[str] = [str(row.get("title") or "")]
    docs = []
    if store_row:
        docs = store_row.get("attachments_metadata") or store_row.get("document_inventory") or []
    docs = docs or row.get("attachments_metadata") or row.get("documents") or []
    if isinstance(docs, list):
        for d in docs:
            if isinstance(d, dict):
                parts.append(str(d.get("document_name") or d.get("filename") or d.get("name") or ""))
                parts.append(str(d.get("document_url") or d.get("url") or ""))
            else:
                parts.append(str(d)[:200])
    parts.append(str((store_row or {}).get("description") or row.get("description") or "")[:4000])
    return "\n".join(parts)


def exclusion_reason(title: str, blob: str = "") -> str | None:
    text = f"{title}\n{blob}"
    # Tangible-supply-primary titles may mention incidental install — allow through
    supply_dominant = bool(_SUPPLY_DOMINANT.search(title or ""))
    for label, rx in _EXCLUDE_REASON:
        if not rx.search(text):
            continue
        if supply_dominant and label in {"INSTALL", "SERVICE"}:
            continue
        return label
    if _HARD_EXCLUDE.search(title or "") and not supply_dominant:
        return "SERVICE"
    return None


def assess_product_dominance_for_new20(
    *,
    title: str,
    product_classification: str | None = None,
    extracted_lines: int = 0,
    operator_status: str = "",
) -> dict[str, Any]:
    """Post-selection / post-inspection gate for NEW-20 acceptance counting."""
    title = title or ""
    clf = str(product_classification or "")
    excl = exclusion_reason(title)
    if excl == "INSTALL":
        return {
            "counts_toward_new20": False,
            "exclusion": "EXCLUDED_INSTALL",
            "primary_requirement": "INSTALLATION_OR_SERVICE",
            "reason": "Title/scope is installation-dominant, not product-resale supply.",
        }
    if excl in {"SERVICE", "REPAIR", "MAINTENANCE", "CONSTRUCTION", "ROOF"}:
        return {
            "counts_toward_new20": False,
            "exclusion": f"EXCLUDED_{excl}",
            "primary_requirement": "SERVICE_OR_NON_PRODUCT",
            "reason": f"Title/scope is {excl.lower()}-dominant, not tangible product resale.",
        }
    if excl == "CATALOG_DISCOUNT" or clf == "CATALOG_DISCOUNT_ONLY":
        return {
            "counts_toward_new20": False,
            "exclusion": "EXCLUDED_CATALOG_DISCOUNT",
            "primary_requirement": "CATALOG_DISCOUNT",
            "reason": "Catalog-discount / percentage-off scope — not itemized product demand.",
        }
    if clf == "NO_PRODUCT_LINES_ACTUALLY_PRESENT":
        low = (operator_status or "").lower()
        if any(x in low for x in ("service", "grant", "administration", "labor")):
            return {
                "counts_toward_new20": False,
                "exclusion": "EXCLUDED_SERVICE",
                "primary_requirement": "SERVICE_OR_NON_PRODUCT",
                "reason": "Document inspection found no itemized product requirement.",
            }
    return {
        "counts_toward_new20": True,
        "exclusion": None,
        "primary_requirement": "PRODUCT_SUPPLY",
        "reason": "Product-dominant / eligible for NEW-20 acceptance set.",
        "extracted_lines": int(extracted_lines or 0),
    }


def detect_schedule_evidence(blob: str, *, docs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """LIKELY_PRODUCT_SCHEDULE evidence from filenames/body — no invention."""
    evidence: list[str] = []
    source_document = None
    source_type = None
    expected = None

    for d in docs or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("document_name") or d.get("filename") or d.get("name") or "")
        url = str(d.get("document_url") or d.get("url") or "")
        dtype = str(d.get("document_type") or "")
        name_blob = f"{name} {url} {dtype}"
        if (
            _SCHEDULE_NAME.search(name_blob)
            or _SCHEDULE_EXT.search(name_blob)
            or dtype
            in {
                "pricing_sheet",
                "PRICING_SCHEDULE",
                "LINE_ITEM_SCHEDULE",
                "BID_FORM",
            }
        ):
            evidence.append(f"doc:{name or url[:80]}")
            if not source_document:
                source_document = name or url
                source_type = dtype or ("spreadsheet" if _SCHEDULE_EXT.search(name_blob) else "named_schedule")
    # Tangible supply/parts language often accompanies schedules even when filename is opaque
    if re.search(r"\b(supply\s+only|parts\s+list|material(?:s)?\s+list|commodit(?:y|ies))\b", blob, re.I):
        evidence.append("title_or_body:supply_parts_list")

    if _SCHEDULE_NAME.search(blob):
        evidence.append("name_or_body:schedule_keyword")
    if _BODY_LINE.search(blob):
        evidence.append("body:item_qty_uom")
    if _MPN.search(blob):
        evidence.append("body:mpn")
    if _NSN.search(blob):
        evidence.append("body:nsn")
    if _SKU.search(blob):
        evidence.append("body:sku")
    if _MODEL.search(blob):
        evidence.append("body:model")

    # Expected line hint from "Item 1..N" style — provisional only
    nums = re.findall(r"\b(?:item|line|clin)\s*[#:]?\s*(\d{1,3})\b", blob, re.I)
    if nums:
        try:
            expected = max(int(x) for x in nums)
        except ValueError:
            expected = None

    return {
        "LIKELY_PRODUCT_SCHEDULE": bool(evidence),
        "PRODUCT_SCHEDULE_EVIDENCE": evidence,
        "SOURCE_DOCUMENT": source_document,
        "SOURCE_TYPE": source_type,
        "EXPECTED_PRODUCT_LINES": expected,
        "signals": {
            "EXACT_MPN": bool(_MPN.search(blob)),
            "EXACT_MODEL": bool(_MODEL.search(blob)),
            "NSN": bool(_NSN.search(blob)),
            "SKU": bool(_SKU.search(blob)),
            "GENERIC_PRODUCT_TABLE": bool(_SCHEDULE_NAME.search(blob) or _BODY_LINE.search(blob)),
            "MULTI_BRAND": bool(re.search(r"\b(multi[\-\s]?brand|various\s+brand|or\s+equal)\b", blob, re.I)),
        },
    }


def product_canary_selection_gate(
    row: dict[str, Any],
    store_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return {accepted, reason, ...} for PRODUCT_CANARY_SELECTION_GATE."""
    store_row = store_row or {}
    title = str(row.get("title") or store_row.get("title") or "")
    classification = row.get("classification") or store_row.get("classification")
    days = _days_remaining(row.get("deadline") or store_row.get("deadline"))
    pkg = str(row.get("package_state") or store_row.get("package_state") or "")
    docs = store_row.get("attachments_metadata") or store_row.get("document_inventory") or []
    if not isinstance(docs, list):
        docs = []
    blob = _doc_blob(row, store_row)
    excl = exclusion_reason(title, blob)
    sched = detect_schedule_evidence(blob, docs=[d for d in docs if isinstance(d, dict)])

    package_available = "ACQUIRED" in pkg.upper() or any(
        isinstance(d, dict) and (d.get("local_path") or d.get("document_url") or d.get("filename"))
        for d in docs
    )
    body_has_lines = "body:item_qty_uom" in (sched.get("PRODUCT_SCHEDULE_EVIDENCE") or [])

    reasons_fail: list[str] = []
    if classification not in PRODUCT_CLASSES:
        reasons_fail.append("NOT_PRODUCT_CLASS")
    if days is not None and days < 3:
        reasons_fail.append("INSUFFICIENT_RUNWAY")
    if excl:
        reasons_fail.append(f"EXCLUDED_{excl}")
    if not package_available and not body_has_lines:
        reasons_fail.append("NO_PACKAGE_OR_BODY_LINES")
    if not sched.get("LIKELY_PRODUCT_SCHEDULE"):
        reasons_fail.append("NO_PRODUCT_SCHEDULE")

    accepted = not reasons_fail
    score = 0
    if accepted:
        score = 50
        if package_available:
            score += 20
        if sched.get("SOURCE_TYPE") in {"pricing_sheet", "PRICING_SCHEDULE", "spreadsheet"}:
            score += 25
        if _PREFERRED.search(title) or _PREFERRED.search(blob):
            score += 15
        sig = sched.get("signals") or {}
        score += 10 * sum(1 for k in ("EXACT_MPN", "EXACT_MODEL", "NSN", "SKU") if sig.get(k))
        if days is not None and days >= 5:
            score += 10

    return {
        "accepted": accepted,
        "fail_reasons": reasons_fail,
        "exclusion": excl,
        "LIVE": True,
        "PACKAGE_AVAILABLE": package_available,
        "SOLICITATION_BODY_HAS_EXPLICIT_PRODUCT_LINES": body_has_lines,
        "DEADLINE_RUNWAY": days,
        "SERVICE_DOMINANCE": excl in {"SERVICE", "REPAIR", "MAINTENANCE"} if excl else False,
        "INSTALL_DOMINANCE": excl == "INSTALL",
        "CONSTRUCTION_DOMINANCE": excl in {"CONSTRUCTION", "ROOF"},
        **sched,
        "selection_score": score,
    }


def select_schedule_backed_candidates(
    rows: list[dict[str, Any]],
    store_by_cid: dict[str, dict[str, Any]],
    *,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter + rank for schedule-backed product canary."""
    excluded = {
        "SERVICE": 0,
        "REPAIR": 0,
        "CONSTRUCTION": 0,
        "INSTALL": 0,
        "CATALOG_DISCOUNT": 0,
        "NO_SCHEDULE": 0,
        "OTHER": 0,
        "REVIEWED": 0,
    }
    accepted: list[dict[str, Any]] = []
    provisional: list[dict[str, Any]] = []
    for r in rows:
        excluded["REVIEWED"] += 1
        cid = str(r.get("canonical_opportunity_id") or "")
        store_row = store_by_cid.get(cid) or {}
        gate = product_canary_selection_gate(r, store_row)
        title = str(r.get("title") or "")
        if not gate["accepted"]:
            reasons = gate.get("fail_reasons") or []
            if any("EXCLUDED_ROOF" in x or "EXCLUDED_CONSTRUCTION" in x for x in reasons):
                excluded["CONSTRUCTION"] += 1
            elif any("EXCLUDED_REPAIR" in x for x in reasons):
                excluded["REPAIR"] += 1
            elif any("EXCLUDED_INSTALL" in x for x in reasons):
                excluded["INSTALL"] += 1
            elif any("EXCLUDED_CATALOG" in x for x in reasons):
                excluded["CATALOG_DISCOUNT"] += 1
            elif any("EXCLUDED_SERVICE" in x or "EXCLUDED_MAINTENANCE" in x for x in reasons):
                excluded["SERVICE"] += 1
            elif any("NO_PRODUCT_SCHEDULE" in x for x in reasons):
                excluded["NO_SCHEDULE"] += 1
                # Provisional: preferred product + package, schedule may appear after download
                if (
                    gate.get("PACKAGE_AVAILABLE")
                    and not gate.get("exclusion")
                    and _PREFERRED.search(title)
                    and (gate.get("DEADLINE_RUNWAY") is None or float(gate.get("DEADLINE_RUNWAY") or 0) >= 3)
                    and (r.get("classification") in PRODUCT_CLASSES)
                ):
                    item = dict(r)
                    gate2 = dict(gate)
                    gate2["accepted"] = True
                    gate2["LIKELY_PRODUCT_SCHEDULE"] = True
                    gate2["PRODUCT_SCHEDULE_EVIDENCE"] = list(gate.get("PRODUCT_SCHEDULE_EVIDENCE") or []) + [
                        "provisional:preferred_product_package_pending_doc_inventory"
                    ]
                    gate2["selection_score"] = int(gate.get("selection_score") or 0) + 30
                    item["_schedule_gate"] = gate2
                    item["_schedule_select_score"] = gate2["selection_score"]
                    item["_days_remaining"] = gate.get("DEADLINE_RUNWAY")
                    item["_provisional_schedule"] = True
                    provisional.append(item)
            else:
                excluded["OTHER"] += 1
            continue
        item = dict(r)
        item["_schedule_gate"] = gate
        item["_schedule_select_score"] = int(gate.get("selection_score") or 0)
        item["_days_remaining"] = gate.get("DEADLINE_RUNWAY")
        accepted.append(item)
    accepted.sort(
        key=lambda x: (-int(x.get("_schedule_select_score") or 0), str(x.get("deadline") or "9999"))
    )
    if len(accepted) < limit:
        provisional.sort(
            key=lambda x: (-int(x.get("_schedule_select_score") or 0), str(x.get("deadline") or "9999"))
        )
        seen = {str(x.get("stable_key") or x.get("canonical_opportunity_id")) for x in accepted}
        for p in provisional:
            key = str(p.get("stable_key") or p.get("canonical_opportunity_id"))
            if key in seen:
                continue
            accepted.append(p)
            seen.add(key)
            if len(accepted) >= limit:
                break
    return accepted[:limit], excluded
