"""SAM_PACKAGE_PRIORITY_SCORE — spend credits only on high-value tangible product deals."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc


_SERVICE_HEAVY = re.compile(
    r"\b(consulting|construction|renovation|install(?:ation)?|maintenance service|"
    r"staffing|janitorial|landscap|professional services|engineering services)\b",
    re.I,
)
_PRODUCTISH = re.compile(
    r"\b(supply|supplies|equipment|parts?|materials?|ppe|tools?|furniture|"
    r"lighting|hvac|plumbing|mro|commodity|goods)\b",
    re.I,
)


def _parse_deadline(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw)[:32]
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[: len(fmt.replace("%", "X"))], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def score_sam_opportunity(meta: dict[str, Any], l23_row: dict[str, Any] | None = None) -> dict[str, Any]:
    row = l23_row or {}
    title = str(meta.get("title") or row.get("title") or "")
    buyer = str(meta.get("buyer") or row.get("buyer") or "")
    blob = f"{title} {buyer}"
    score = 0.0
    reasons: list[str] = []

    # Product fit
    if _PRODUCTISH.search(blob):
        score += 25
        reasons.append("product_fit")
    if _SERVICE_HEAVY.search(blob):
        score -= 40
        reasons.append("service_heavy_penalty")

    # Deadline runway
    dl = _parse_deadline(meta.get("deadline") or row.get("deadline"))
    if dl:
        now = now_utc()
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        days = (dl.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() / 86400.0
        if days < 0:
            score -= 50
            reasons.append("expired")
        elif days < 3:
            score -= 20
            reasons.append("deadline_too_close")
        elif 7 <= days <= 45:
            score += 20
            reasons.append("good_runway")
        elif days > 45:
            score += 10
            reasons.append("long_runway")

    # Identity / sol number present
    sol = (
        row.get("solicitation_event_id")
        or (row.get("row_ref") or {}).get("solicitation_number")
        or meta.get("solicitation_id")
    )
    if sol:
        score += 15
        reasons.append("has_solicitation_id")

    # NSN / federal supply class clues in title (DLA often)
    if re.search(r"\b\d{2}--", title) or re.search(r"\bNSN\b", title, re.I):
        score += 10
        reasons.append("federal_catalog_clue")

    # Prior attachments / package success hint
    if row.get("attachments_metadata"):
        score += 5
        reasons.append("prior_attachment_meta")

    # Avoid pure abstract with no URL and no sol
    if not sol and not row.get("authoritative_url") and not (row.get("row_ref") or {}).get("detail_url"):
        score -= 15
        reasons.append("thin_record")

    # Prefer commercial-sounding over pure DLA bulk if both present
    if re.search(r"\b(office|furniture|ppe|glove|toner|filter)\b", title, re.I):
        score += 12
        reasons.append("commercial_category")

    return {
        "opportunity_id": meta.get("opportunity_id"),
        "priority_score": round(score, 2),
        "reasons": reasons,
        "eligible": score >= 0 and "expired" not in reasons and "service_heavy_penalty" not in reasons,
    }


def rank_sam_candidates(metas: list[dict[str, Any]], l23_by_cid: dict[str, Any]) -> list[dict[str, Any]]:
    ranked = []
    for m in metas:
        oid = m["opportunity_id"]
        cid = oid.split(":", 1)[1] if oid.startswith("l23:") else oid
        sc = score_sam_opportunity(m, l23_by_cid.get(cid))
        ranked.append({**m, **sc})
    ranked.sort(key=lambda r: (-float(r.get("priority_score") or 0), r.get("opportunity_id") or ""))
    return ranked
