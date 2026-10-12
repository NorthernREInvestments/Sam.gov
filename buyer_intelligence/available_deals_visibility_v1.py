"""Available Deals visibility — show eligible open product opps before pricing is known.

Buckets:
  READY_TO_BID — rare; full commercial evidence
  WORTH_RESEARCHING — primary daily queue (open, product, eligible/unknown)
  BLOCKED_INELIGIBLE — HUBZone/8(a)/SDVOSB/WOSB-only, expired, non-product, etc.

Unknown profitability does NOT hide a deal.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from company_eligibility import set_aside_eligibility
from m3_data_root import data_path
from phase_l.access_gate import (
    ACCESS_NO,
    ACCESS_UNKNOWN,
    ACCESS_YES,
    SPECIAL_SET_ASIDE,
    TOTAL_SMALL_BUSINESS,
    UNRESTRICTED,
    OPEN_MARKET,
    classify_competition_access_type,
    evaluate_phase_l_access,
)

log = logging.getLogger("govtracker.available_deals_visibility_v1")

BUILD = "20261012-m3-available-deals-visibility-v1"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_adv_v1_report.json"
DEALS = f"{OUT}/m3_adv_v1_available_deals.json"
FUNNEL = f"{OUT}/m3_adv_v1_funnel_counts.json"
BASELINE = f"{OUT}/m3_adv_v1_solicitation_baseline.json"

READY = "READY_TO_BID"
WORTH = "WORTH_RESEARCHING"
BLOCKED = "BLOCKED_INELIGIBLE"
NEEDS_VERIFICATION = "NEEDS_VERIFICATION"

_SERVICE_RE = re.compile(
    r"\b(janitorial service|custodial service|grounds?\s+maintenance|lawn\s+care|"
    r"snow\s+removal|construction|renovation|remodel|installation\s+services?|"
    r"professional\s+services?|consulting|staffing|temp\s+labor)\b",
    re.I,
)
_COMMON_RE = re.compile(
    r"\b(tool(?:s|kit)?|office\s+furniture|paper|tissue|towel|janitorial|soap|cleaner|"
    r"electrical|lighting|led|lamp|ppe|glove|hardware|fastener|bolt|screw|nut|"
    r"automotive|brake|filter|battery|mro|consumable|supply|supplies|toner|"
    r"cable|wire|outlet|switch)\b",
    re.I,
)
_SPECIALTY_RE = re.compile(
    r"\b(nsn|cage|source\s+approval|oem\s+only|ammunition|munition|weapon|"
    r"hazardous|radioactive|classified|itar)\b",
    re.I,
)
_DEAD_FUNNEL = {"EXPIRED", "CANCELED", "CANCELLED", "FAST_REJECT", "HARD_REJECT"}


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, payload: dict[str, Any]) -> None:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_l23() -> dict[str, Any]:
    path = Path(data_path("l23_canonical_population_store.json"))
    if not path.exists():
        return {"opportunities": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_deadline(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        from dateutil import parser as dp

        d = dp.isoparse(str(raw))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _days_left(deadline: Any) -> float | None:
    d = _parse_deadline(deadline)
    if not d:
        return None
    return (d - now_utc()).total_seconds() / 86400.0


def _deadline_status(rec: dict[str, Any]) -> str:
    status = str(rec.get("deadline_status") or "").upper()
    if status in {
        "OPEN_CONFIRMED",
        "CLOSED_CONFIRMED",
        "DEADLINE_UNKNOWN",
        "DEADLINE_CONFLICT",
        "CANCELLED",
    }:
        return status
    try:
        from buyer_intelligence.deadline_extraction_repair_v1 import classify_deadline_status

        return classify_deadline_status(rec)
    except Exception:
        days = _days_left(rec.get("deadline"))
        if days is None:
            return "DEADLINE_UNKNOWN"
        return "OPEN_CONFIRMED" if days >= 0 else "CLOSED_CONFIRMED"


def _is_expired(rec: dict[str, Any]) -> bool:
    if str(rec.get("current_funnel_state") or "") in {"EXPIRED", "CANCELED", "CANCELLED"}:
        return True
    if str(rec.get("freshness") or "").upper() in {"EXPIRED", "CANCELED", "CANCELLED"}:
        return True
    if _deadline_status(rec) == "CANCELLED":
        return True
    # Past deadline → expired history; unknown deadline is NOT expired
    if _deadline_status(rec) == "CLOSED_CONFIRMED":
        return True
    days = _days_left(rec.get("deadline"))
    if days is not None and days < 0:
        return True
    return False


def _product_class(rec: dict[str, Any]) -> str:
    cls = str(rec.get("product_service_classification") or "").upper()
    title = str(rec.get("title") or "")
    if cls in {"PURE_SERVICE", "CONSTRUCTION"}:
        return "NON_PRODUCT"
    if cls == "TANGIBLE_PRODUCT":
        return "PRODUCT"
    if cls == "MIXED_PRODUCT_SERVICE":
        return "MIXED"
    if _SERVICE_RE.search(title) and "supply" not in title.lower() and "delivery" not in title.lower():
        return "NON_PRODUCT"
    if cls in {"", "UNKNOWN", "NONE"}:
        return "UNKNOWN_PRODUCTNESS"
    if "PRODUCT" in cls or "COMMERCIAL" in cls:
        return "PRODUCT"
    return "UNKNOWN_PRODUCTNESS"


def _structured_set_aside_parts(rec: dict[str, Any]) -> list[str]:
    """Only structured set-aside fields — never title/description (avoids false matches)."""
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    sam = rec.get("sam_raw") if isinstance(rec.get("sam_raw"), dict) else {}
    parts = [
        rec.get("set_aside"),
        rec.get("typeOfSetAside"),
        rec.get("typeOfSetAsideDescription"),
        rr.get("set_aside"),
        rr.get("typeOfSetAside"),
        rr.get("typeOfSetAsideDescription"),
        sam.get("typeOfSetAside"),
        sam.get("typeOfSetAsideDescription"),
    ]
    return [str(x).strip() for x in parts if x and str(x).strip()]


def _set_aside_text(rec: dict[str, Any]) -> str:
    return " ".join(_structured_set_aside_parts(rec))


def _set_aside_label(rec: dict[str, Any], elig: dict[str, Any]) -> str:
    """Owner-facing set-aside label (short), not a title blob."""
    access = elig.get("competition_access_type")
    if access and access not in {"UNKNOWN", "OPEN_MARKET"}:
        return str(access)
    parts = _structured_set_aside_parts(rec)
    if parts:
        # Prefer description-like longer field if present
        best = max(parts, key=len)
        return best[:80]
    if access:
        return str(access)
    return "UNKNOWN_VERIFY"


def classify_eligibility(rec: dict[str, Any]) -> dict[str, Any]:
    """Use company_eligibility + access gate. Unknown ≠ eligible."""
    text = _set_aside_text(rec)
    structured = _structured_set_aside_parts(rec)
    sa = set_aside_eligibility(text) if text else {"eligible": None, "reason": "missing_set_aside", "matched": None}
    access_type = classify_competition_access_type(rec, text=text or None)
    try:
        access = evaluate_phase_l_access(rec, text=text or None)
        our = access.get("our_bid_access")
    except Exception:
        our = ACCESS_UNKNOWN
        access = {}

    if sa.get("eligible") is False:
        return {
            "eligibility_bucket": "INELIGIBLE",
            "eligible": False,
            "reason": sa.get("reason"),
            "matched": sa.get("matched"),
            "set_aside_text": (text or "UNKNOWN")[:120],
            "competition_access_type": access_type,
            "our_bid_access": ACCESS_NO,
        }
    if access_type == SPECIAL_SET_ASIDE and sa.get("eligible") is not True:
        return {
            "eligibility_bucket": "INELIGIBLE" if sa.get("eligible") is False else "UNKNOWN_VERIFY",
            "eligible": sa.get("eligible"),
            "reason": sa.get("reason") or "special_set_aside",
            "matched": sa.get("matched"),
            "set_aside_text": (text or "UNKNOWN")[:120],
            "competition_access_type": access_type,
            "our_bid_access": our or ACCESS_UNKNOWN,
        }
    if our == ACCESS_NO:
        return {
            "eligibility_bucket": "INELIGIBLE",
            "eligible": False,
            "reason": "access_gate_no",
            "matched": None,
            "set_aside_text": (text or "UNKNOWN")[:120],
            "competition_access_type": access_type,
            "our_bid_access": our,
        }
    # Explicit allowed codes / unrestricted / total SB only — do not assume from title
    if sa.get("eligible") is True or access_type in {UNRESTRICTED, TOTAL_SMALL_BUSINESS}:
        return {
            "eligibility_bucket": "ELIGIBLE",
            "eligible": True,
            "reason": sa.get("reason") or "allowed_access",
            "matched": None,
            "set_aside_text": (text or access_type)[:120],
            "competition_access_type": access_type,
            "our_bid_access": our or ACCESS_YES,
        }
    if access_type == OPEN_MARKET and structured:
        # Structured open/none set-aside present
        lower = text.lower()
        if any(x in lower for x in ("no set aside", "none", "unrestricted", "no set-aside")):
            return {
                "eligibility_bucket": "ELIGIBLE",
                "eligible": True,
                "reason": "open_market_structured",
                "matched": None,
                "set_aside_text": text[:120],
                "competition_access_type": access_type,
                "our_bid_access": our or ACCESS_YES,
            }

    platform = str(rec.get("platform") or rec.get("source_platform") or "").lower()
    jurisdiction = str(rec.get("jurisdiction") or "").upper()
    is_federal = bool(rec.get("is_federal")) or jurisdiction == "FEDERAL" or "sam" in platform
    is_open_public_portal = (
        "bidnet" in platform
        or "opengov" in platform
        or "bonfire" in platform
        or "jaggaer" in platform
        or "socrata" in platform
        or jurisdiction in {"STATE", "LOCAL", "STATE_LOCAL"}
        or (not is_federal and platform.startswith("live_"))
    )
    # Public state/local open-bid portals rarely use federal set-asides — treat as open eligible
    if not structured and is_open_public_portal and not is_federal:
        return {
            "eligibility_bucket": "ELIGIBLE",
            "eligible": True,
            "reason": "open_public_portal",
            "matched": None,
            "set_aside_text": "OPEN_PUBLIC",
            "competition_access_type": OPEN_MARKET,
            "our_bid_access": our or ACCESS_YES,
        }

    # Federal missing/unrecognized set-aside → verify (still visible in Worth Researching)
    return {
        "eligibility_bucket": "UNKNOWN_VERIFY",
        "eligible": None,
        "reason": sa.get("reason") or ("missing_set_aside" if not structured else "unknown_set_aside"),
        "matched": sa.get("matched"),
        "set_aside_text": (text or "UNKNOWN_VERIFY")[:120],
        "competition_access_type": access_type if structured else "UNKNOWN",
        "our_bid_access": our or ACCESS_UNKNOWN,
    }


_COMMERCIAL_INDEX: dict[str, dict[str, Any]] | None = None


def _commercial_index() -> dict[str, dict[str, Any]]:
    """Load commercial overlays once (rebuild was O(n×files×deals) without this)."""
    global _COMMERCIAL_INDEX
    if _COMMERCIAL_INDEX is not None:
        return _COMMERCIAL_INDEX
    idx: dict[str, dict[str, Any]] = {}
    for name in (
        "m3_cex_v1_available_deals.json",
        "m3_rse_v1_available_deals.json",
        "m3_cgc_v1_available_deals.json",
        "m3_frds_v1_available_deals.json",
        "m3_source_product_v1_available_deals.json",
    ):
        path = Path(data_path(f"{OUT}/{name}"))
        if not path.exists():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for d in blob.get("deals") or []:
            oid = str(d.get("opportunity_id") or "")
            if oid and oid not in idx:
                idx[oid] = d
    _COMMERCIAL_INDEX = idx
    return idx


def _commercial_overlay(oid: str) -> dict[str, Any]:
    """Pull any existing commercial research without requiring it for visibility."""
    return _commercial_index().get(str(oid), {})


def _ready_to_bid(rec: dict[str, Any], elig: dict[str, Any], commercial: dict[str, Any]) -> bool:
    if elig.get("eligible") is not True:
        return False
    if _is_expired(rec):
        return False
    if not commercial:
        return False
    has_quote = bool(
        commercial.get("supplier_quote")
        or commercial.get("quotes")
        or str(commercial.get("supplier_status") or "").upper() in {"QUOTED", "QUOTE_ON_FILE"}
    )
    contrib = commercial.get("verified_remaining_contribution")
    try:
        positive = contrib is not None and float(str(contrib)) > 0
    except Exception:
        positive = False
    financing_ok = str(commercial.get("financing_status") or "").upper() in {
        "FINANCING_CONFIRMED",
        "SUPPLIER_TERMS_CONFIRMED",
        "NO_FINANCING_NEEDED",
    }
    days = _days_left(rec.get("deadline") or commercial.get("deadline"))
    runway = days is None or days >= 1
    mandatory_ok = bool(commercial.get("full_mandatory") or commercial.get("mandatory_coverage"))
    return bool(has_quote and positive and financing_ok and runway and mandatory_ok)


def research_priority_score(rec: dict[str, Any], elig: dict[str, Any], commercial: dict[str, Any]) -> int:
    """Discovery relevance — unknown profit does NOT zero this out."""
    score = 0
    title = str(rec.get("title") or "")
    days = _days_left(rec.get("deadline"))
    if elig.get("eligible") is True:
        score += 40
    elif elig.get("eligible") is None:
        score += 15  # unknown — still researchable
    else:
        score -= 100
    pc = _product_class(rec)
    if pc == "PRODUCT":
        score += 35
    elif pc == "MIXED":
        score += 10
    elif pc == "UNKNOWN_PRODUCTNESS":
        score += 5
    else:
        score -= 50
    if _COMMON_RE.search(title):
        score += 30
    if _SPECIALTY_RE.search(title):
        score -= 15
    if days is not None:
        if days >= 5:
            score += 25
        elif days >= 2:
            score += 10
        elif days >= 0:
            score += 3
        else:
            score -= 80
    else:
        # No deadline — keep visible but lower
        score += 2
    if commercial.get("public_acquisition_cost") or commercial.get("acquisition_prices"):
        score += 8
    if commercial.get("historical_unit") or commercial.get("historical_government_price"):
        score += 10
    # Do NOT subtract for missing pricing/financing
    funnel = str(rec.get("current_funnel_state") or "")
    if funnel in {"READY_TO_CALL", "ACCESSIBLE_PRODUCT", "DEEP_RESEARCH_COMPLETE"}:
        score += 12
    if funnel == "RAW":
        score += 5
    return score


def _next_action(bucket: str, rec: dict[str, Any], elig: dict[str, Any], commercial: dict[str, Any]) -> str:
    if bucket == BLOCKED:
        if elig.get("matched"):
            return f"Ineligible — {elig.get('matched')} set-aside"
        if _is_expired(rec):
            return "Expired — archive"
        return "Blocked — review reason"
    if bucket == NEEDS_VERIFICATION:
        if _deadline_status(rec) == "DEADLINE_CONFLICT":
            return "Resolve deadline conflict (cancel vs date)"
        return "Verify closing date from source documents"
    if bucket == READY:
        return "Prepare bid packet"
    if elig.get("eligible") is None:
        return "Verify set-aside eligibility"
    if not commercial.get("acquisition_prices") and not commercial.get("public_acquisition_cost"):
        return "Find acquisition price / request supplier quote"
    if not commercial.get("historical_unit") and not commercial.get("historical_government_price"):
        return "Recover buyer unit prices"
    if str(commercial.get("financing_status") or "").upper() in {"", "UNKNOWN", "FINANCING_UNKNOWN", "NONE"}:
        return "Assess financing / supplier terms"
    return "Continue commercial research"


def classify_record(oid: str, rec: dict[str, Any]) -> dict[str, Any]:
    elig = classify_eligibility(rec)
    pc = _product_class(rec)
    expired = _is_expired(rec)
    commercial = _commercial_overlay(oid)
    days = _days_left(rec.get("deadline"))
    dl_status = _deadline_status(rec)

    hide_reasons_avoided = []
    # Explicitly document that these do NOT block visibility
    if not commercial.get("public_acquisition_cost"):
        hide_reasons_avoided.append("MISSING_PRICING_STILL_VISIBLE")
    if str(commercial.get("financing_status") or "").upper() in {"", "UNKNOWN", "FINANCING_UNKNOWN", "NONE"}:
        hide_reasons_avoided.append("MISSING_FINANCING_STILL_VISIBLE")

    if expired:
        bucket = BLOCKED
        block_reason = "EXPIRED" if dl_status != "CANCELLED" else "CANCELLED"
    elif pc == "NON_PRODUCT":
        bucket = BLOCKED
        block_reason = "NON_PRODUCT"
    elif elig.get("eligible") is False:
        bucket = BLOCKED
        block_reason = elig.get("reason") or "INELIGIBLE_SET_ASIDE"
    elif _ready_to_bid(rec, elig, commercial):
        bucket = READY
        block_reason = None
    elif dl_status in {"DEADLINE_UNKNOWN", "DEADLINE_CONFLICT"}:
        # Missing/conflict deadline → Needs Verification (not assumed open, not expired)
        bucket = NEEDS_VERIFICATION
        block_reason = dl_status
    else:
        bucket = WORTH
        block_reason = None

    score = research_priority_score(rec, elig, commercial)
    if bucket == BLOCKED:
        score = min(score, 0)

    return {
        "opportunity_id": oid,
        "product": rec.get("title"),
        "buyer": rec.get("buyer"),
        "solicitation": rec.get("solicitation_event_id"),
        "deadline": rec.get("deadline"),
        "deadline_raw": rec.get("deadline_raw"),
        "deadline_timezone": rec.get("deadline_timezone"),
        "deadline_status": dl_status,
        "days_left": round(days, 2) if days is not None else None,
        "set_aside": _set_aside_label(rec, elig),
        "competition_access_type": elig.get("competition_access_type"),
        "eligibility_bucket": elig.get("eligibility_bucket"),
        "eligible": elig.get("eligible"),
        "eligibility_reason": elig.get("reason"),
        "eligibility_matched": elig.get("matched"),
        "product_class": pc,
        "product_service_classification": rec.get("product_service_classification"),
        "funnel_state": rec.get("current_funnel_state"),
        "visibility_bucket": bucket,
        "block_reason": block_reason,
        "research_priority_score": score,
        "commercial_readiness": "READY" if bucket == READY else ("HAS_PARTIAL_EVIDENCE" if commercial else "NEEDS_RESEARCH"),
        "pricing_status": (
            "KNOWN"
            if commercial.get("public_acquisition_cost") or commercial.get("acquisition_prices")
            else "UNKNOWN"
        ),
        "historical_pricing_status": (
            "KNOWN"
            if commercial.get("historical_unit") or commercial.get("historical_government_price")
            else "UNKNOWN"
        ),
        "financing_status": commercial.get("financing_status") or "UNKNOWN",
        "lines": commercial.get("mandatory_lines") or commercial.get("lines_recovered"),
        "missing_information": [
            x
            for x, ok in (
                ("acquisition_price", bool(commercial.get("public_acquisition_cost") or commercial.get("acquisition_prices"))),
                ("historical_gov_price", bool(commercial.get("historical_unit") or commercial.get("historical_government_price"))),
                ("supplier_quote", bool(commercial.get("supplier_quote") or commercial.get("quotes"))),
                ("financing", str(commercial.get("financing_status") or "").upper()
                not in {"", "UNKNOWN", "FINANCING_UNKNOWN", "NONE"}),
                ("deadline", rec.get("deadline") not in (None, "")),
            )
            if not ok
        ],
        "next_action": _next_action(bucket, rec, elig, commercial),
        "authoritative_url": rec.get("authoritative_url"),
        "common_product": bool(_COMMON_RE.search(str(rec.get("title") or ""))),
        "specialty_product": bool(_SPECIALTY_RE.search(str(rec.get("title") or ""))),
        "hide_reasons_avoided": hide_reasons_avoided,
        "filter_tags": [
            bucket.lower(),
            "worth_researching" if bucket == WORTH else bucket.lower(),
            "needs_verification" if bucket == NEEDS_VERIFICATION else bucket.lower(),
            "available" if bucket in {WORTH, READY} else bucket.lower(),
            "product_resale" if pc in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"} else "non_product",
        ]
        + (["common_commercial"] if _COMMON_RE.search(str(rec.get("title") or "")) else [])
        + (["needs_pricing"] if not (commercial.get("public_acquisition_cost") or commercial.get("acquisition_prices")) else [])
        + (["unknown_eligibility"] if elig.get("eligible") is None else [])
        + (["eligible"] if elig.get("eligible") is True else [])
        + (["open_confirmed"] if dl_status == "OPEN_CONFIRMED" else [])
        + (["blocked"] if bucket == BLOCKED else []),
        "lane": commercial.get("lane"),
        "contribution_state": commercial.get("contribution_state") or "CONTRIBUTION_UNKNOWN",
        "verified_remaining_contribution": commercial.get("verified_remaining_contribution"),
        "conditional_remaining_contribution": commercial.get("conditional_remaining_contribution"),
        "public_acquisition_cost": commercial.get("public_acquisition_cost"),
        "acquisition_prices": commercial.get("acquisition_prices"),
        "historical_unit": commercial.get("historical_unit") or commercial.get("historical_government_price"),
        "source_platform": rec.get("platform"),
        "updated_at": rec.get("updated_at"),
    }


def run_visibility_rebuild(*, prefer_open_deadline: bool = True) -> dict[str, Any]:
    global _COMMERCIAL_INDEX
    _COMMERCIAL_INDEX = None  # refresh commercial overlay index each rebuild
    store = _load_l23()
    opps = store.get("opportunities") or {}
    rows = []
    funnel_before = {
        "total_live_records": len(opps),
        "by_funnel": dict(Counter(str(r.get("current_funnel_state") or "NONE") for r in opps.values())),
        "by_product_class_raw": dict(
            Counter(str(r.get("product_service_classification") or "NONE") for r in opps.values())
        ),
        "eligible_for_profit_research_true": sum(1 for r in opps.values() if r.get("eligible_for_profit_research") is True),
        "eligible_for_profit_research_false": sum(1 for r in opps.values() if r.get("eligible_for_profit_research") is False),
        "commercial_feed_deals_before": _count_commercial_feed(),
    }

    for oid, rec in opps.items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("current_funnel_state") or "") in {"FAST_REJECT"} and _product_class(rec) == "NON_PRODUCT":
            # still classify into blocked for transparency
            pass
        rows.append(classify_record(oid, rec))

    worth = [r for r in rows if r["visibility_bucket"] == WORTH]
    ready = [r for r in rows if r["visibility_bucket"] == READY]
    needs = [r for r in rows if r["visibility_bucket"] == NEEDS_VERIFICATION]
    blocked = [r for r in rows if r["visibility_bucket"] == BLOCKED]

    # Primary shortlist: confirmed-open first; prefer Quote runway (>=5 days) then sooner deadlines
    def sort_key(r: dict[str, Any]) -> tuple:
        days = r.get("days_left")
        if days is None:
            runway = 2
        elif days >= 5:
            runway = 0
        elif days >= 0:
            runway = 1
        else:
            runway = 3
        return (runway, days if days is not None else 9999, -int(r.get("research_priority_score") or 0))

    worth.sort(key=sort_key)
    ready.sort(key=sort_key)
    needs.sort(key=lambda r: (-int(r.get("research_priority_score") or 0), str(r.get("product") or "")))

    # Previously hidden by commercial-only UI (proxy): profit-research false OR not in commercial feed
    commercial_ids = _commercial_feed_ids()
    previously_hidden_pricing = sum(
        1
        for r in worth
        if r["opportunity_id"] not in commercial_ids and "MISSING_PRICING_STILL_VISIBLE" in (r.get("hide_reasons_avoided") or [])
    )
    previously_hidden_financing = sum(
        1
        for r in worth
        if r["opportunity_id"] not in commercial_ids and "MISSING_FINANCING_STILL_VISIBLE" in (r.get("hide_reasons_avoided") or [])
    )

    deals_out = {
        "kind": "adv_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "default_filter": "worth_researching",
        "counts": {
            "worth_researching": len(worth),
            "ready_to_bid": len(ready),
            "needs_verification": len(needs),
            "blocked_ineligible": len(blocked),
            "unknown_eligibility": sum(1 for r in worth + needs if r.get("eligible") is None),
            "eligible": sum(1 for r in worth if r.get("eligible") is True),
            "product_opportunities": sum(1 for r in rows if r.get("product_class") in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}),
            "open_deadline": sum(1 for r in worth if r.get("days_left") is not None and r["days_left"] >= 0),
            "open_confirmed": sum(1 for r in rows if r.get("deadline_status") == "OPEN_CONFIRMED"),
            "deadline_unknown": sum(1 for r in rows if r.get("deadline_status") == "DEADLINE_UNKNOWN"),
        },
        "deals": worth + ready,  # default: confirmed-open worth researching
        "needs_verification_deals": needs[:2000],
        # Prefer set-aside blockers first so HUBZone/8(a) audits are not truncated away
        "blocked_deals": sorted(
            blocked,
            key=lambda r: (
                0 if r.get("block_reason") not in {None, "EXPIRED", "NON_PRODUCT", "CANCELLED"} else 1,
                str(r.get("deadline") or "9999"),
            ),
        )[:1500],
        "NOTE": "Default Worth Researching = confirmed-open product opportunities. Missing deadlines → Needs Verification.",
    }
    _save(DEALS, deals_out)

    # Sync primary feed so revenue_strategy path also sees worth-researching
    sync_payload = {
        "kind": "cex_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(worth) + len(ready),
        "deals": [
            {
                **d,
                "filter_tags": list(
                    set(
                        (d.get("filter_tags") or [])
                        + ["revenue_strategy", "worth_researching", "available", "all_viable"]
                    )
                ),
                "next_action": d.get("next_action"),
            }
            for d in (worth[:400] + ready)
        ],
        "visibility_counts": deals_out["counts"],
        "NOTE": "Synced from available_deals_visibility_v1 — unknown pricing still listed",
    }
    for feed in (
        f"{OUT}/m3_cex_v1_available_deals.json",
        f"{OUT}/m3_rse_v1_available_deals.json",
        f"{OUT}/m3_source_product_v1_available_deals.json",
    ):
        _save(feed, sync_payload)

    funnel_after = {
        **funnel_before,
        "worth_researching": len(worth),
        "ready_to_bid": len(ready),
        "blocked_ineligible": len(blocked),
        "unknown_eligibility": deals_out["counts"]["unknown_eligibility"],
        "previously_hidden_missing_pricing_proxy": previously_hidden_pricing,
        "previously_hidden_missing_financing_proxy": previously_hidden_financing,
        "recovered_into_worth_researching": len(worth),
        "commercial_feed_deals_after": len(sync_payload["deals"]),
    }
    _save(FUNNEL, {"build": BUILD, "at": _utc(), "before": funnel_before, "after": funnel_after})

    report = {
        "kind": "m3_available_deals_visibility_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "funnel": funnel_after,
        "counts": deals_out["counts"],
        "top_worth_researching": worth[:20],
        "top_ready_to_bid": ready[:20],
        "blocked_sample": blocked[:20],
        "repairs": [
            "Available Deals no longer limited to commercial-research projections for visibility",
            "Unknown pricing/financing do not hide eligible product opportunities",
            "HUBZone/8(a)/SDVOSB/WOSB-only routed to BLOCKED via company_eligibility",
            "UI default worth_researching",
        ],
    }
    _save(REPORT, report)
    return report


def _count_commercial_feed() -> int:
    path = Path(data_path(f"{OUT}/m3_cex_v1_available_deals.json"))
    if not path.exists():
        return 0
    try:
        return len((json.loads(path.read_text(encoding="utf-8")).get("deals") or []))
    except Exception:
        return 0


def _commercial_feed_ids() -> set[str]:
    path = Path(data_path(f"{OUT}/m3_cex_v1_available_deals.json"))
    if not path.exists():
        return set()
    try:
        return {str(d.get("opportunity_id")) for d in (json.loads(path.read_text(encoding="utf-8")).get("deals") or [])}
    except Exception:
        return set()


def baseline_lookup_solicitations(needles: list[str]) -> dict[str, Any]:
    """Search local+known stores for solicitations WITHOUT injecting."""
    store = _load_l23()
    opps = store.get("opportunities") or {}
    results = {}
    for needle in needles:
        norm = re.sub(r"[^A-Z0-9]", "", needle.upper())
        hits = []
        for oid, rec in opps.items():
            blob = " ".join(
                str(x or "")
                for x in (
                    oid,
                    rec.get("solicitation_event_id"),
                    rec.get("title"),
                    rec.get("authoritative_url"),
                    json.dumps(rec.get("row_ref") or {})[:2000],
                )
            )
            if needle.upper() in blob.upper() or norm in re.sub(r"[^A-Z0-9]", "", blob.upper()):
                hits.append(
                    {
                        "opportunity_id": oid,
                        "solicitation_event_id": rec.get("solicitation_event_id"),
                        "title": rec.get("title"),
                        "buyer": rec.get("buyer"),
                        "deadline": rec.get("deadline"),
                        "funnel": rec.get("current_funnel_state"),
                        "product_service_classification": rec.get("product_service_classification"),
                        "updated_at": rec.get("updated_at"),
                        "platform": rec.get("platform"),
                    }
                )
        results[needle] = {
            "discovered": bool(hits),
            "hit_count": len(hits),
            "hits": hits[:10],
            "NOTE": "Baseline before any injection",
        }
    out = {"build": BUILD, "at": _utc(), "results": results}
    _save(BASELINE, out)
    return out
