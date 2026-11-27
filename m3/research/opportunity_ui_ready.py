"""Hard gate: incomplete opportunities must not appear as Active/Saved UI rows.

Unknown Closes = FAIL. A row is UI-ready only when the operator can act on it
with a real solicitation id, official link, and closing date.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_JUNK_TITLE_RE = re.compile(
    r"(?i)(?:^\s*(?:word|excel|external|office\s+depot\s+contract)\s*$"
    r"|\bNebraska DAS procurement \(public site\) - (?:Word|Excel)\b"
    r"|\bOffice Depot Contract\b)"
)

_BAD_SOL_RE = re.compile(
    r"(?i)interested\s+suppliers|addendum|http://|https://|\.pdf\b"
)

# Active must be tangible goods only — services/construction/software renewals out.
# Avoid false hits on buyer names like "Animal Services Department".
_ACTIVE_SERVICE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:tree\s+trimming|hauling|drayage|waste\s+pickup|biohazard|"
    r"consulting|staffing|launder(?:ing)?|"
    r"investigation|support|professional|management|janitorial|"
    r"custodial|landscap(?:e|ing)|grounds?\s+maintenance|"
    r"snow\s+removal|pest\s+control)\s+services?\b|"
    r"\bservices?\s+(?:required|contract|agreement|only)\b|"
    r"\blabor,\s*materials,\s*and\s*services\b|"
    r"\b(?:equipment|uniform|tractor|vehicle|truck)\s+rental\b|"
    r"\brental\s+and\s+launder\b|"
    r"\bhauling\s+of\b|\bdrayage\s+or\s+hauling\b|"
    r"\bcommute\s+solutions?\b|\bjob\s+walk\b|"
    r"\btherapy\s+training\b|\b(?:erp\)?)?therapy\b|"
    r"\bsoftware\s+(?:renewal|maintenance|subscription|license)\b|"
    r"\b(?:annual|license)\s+renewal\b|"
    r"\bmaintenance\s+and\s+support\b|"
    r"\bauthorized\s+service\s+for\b|"
    r"\bon[\s-]?site\s+calibration\b|"
    r"\banalytics\s+platform\b|\buser\s+journey\b|"
    r"\b911\s+evolution\b|\be911\b|"
    r"\bvertical\s+blind\s+installation\b|"
    r"\bwaste\s+pickup\b|"
    r"\bparts\s*&\s*labor\b|\bparts\s+and\s+labor\b|"
    r"\bmine\s+subsidence\s+assessments?\b|"
    r"\bstriping\b|\bdrainage\s+project\b|"
    r"\bfurnish,\s*deliver,\s*and\s*install\b|"
    r"\bgenerator[^.]{0,40}\binstallation\b"
    r")"
)

_GOODS_CUE_RE = re.compile(
    r"(?i)\b("
    r"purchase|furnish|supply|supplies|materials|equipment|goods|"
    r"trailer|truck|dozer|salt|aggregate|grout|pipe|lids?|"
    r"coveralls|vests|jackets|scanner|router|switch|hardware|"
    r"bags?|cases?|each|ea\b|tons?|gallons?"
    r")\b"
)

_JUNK_QTY_TITLE_RE = re.compile(
    r"(?i)^("
    r"not\s+guaranteed\b.*|"
    r"exceed\s+\$?[\d.]+\b.*|"
    r"quantit(?:y|ies)\s*&?\s*orders?\s+may\s+vary\b.*|"
    r"will\s+be\s+based\s+on\s+needs\b.*"
    r")"
)

# Construction / capital project — not a product-supply bid line.
_ACTIVE_CONSTRUCTION_RE = re.compile(
    r"(?i)\b("
    r"roof\s+replacement|fire\s+alarm\s+system\s+replacement|"
    r"window\s+and\s+door\s+replacement|"
    r"road\s+(?:overlay|resurfacing)|pavement\s+(?:repair|overlay)|"
    r"(?:&|and)\s+\w+\s+road\s+overlay|"
    r"sidewalk|curb\s+and\s+gutter|reconstruction|"
    r"renovation|remodel(?:ing)?|new\s+construction|"
    r"bid\s+pack\b|phase\s+\d+\s+bid|"
    r"site\s+electrical\s+reconfiguration|"
    r"complete\s+replacement\s+of\s+existing|"
    r"construction\s+for\b|subdivision\s+project|"
    r"draining\s+project|drainage\s+project|"
    r"commercial\s+corridor\s+initiative"
    r")"
)

_VAGUE_TITLE_RE = re.compile(
    r"(?i)^("
    r"goods\s+for\b.*|"
    r"various\s+types?\s+of\b.*|"
    r"(?:\d+\s*[—–-]\s*)?multiple\s+parts?\s+listing\s*$|"
    r"untitled|"
    r"string\s*\|\s*null|"
    r"as\s+specified\b.*|"
    r"to\s+be\s+specified\b.*|"
    r"not\s+specified\b.*|"
    r"bids?\s+for\s+materials\b.*|"
    r"washer\s+and\s+dryers?\s*$"
    r")$"
)

# Sol-number-first titles with no cleaned product phrase.
_SOL_LED_TITLE_RE = re.compile(
    r"(?i)^\s*(?:RFB|RFP|RFQ|IFB|ITB|IS)[-_/\s]*\d"
)


def parse_deadline_value(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s or s.lower() in {"unknown", "n/a", "none", "null", "tbd"}:
        return None
    s = s.replace("Z", "+00:00")
    # Strip timezone labels (CT/ET/PT/…) so strptime can see the clock time.
    s_clock = re.sub(
        r"(?i)\s*\(?\b(?:UTC|GMT|[ECMP][DS]?T|Central|Eastern|Pacific|Mountain)(?:\s+Time)?\)?\s*$",
        "",
        s,
    ).strip()
    for cand in (s, s_clock, s[:25], s[:19], s[:10]):
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        for candidate in (s_clock[:40], s[:40]):
            try:
                dt = datetime.strptime(candidate, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    # "January 5, 2027 at 2:00 PM"
    m = re.search(
        r"(?i)(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
        s,
    )
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
        except Exception:
            try:
                dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            except Exception:
                dt = None
        if dt:
            return dt.replace(tzinfo=timezone.utc)
    return None


def solicitation_ok(sol: Any) -> bool:
    s = str(sol or "").strip()
    if not s or len(s) < 3:
        return False
    if len(s) > 64:
        return False
    if _BAD_SOL_RE.search(s):
        return False
    # Title fragments used as ids (Providence fallback)
    if s.count(" ") >= 4:
        return False
    return True


def title_ok(title: Any) -> bool:
    t = str(title or "").strip()
    if len(t) < 4:
        return False
    if _JUNK_TITLE_RE.search(t):
        return False
    return True


def bidable_product_fail_reasons(item: dict[str, Any] | Any) -> list[str]:
    """Hard Active gate: product-only, concrete title, category required."""
    from m3.research.opportunity_checklist_triage import perishable_food_reason

    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    reasons: list[str] = []
    display = str(get("display_title") or "").strip()
    official = str(get("title") or "").strip()
    title = display or official
    what = str(get("what_they_want") or (get("evidence") or {}).get("what_they_want") or "")
    blob = f"{title}\n{official}\n{what}"
    want_type = str(get("want_type") or (get("evidence") or {}).get("want_type") or "").upper()
    req = str(get("requirement_class") or "").strip().lower()

    food = perishable_food_reason(title, official, what)
    if food:
        reasons.append(food)

    if want_type in {"SERVICE", "MIX", "SERVICE_OR_MIX"}:
        reasons.append(f"want_type_not_product:{want_type}")
    goodsish = bool(_GOODS_CUE_RE.search(blob))
    if req and req not in {"product_supply", "product", ""}:
        if req in {"service", "services", "mixed", "construction"}:
            reasons.append(f"requirement_class_not_product:{req}")
        elif req == "unknown_mixed" and not goodsish:
            reasons.append("requirement_class_not_product:unknown_mixed")

    m = _ACTIVE_SERVICE_RE.search(blob)
    if m:
        reasons.append(f"service_contract:{m.group(0).lower()[:80]}")
    m = _ACTIVE_CONSTRUCTION_RE.search(blob)
    if m:
        reasons.append(f"construction_not_product:{m.group(0).lower()[:80]}")

    if _VAGUE_TITLE_RE.match(title.strip()):
        reasons.append("vague_title")
    if _JUNK_QTY_TITLE_RE.match(title.strip()) or (
        " — " in title and _JUNK_QTY_TITLE_RE.match(title.split(" — ", 1)[0].strip())
    ):
        reasons.append("junk_quantity_title")
    if _SOL_LED_TITLE_RE.match(display or title):
        # Allow only if a cleaned product phrase follows an em dash.
        if " — " not in (display or title):
            reasons.append("sol_number_title_not_product")

    cat = get("product_category")
    if not str(cat or "").strip():
        reasons.append("missing_product_category")

    return reasons


def opportunity_ui_ready(item: dict[str, Any] | Any) -> tuple[bool, list[str]]:
    """Return (ok, fail_reasons). Unknown closes / bad id / junk title = FAIL."""
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    reasons: list[str] = []
    title = get("display_title") or get("title")
    if not title_ok(title):
        reasons.append("junk_or_missing_title")
    sol = get("solicitation_number")
    if not solicitation_ok(sol):
        reasons.append("bad_or_missing_solicitation")
    url = get("official_url")
    if not str(url or "").strip():
        reasons.append("missing_official_url")
    deadline = get("deadline_at")
    if parse_deadline_value(deadline) is None:
        reasons.append("unknown_closes")
    reasons.extend(bidable_product_fail_reasons(item))
    # de-dupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return (not uniq, uniq)


def enrich_deadline_from_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Fill deadline_at from triage / doc-read close_date when listing omitted it."""
    out = dict(item)
    if parse_deadline_value(out.get("deadline_at")) is not None:
        return out
    triage = out.get("triage") or {}
    doc_final = ((out.get("doc_read") or {}).get("final") or {})
    for cand in (
        triage.get("close_date"),
        doc_final.get("close_date"),
        out.get("response_deadline"),
        (out.get("evidence") or {}).get("close_date"),
    ):
        dt = parse_deadline_value(cand)
        if dt is not None:
            out["deadline_at"] = dt.isoformat()
            break
    return out
