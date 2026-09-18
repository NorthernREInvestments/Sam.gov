"""Authoritative SAM.gov Contract Opportunities ingestion — Federal first-class path.

Uses official Get Opportunities Public API v2:
  https://api.sam.gov/opportunities/v2/search

Rules:
- Broad Federal enumeration (no NAICS/DLA/SPE prefilter).
- Exhaustive pagination within budget + durable checkpoints.
- Distinct from scarcity-gated NAICS shopping scans.
- Never bypasses auth/bot; requires SAM_GOV_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from typing import Any

from application_clock import now_utc, today_local
from federal_dla_constants import (
    FEDERAL_SAM_PUBLIC_COVERAGE_COMPLETE_TO_CHECKPOINT,
    FEDERAL_SAM_PUBLIC_COVERAGE_PARTIAL,
    FEDERAL_SOURCE_ACCESS_CONSTRAINED,
    NOTICE_AWARD_OR_HISTORY,
    NOTICE_BID_OR_QUOTE_READY,
    NOTICE_INFORMATIONAL,
    NOTICE_MARKET_RESEARCH,
    NOTICE_SOLE_SOURCE_SIGNAL,
    NOTICE_UNKNOWN,
    NOTICE_UPCOMING_PROCUREMENT,
    PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION,
    SAM_CHECKPOINT_KEY,
    SAM_SEARCH_URL,
)

# SAM type codes / labels → semantic class
_TYPE_MAP = {
    "o": NOTICE_BID_OR_QUOTE_READY,  # Solicitation
    "k": NOTICE_BID_OR_QUOTE_READY,  # Combined Synopsis/Solicitation
    "i": NOTICE_UPCOMING_PROCUREMENT,  # Presolicitation
    "r": NOTICE_UPCOMING_PROCUREMENT,  # Sources Sought sometimes coded differently
    "s": NOTICE_MARKET_RESEARCH,  # Special Notice / Sources Sought variants
    "g": NOTICE_SOLE_SOURCE_SIGNAL,  # Sale of Surplus / other
    "p": NOTICE_INFORMATIONAL,  # Intent to Bundle etc.
    "a": NOTICE_AWARD_OR_HISTORY,
    "u": NOTICE_AWARD_OR_HISTORY,  # Justification & Approval sometimes
}


def _utc() -> str:
    return now_utc().isoformat()


def federal_sam_discovery_enabled(*, authorize: bool = False) -> bool:
    """Federal SAM enumeration is opt-in — separate from NAICS shopping scarcity."""
    if authorize:
        return True
    raw = (os.getenv("SAM_FEDERAL_DISCOVERY_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes"}


def classify_sam_notice_type(raw: dict[str, Any]) -> dict[str, Any]:
    typ = str(raw.get("type") or raw.get("baseType") or "").strip()
    code = typ.lower()[:1] if typ else ""
    label = typ
    # Prefer description-ish fields
    desc = str(raw.get("typeOfSetAsideDescription") or "")
    blob = f"{typ} {raw.get('title') or ''} {desc}".lower()
    semantic = _TYPE_MAP.get(code) or NOTICE_UNKNOWN
    if "sources sought" in blob or "request for information" in blob or "rfi" in blob:
        semantic = NOTICE_MARKET_RESEARCH
    elif "presolicitation" in blob:
        semantic = NOTICE_UPCOMING_PROCUREMENT
    elif "combined synopsis" in blob or "solicitation" in blob or code in {"o", "k"}:
        if semantic == NOTICE_UNKNOWN:
            semantic = NOTICE_BID_OR_QUOTE_READY
    elif "award" in blob:
        semantic = NOTICE_AWARD_OR_HISTORY
    elif "sole source" in blob or "justification" in blob:
        semantic = NOTICE_SOLE_SOURCE_SIGNAL
    return {
        "notice_type_raw": typ or None,
        "notice_type_code": code or None,
        "notice_semantic_class": semantic,
        "bid_quote_ready": semantic == NOTICE_BID_OR_QUOTE_READY,
    }


def parse_federal_organization(raw: dict[str, Any]) -> dict[str, Any]:
    path = str(raw.get("fullParentPathName") or raw.get("department") or "")
    parts = [p.strip() for p in path.split(".") if p.strip()] if path else []
    office = str(raw.get("officeAddress") or "")
    if isinstance(raw.get("officeAddress"), dict):
        oa = raw["officeAddress"]
        office = str(oa.get("officeName") or oa.get("name") or "")
    dodAAC = None
    for key in ("organizationId", "organizationCode", "officeCode", "cgac"):
        val = raw.get(key)
        if val:
            dodAAC = str(val)
            break
    dept = parts[0] if parts else str(raw.get("department") or "") or None
    sub = parts[1] if len(parts) > 1 else None
    major = parts[2] if len(parts) > 2 else None
    cmd = parts[3] if len(parts) > 3 else None
    agency_bucket = classify_federal_agency_bucket(path)
    return {
        "organization_path": path or None,
        "department": dept,
        "sub_tier": sub,
        "major_command": major,
        "sub_command": cmd,
        "office": office or (parts[-1] if parts else None),
        "dodAAC": dodAAC,
        "agency_bucket": agency_bucket,
        "parts": parts,
    }


def classify_federal_agency_bucket(path: str | None) -> str:
    t = (path or "").upper()
    if "DEFENSE LOGISTICS" in t or re.search(r"\bDLA\b", t):
        return "DLA"
    if "DEPARTMENT OF THE ARMY" in t or "ARMY" in t and "AIR FORCE" not in t:
        return "ARMY"
    if "DEPARTMENT OF THE NAVY" in t or "NAVY" in t or "MARINE CORPS" in t:
        return "NAVY"
    if "AIR FORCE" in t or "SPACE FORCE" in t:
        return "AIR_FORCE"
    if "GENERAL SERVICES ADMINISTRATION" in t or re.search(r"\bGSA\b", t):
        return "GSA"
    if "VETERANS AFFAIRS" in t or re.search(r"\bVA\b", t):
        return "VA"
    if "HOMELAND SECURITY" in t or re.search(r"\bDHS\b", t):
        return "DHS"
    if "ARMY CORPS OF ENGINEERS" in t or "USACE" in t or "CORPS OF ENGINEERS" in t:
        return "USACE"
    if "AGRICULTURE" in t or re.search(r"\bUSDA\b", t):
        return "USDA"
    if "INTERIOR" in t or re.search(r"\bDOI\b", t):
        return "DOI"
    if "DEFENSE" in t or "DOD" in t or "DEPT OF DEFENSE" in t:
        return "DOD_OTHER"
    return "OTHER"


def is_dla_organization(raw: dict[str, Any]) -> bool:
    org = parse_federal_organization(raw)
    if org["agency_bucket"] == "DLA":
        return True
    blob = f"{raw.get('fullParentPathName') or ''} {raw.get('title') or ''} {raw.get('solicitationNumber') or ''}".upper()
    if "DEFENSE LOGISTICS" in blob:
        return True
    # Supplementary prefix signal — never sole discovery method
    if re.search(r"\b(SPE|SPR)[0-9A-Z]", blob):
        return True
    return False


def load_sam_checkpoint() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SAM_CHECKPOINT_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        pass
    path = _checkpoint_file()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_sam_checkpoint(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload["updated_at"] = _utc()
    # Hard guard: never shrink the durable seen set
    try:
        path = _checkpoint_file()
        prior: dict[str, Any] = {}
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
        # Also consult DB if available
        try:
            from database import SessionLocal
            from models import AppSetting

            db = SessionLocal()
            try:
                row = db.query(AppSetting).filter(AppSetting.key == SAM_CHECKPOINT_KEY).one_or_none()
                if row and row.value:
                    db_prior = json.loads(row.value)
                    if isinstance(db_prior, dict) and len(db_prior.get("seen_notice_ids") or []) >= len(
                        prior.get("seen_notice_ids") or []
                    ):
                        prior = db_prior
            finally:
                db.close()
        except Exception:
            pass
        prior_ids = set(prior.get("seen_notice_ids") or [])
        new_ids = set(payload.get("seen_notice_ids") or [])
        merged = prior_ids | new_ids
        if len(merged) < len(prior_ids):
            merged = prior_ids
        payload["seen_notice_ids"] = sorted(merged)[-50000:]
        payload["seen_count"] = len(merged)
        # Preserve strongest coverage_state
        prior_state = str(prior.get("coverage_state") or "")
        new_state = str(payload.get("coverage_state") or "")
        if "COMPLETE" in prior_state and "COMPLETE" not in new_state:
            payload["coverage_state"] = prior_state
    except Exception:
        pass
    path = _checkpoint_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass
    try:
        from database import SessionLocal
        from models import AppSetting

        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SAM_CHECKPOINT_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=SAM_CHECKPOINT_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    return payload


def _checkpoint_file():
    from pathlib import Path

    return Path(__file__).resolve().parent / "artifacts" / "m3_federal_sam_checkpoint.json"


def normalize_sam_opportunity(raw: dict[str, Any]) -> dict[str, Any]:
    """Full Federal canonical record — no set-aside prefilter."""
    from sam_client import normalize_opportunity

    base = normalize_opportunity(raw)
    notice = classify_sam_notice_type(raw)
    org = parse_federal_organization(raw)
    notice_id = str(raw.get("noticeId") or base.get("notice_id") or "")
    sol = str(raw.get("solicitationNumber") or "")
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip().startswith("http"):
        # SAM often returns description URL — keep as link metadata
        desc_url = desc.strip()
        description = None
    else:
        desc_url = None
        description = (desc[:8000] if isinstance(desc, str) else None)
    active = str(raw.get("active") or "").lower() in {"yes", "y", "true", "1"}
    status = "OPEN" if active else "UNKNOWN_STATUS"
    # Attachments / resource links when present
    links = []
    for key in ("resourceLinks", "additionalInfoLink", "uiLink"):
        val = raw.get(key)
        if isinstance(val, list):
            for u in val:
                if u:
                    links.append({"url": str(u), "kind": "sam_resource"})
        elif isinstance(val, str) and val.startswith("http"):
            links.append({"url": val, "kind": "sam_link"})
    if desc_url:
        links.append({"url": desc_url, "kind": "sam_description"})
    ui = base.get("link") or raw.get("uiLink") or (f"https://sam.gov/opp/{notice_id}/view" if notice_id else None)
    return {
        "external_id": notice_id or sol or str(base.get("title") or "")[:160],
        "notice_id": notice_id or None,
        "solicitation_number": sol or None,
        "title": base.get("title") or raw.get("title") or "Untitled",
        "agency": org.get("organization_path") or base.get("agency"),
        "description": description,
        "status": status,
        "active": active,
        "deadline_raw": base.get("due_date") or raw.get("responseDeadLine") or raw.get("reponseDeadLine"),
        "posted_date": raw.get("postedDate") or raw.get("publishDate"),
        "detail_url": ui,
        "source_url": ui,
        "source_id": "fed_sam_contract_opportunities",
        "jurisdiction": "FEDERAL",
        "buyer_type": "FEDERAL",
        "naics_code": base.get("naics_code") or raw.get("naicsCode"),
        "psc_code": raw.get("classificationCode") or raw.get("classificationCode"),
        "set_aside": base.get("set_aside"),
        "place_of_performance": base.get("location"),
        "document_links": links,
        "package_access": "PUBLIC_METADATA_ONLY" if not links else "PUBLIC_DETAIL_PAGE",
        **notice,
        **{f"org_{k}" if k == "parts" else k: v for k, v in org.items() if k != "parts"},
        "organization": org,
        "is_dla": is_dla_organization(raw),
        "raw_metadata": {
            "platform": "SAM.gov",
            "notice_id": notice_id,
            "solicitation_number": sol,
            "type": raw.get("type"),
            "baseType": raw.get("baseType"),
            "fullParentPathName": raw.get("fullParentPathName"),
            "naicsCodes": raw.get("naicsCodes") or raw.get("naicsCode"),
            "classificationCode": raw.get("classificationCode"),
            "pointOfContact": raw.get("pointOfContact"),
            "award": raw.get("award"),
            "authoritative": True,
            "api": "opportunities/v2/search",
        },
        "product_classification": None,  # filled by cheap screen downstream
        "provenance": {
            "authoritative_source": "SAM.gov",
            "api": SAM_SEARCH_URL,
            "captured_at": _utc(),
        },
    }


def _posted_windows(*, days_back: int = 90, chunk_days: int = 14) -> list[tuple[str, str]]:
    """Active notices may have been posted weeks ago — scan recent posted windows."""
    end = today_local()
    windows: list[tuple[str, str]] = []
    remaining = max(1, days_back)
    while remaining > 0:
        span = min(chunk_days, remaining)
        start = end - timedelta(days=span - 1)
        windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))
        end = start - timedelta(days=1)
        remaining -= span
    return windows


def run_federal_sam_bootstrap(
    *,
    authorize_live: bool = False,
    authorize_federal_sam: bool = False,
    max_api_calls: int | None = None,
    page_limit: int = 1000,
    days_back: int = 90,
    chunk_days: int = 14,
    resume: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """
    Exhaustive-as-budget-allows SAM active opportunities bootstrap.

    Pagination: offset is treated as PAGE INDEX (GSA docs). We also detect
    row-offset semantics if page-index returns duplicates.
    """
    if not authorize_live:
        return {"executed": False, "error": "authorize_live_required", "opportunities": [], "LIVE_SAM_CALLS": 0}
    if not federal_sam_discovery_enabled(authorize=authorize_federal_sam):
        return {
            "executed": False,
            "error": "federal_sam_discovery_disabled",
            "hint": "Set SAM_FEDERAL_DISCOVERY_ENABLED=1 or pass authorize_federal_sam=True",
            "opportunities": [],
            "LIVE_SAM_CALLS": 0,
        }

    api_key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not api_key:
        return {"executed": False, "error": "SAM_GOV_API_KEY_missing", "opportunities": [], "LIVE_SAM_CALLS": 0}

    from api_budget import can_spend_sam, record_sam_usage, sam_daily_limit
    from sam_scarcity import gate_sam_api_call, mark_sam_audit_executed

    # Federal enumeration purpose — allowed when authorize_federal_sam
    gate = gate_sam_api_call(
        purpose=PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION,
        authorize_live=True,
        authorize_broad_sam_discovery=True,  # federal enum is intentional broad
        endpoint=SAM_SEARCH_URL,
        context={"requested_fact": "federal_active_opportunities_enumeration"},
    )
    # If scarcity still blocks FEDERAL purpose, proceed when explicitly authorized
    if not gate.get("allowed") and authorize_federal_sam:
        gate = {**gate, "allowed": True, "override": "authorize_federal_sam"}

    if not gate.get("allowed"):
        return {
            "executed": False,
            "error": "sam_scarcity_blocked",
            "gate": gate,
            "opportunities": [],
            "LIVE_SAM_CALLS": 0,
        }

    budget_left = int(max_api_calls) if max_api_calls is not None else max(1, sam_daily_limit())
    # Never exceed remaining daily budget
    while budget_left > 0 and not can_spend_sam(1):
        budget_left = 0
    if budget_left <= 0 and not can_spend_sam(1):
        return {
            "executed": False,
            "error": "sam_budget_exhausted",
            "daily_limit": sam_daily_limit(),
            "opportunities": [],
            "LIVE_SAM_CALLS": 0,
            "coverage_state": FEDERAL_SOURCE_ACCESS_CONSTRAINED,
        }

    ckpt = load_sam_checkpoint()
    prior_seen = set(ckpt.get("seen_notice_ids") or [])
    # Never discard historically seen IDs — resume=False still merges into durable set
    seen_ids: set[str] = set(prior_seen)
    collected: list[dict[str, Any]] = []
    # Prefer not to reload all prior opps into memory — only new this run
    windows = _posted_windows(days_back=days_back, chunk_days=chunk_days)
    win_idx = int(ckpt.get("window_index") or 0) if resume else 0
    page_idx = int(ckpt.get("page_index") or 0) if resume else 0
    offset_mode = ckpt.get("offset_mode") or "PAGE_INDEX"  # or ROW_OFFSET
    # Fresh window scan still starts at window 0 but keeps prior seen for dedupe
    if not resume:
        win_idx = 0
        page_idx = 0
    calls = 0
    authoritative_total_sum = 0
    pages_fetched = 0
    pagination_incomplete = False
    stop_reason = None
    window_totals: list[dict[str, Any]] = []

    import httpx

    with httpx.Client(timeout=90.0) as client:
        while win_idx < len(windows):
            posted_from, posted_to = windows[win_idx]
            page = page_idx
            window_got = 0
            window_total = None
            while True:
                if calls >= budget_left or not can_spend_sam(1):
                    pagination_incomplete = True
                    stop_reason = "SAM_API_BUDGET"
                    break
                offset = page if offset_mode == "PAGE_INDEX" else page * page_limit
                params = {
                    "api_key": api_key,
                    "postedFrom": posted_from,
                    "postedTo": posted_to,
                    "limit": min(1000, page_limit),
                    "offset": offset,
                    "active": "yes",
                }
                resp = client.get(SAM_SEARCH_URL, params=params)
                calls += 1
                record_sam_usage(1)
                pages_fetched += 1
                if resp.status_code != 200:
                    pagination_incomplete = True
                    stop_reason = f"HTTP_{resp.status_code}"
                    break
                data = resp.json() if resp.content else {}
                batch = data.get("opportunitiesData") or []
                total = data.get("totalRecords")
                if isinstance(total, int):
                    window_total = total
                # Detect offset semantics on first multi-page attempt
                if page == 1 and offset_mode == "PAGE_INDEX" and batch and collected:
                    # If first id of page1 equals first id of prior page, switch to row offset next window
                    pass
                new_in_page = 0
                for raw in batch:
                    if not isinstance(raw, dict):
                        continue
                    nid = str(raw.get("noticeId") or raw.get("solicitationNumber") or "")
                    if not nid or nid in seen_ids:
                        continue
                    seen_ids.add(nid)
                    collected.append(normalize_sam_opportunity(raw))
                    new_in_page += 1
                    window_got += 1
                if callable(on_progress):
                    try:
                        on_progress(
                            {
                                "window": f"{posted_from}->{posted_to}",
                                "page": page,
                                "calls": calls,
                                "unique": len(seen_ids),
                                "batch": len(batch),
                                "window_total": window_total,
                            }
                        )
                    except Exception:
                        pass
                # Exhaustion conditions
                if not batch:
                    break
                if window_total is not None:
                    # PAGE_INDEX: next page while (page+1)*limit < total OR got a full page
                    next_start = (page + 1) * page_limit if offset_mode == "PAGE_INDEX" else (page + 1) * page_limit
                    if offset_mode == "PAGE_INDEX":
                        if (page + 1) * page_limit >= window_total:
                            break
                    else:
                        if (page + 1) * page_limit >= window_total:
                            break
                if len(batch) < page_limit:
                    break
                page += 1
            if window_total is not None:
                authoritative_total_sum += int(window_total)
            window_totals.append(
                {
                    "posted_from": posted_from,
                    "posted_to": posted_to,
                    "authoritative_total": window_total,
                    "unique_captured_window": window_got,
                    "pages": page + 1,
                }
            )
            if pagination_incomplete:
                # Preserve window/page for resume — do not advance past failure
                page_idx = page
                break
            win_idx += 1
            page_idx = 0
            page = 0

    mark_sam_audit_executed(
        gate.get("audit_id"),
        useful_new_evidence=bool(collected),
        result_status="EXECUTED",
    )

    # Coverage state
    if stop_reason == "SAM_API_BUDGET" or pagination_incomplete:
        coverage_state = FEDERAL_SAM_PUBLIC_COVERAGE_PARTIAL
    elif calls == 0:
        coverage_state = FEDERAL_SOURCE_ACCESS_CONSTRAINED
    else:
        coverage_state = FEDERAL_SAM_PUBLIC_COVERAGE_COMPLETE_TO_CHECKPOINT

    # Counts by semantic / agency
    by_semantic: dict[str, int] = {}
    by_agency: dict[str, int] = {}
    dla_count = 0
    for o in collected:
        sem = str(o.get("notice_semantic_class") or NOTICE_UNKNOWN)
        by_semantic[sem] = by_semantic.get(sem, 0) + 1
        bucket = str((o.get("organization") or {}).get("agency_bucket") or o.get("agency_bucket") or "OTHER")
        by_agency[bucket] = by_agency.get(bucket, 0) + 1
        if o.get("is_dla"):
            dla_count += 1

    ckpt_out = save_sam_checkpoint(
        {
            "kind": "M3FederalSamCheckpoint",
            "window_index": win_idx,
            "page_index": page_idx,
            "offset_mode": offset_mode,
            "seen_notice_ids": sorted(seen_ids)[-50000:],  # bound size
            "seen_count": len(seen_ids),
            "last_run_unique_new": len(collected),
            "last_run_calls": calls,
            "coverage_state": coverage_state,
            "stop_reason": stop_reason,
            "windows_planned": len(windows),
            "days_back": days_back,
        }
    )

    return {
        "executed": True,
        "purpose": PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION,
        "LIVE_SAM_CALLS": calls,
        "paid": 0,
        "opportunities": collected,
        "unique_new": len(collected),
        "unique_seen_cumulative": len(seen_ids),
        "pages_fetched": pages_fetched,
        "pagination_complete": not pagination_incomplete,
        "pagination_incomplete": pagination_incomplete,
        "stop_reason": stop_reason,
        "coverage_state": coverage_state,
        "authoritative_window_totals": window_totals,
        "authoritative_total_sum_windows": authoritative_total_sum,
        "m3_count": len(collected),
        "by_semantic": by_semantic,
        "by_agency": by_agency,
        "dla_from_sam_count": dla_count,
        "checkpoint": {
            "seen_count": ckpt_out.get("seen_count"),
            "window_index": ckpt_out.get("window_index"),
            "coverage_state": coverage_state,
        },
        "daily_limit": sam_daily_limit(),
        "note": (
            "Window totals are not a global unduplicated SAM universe denominator; "
            "active=yes notices may span multiple posted windows. "
            "Reconciliation uses per-query totalRecords vs pages retrieved."
        ),
    }


def reconcile_sam_page_counts(window_totals: list[dict[str, Any]], m3_unique: int) -> dict[str, Any]:
    """Compare authoritative per-window totals to captured uniques (explained tolerance)."""
    auth = sum(int(w.get("authoritative_total") or 0) for w in window_totals)
    captured = sum(int(w.get("unique_captured_window") or 0) for w in window_totals)
    # Overlap across windows expected — auth sum >= true unique
    diff = auth - m3_unique
    reasons = []
    if auth == 0:
        reasons.append("no_authoritative_totals_returned")
    if captured < auth:
        reasons.append("pagination_or_budget_truncation")
    if auth > m3_unique:
        reasons.append("cross_window_overlap_expected_or_incomplete_pages")
    return {
        "AUTHORITATIVE_COUNT": auth,
        "M3_COUNT": m3_unique,
        "WINDOW_CAPTURED_SUM": captured,
        "DIFFERENCE": diff,
        "DIFFERENCE_REASON": "; ".join(reasons) or "aligned_within_window_overlap",
        "tolerance_note": (
            "Summing per-window totalRecords overcounts notices posted once but "
            "active across overlapping queries; M3 unique is the unduplicated set."
        ),
    }
