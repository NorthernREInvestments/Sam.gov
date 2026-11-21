"""Recover opportunity fields from listing/title evidence — never fabricate."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from m3.web.titles import extract_product_from_official_title, resolve_buyer_display

_SOL_FROM_TITLE = re.compile(
    r"(?i)\b((?:RFB|RFP|RFQ|IFB|ITB|BID)[-/\s]?[A-Z0-9][-A-Z0-9/]*)\b"
)
_CLOSING_IN_TITLE = re.compile(
    r"(?i)Closing\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?))?"
)
_QTY = re.compile(
    r"(?i)\b(?:qty|quantity|one|two|three|\d+)\s*(?:\((?:1|one)\))?\s*(?:each|ea|units?)?\b|"
    r"\b(\d+)\s+(?:each|units?|pcs?|pieces?)\b|"
    r"\bone\s*\(\s*1\s*\)\b|"
    r"\b(\d+)\s*[xX×]\b"
)
_BUDGET = re.compile(
    r"(?i)(?:budget|estimated\s+(?:cost|value|amount)|not\s+to\s+exceed|NTE|"
    r"approx(?:imate)?\s+value|estimated\s+contract\s+value)"
    r"[^\d$]{0,20}(?:\$\s*)?([\d,]+(?:\.\d+)?)\s*(k|m|million|thousand)?"
)
_MPN = re.compile(r"\b([A-Z]{1,6}[-/]?\d{2,}[A-Z0-9\-/]*)\b")

FIELD_STATUS = {
    "verified": "Verified",
    "not_yet_researched": "Not Yet Researched",
    "research_in_progress": "Research In Progress",
    "not_published": "Not Published",
    "unable_to_retrieve": "Unable to Retrieve",
}


def _parse_deadline(raw: Any) -> tuple[datetime | None, str | None, str]:
    """Return (dt, timezone_label, field_status)."""
    if raw is None or raw == "":
        return None, None, "not_published"
    try:
        if isinstance(raw, datetime):
            dt = raw
        else:
            s = str(raw).strip().replace("Z", "+00:00")
            # Full ISO with offset
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                # Truncated ISO without offset
                dt = datetime.fromisoformat(s[:19])
                return dt.replace(tzinfo=timezone.utc), "UTC", "verified"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            return dt, "UTC", "verified"
        # Prefer named zone when offset is standard
        off = dt.utcoffset()
        if off is not None and off.total_seconds() == 0:
            return dt.astimezone(timezone.utc), "UTC", "verified"
        # Keep offset label like UTC-5 / UTC+0
        total = int(off.total_seconds() // 60) if off else 0
        sign = "+" if total >= 0 else "-"
        hh, mm = divmod(abs(total), 60)
        label = f"UTC{sign}{hh:02d}" + (f":{mm:02d}" if mm else "")
        return dt, label, "verified"
    except Exception:
        # Title-style US date
        m = _CLOSING_IN_TITLE.search(str(raw))
        if m:
            try:
                md = datetime.strptime(m.group(1), "%m/%d/%Y" if len(m.group(1).split("/")[-1]) == 4 else "%m/%d/%y")
                return md.replace(tzinfo=timezone.utc), "UTC (date only; time not published)", "verified"
            except Exception:
                pass
        return None, None, "unable_to_retrieve"


def human_datetime(dt: datetime | None, tz_label: str | None = None) -> dict[str, str]:
    if not dt:
        return {
            "date": "Unknown",
            "time": "Unknown",
            "timezone": tz_label or "Unknown",
            "status": "not_published",
            "human": "Not published on the listing",
        }
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    off = dt.utcoffset()
    if tz_label:
        label = tz_label
    elif off is not None and off.total_seconds() == 0:
        label = "UTC"
    elif off is not None:
        total = int(off.total_seconds() // 60)
        sign = "+" if total >= 0 else "-"
        hh, mm = divmod(abs(total), 60)
        label = f"UTC{sign}{hh:02d}" + (f":{mm:02d}" if mm else "")
    else:
        label = "UTC"
    local = dt
    return {
        "date": local.strftime("%b %d, %Y"),
        "time": local.strftime("%I:%M %p").lstrip("0"),
        "timezone": label,
        "status": "verified",
        "human": f"{local.strftime('%b %d, %Y')} at {local.strftime('%I:%M %p').lstrip('0')} ({label})",
    }


def enrich_opportunity_fields(item: dict[str, Any]) -> dict[str, Any]:
    """
    Fill gaps from title/listing evidence already present on the item.
    Returns a new dict; records field_status map for detail UI.
    """
    out = dict(item)
    evidence = dict(out.get("evidence") or {})
    field_status: dict[str, str] = dict(evidence.get("field_status") or {})
    reasons: dict[str, str] = dict(evidence.get("unavailable_reasons") or {})
    title = str(out.get("title") or "")

    # Buyer
    buyer = resolve_buyer_display(out.get("buyer_name"), source_name=evidence.get("source_name"))
    if buyer:
        out["buyer_name"] = buyer
        field_status["buyer_name"] = "verified"
    else:
        field_status.setdefault("buyer_name", "not_published")
        reasons.setdefault("buyer_name", "Purchasing entity not published on the listing retrieved by M3.")

    # Solicitation number from title
    if not out.get("solicitation_number"):
        m = _SOL_FROM_TITLE.search(title)
        if m:
            out["solicitation_number"] = m.group(1).replace(" ", "-").upper()
            field_status["solicitation_number"] = "verified"
            evidence["solicitation_from"] = "title_pattern"
        else:
            field_status["solicitation_number"] = "not_published"
            reasons["solicitation_number"] = "No solicitation number on the listing title or fields retrieved."
    else:
        field_status["solicitation_number"] = "verified"

    # Closing from structured field or title
    dt, tz_label, st = _parse_deadline(out.get("deadline_at"))
    if not dt:
        m = _CLOSING_IN_TITLE.search(title)
        if m:
            try:
                fmt = "%m/%d/%Y" if len(m.group(1).split("/")[-1]) == 4 else "%m/%d/%y"
                base = datetime.strptime(m.group(1), fmt)
                if m.group(2):
                    tpart = m.group(2).upper().replace(" ", "")
                    try:
                        tm = datetime.strptime(tpart, "%I:%M%p")
                        base = base.replace(hour=tm.hour, minute=tm.minute)
                    except ValueError:
                        pass
                dt = base.replace(tzinfo=timezone.utc)
                tz_label = "UTC (parsed from listing title; confirm local agency timezone)"
                st = "verified"
                evidence["deadline_from"] = "title_closing_text"
            except Exception:
                pass
    if dt:
        out["deadline_at"] = dt.isoformat()
        out["deadline_timezone"] = tz_label or out.get("deadline_timezone") or "UTC"
        field_status["deadline_at"] = st
    else:
        field_status["deadline_at"] = "not_published"
        reasons["deadline_at"] = "Closing date/time was not present on the listing fields M3 retrieved."

    # City / state
    if out.get("geo_state"):
        field_status["geo_state"] = "verified"
    else:
        field_status["geo_state"] = "not_published"
        reasons["geo_state"] = "State not published on the retrieved listing."
    if out.get("geo_city"):
        field_status["geo_city"] = "verified"
    else:
        field_status["geo_city"] = "not_published"
        reasons["geo_city"] = "City not published on the retrieved listing."

    # Product description from title
    product = extract_product_from_official_title(title, out.get("solicitation_number"))
    if product:
        out["product_description"] = product
        field_status["product_description"] = "verified"
    else:
        field_status["product_description"] = "not_yet_researched"
        reasons["product_description"] = "Product description not yet extracted beyond the official title."

    # Quantity hints in title
    if not out.get("quantity"):
        qm = re.search(r"(?i)\b(?:one\s*\(\s*1\s\)|qty[:\s]*(\d+)|(\d+)\s*(?:each|ea|units?))\b", title)
        if qm:
            out["quantity"] = next((g for g in qm.groups() if g), "1")
            field_status["quantity"] = "verified"
            evidence["quantity_from"] = "title_pattern"
        else:
            field_status["quantity"] = "not_published"
            reasons["quantity"] = "Quantity not published on the listing retrieved by M3."
    else:
        field_status["quantity"] = "verified"

    # Category readable
    cat = out.get("product_category")
    if cat and str(cat).lower() not in {"unknown", "none"}:
        field_status["product_category"] = "verified"
    else:
        field_status["product_category"] = "not_yet_researched"
        reasons["product_category"] = "Category not classified beyond listing text."

    # Set-aside
    if out.get("set_aside") and str(out.get("set_aside")).lower() not in {"unknown", "none"}:
        field_status["set_aside"] = "verified" if out.get("set_aside_verified") else "not_yet_researched"
    else:
        field_status["set_aside"] = "not_published"
        reasons["set_aside"] = "Set-aside not published on the retrieved listing."

    # Value / budget from evidence text or title
    if out.get("estimated_value_min") is None and out.get("estimated_value_max") is None:
        blob = " ".join(
            str(x)
            for x in (
                title,
                out.get("value_evidence"),
                evidence.get("category_raw"),
                evidence.get("status"),
            )
            if x
        )
        bm = _BUDGET.search(blob)
        if bm:
            amount = float(bm.group(1).replace(",", ""))
            unit = (bm.group(2) or "").lower()
            if unit in {"k", "thousand"}:
                amount *= 1_000
            elif unit in {"m", "million"}:
                amount *= 1_000_000
            out["estimated_value_min"] = amount
            out["estimated_value_max"] = amount
            out["estimated_value_unknown"] = False
            out["value_estimate_method"] = "published_estimate"
            out["value_evidence"] = bm.group(0)[:200]
            out["value_evidence_class"] = "published_estimate"
            field_status["estimated_value"] = "verified"
            evidence["value_from"] = "listing_text_pattern"
        else:
            out["estimated_value_unknown"] = True
            field_status["estimated_value"] = "not_published"
            reasons["estimated_value"] = (
                "No published budget or estimated value on the listing fields M3 retrieved. "
                "Historical awards and market benchmarks are not applied automatically without evidence."
            )
    else:
        field_status["estimated_value"] = "verified"

    # Documents
    url = out.get("official_url") or ""
    if url.lower().endswith((".pdf", ".doc", ".docx", ".xlsx", ".xls")):
        out["documents_available"] = "yes"
        field_status["documents"] = "verified"
    elif out.get("documents_available") in {"yes", "available"}:
        field_status["documents"] = "verified"
    elif url:
        out["documents_available"] = out.get("documents_available") or "unknown"
        field_status["documents"] = "not_yet_researched"
        reasons["documents"] = "Listing URL present; attachment inventory not yet researched."
    else:
        field_status["documents"] = "not_published"
        reasons["documents"] = "No official URL retrieved."

    # Manufacturer / MPN hints (do not invent)
    if not out.get("manufacturer"):
        # Only keep if "MFR.:" style appears
        mm = re.search(r"(?i)MFR\.?\s*:\s*([A-Z0-9][A-Z0-9 &./-]{1,60})", title)
        if mm:
            out["manufacturer"] = mm.group(1).strip()
            field_status["manufacturer"] = "verified"
        else:
            field_status["manufacturer"] = "not_published"
            reasons["manufacturer"] = "Manufacturer not stated on the retrieved listing title."
    else:
        field_status["manufacturer"] = "verified"

    evidence["field_status"] = field_status
    evidence["unavailable_reasons"] = reasons
    out["evidence"] = evidence
    out["field_status"] = field_status
    out["unavailable_reasons"] = reasons
    return out
