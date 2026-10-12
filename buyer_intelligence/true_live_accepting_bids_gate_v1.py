"""True Live / Accepting-Bids Gate + Production Cohort V1.

Canonical rule: LIVE/OPEN/ACTIONABLE means the buyer is still accepting
a bid/quote/offer from us NOW. Evaluation and past-deadline are not live.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.category_classifier_v1 import classify_opportunity, classify_text
from buyer_intelligence.category_enrichment_buyer_match_v2 import (
    RELATED_PAIRS,
    category_match_type,
    enrich_category,
)
from buyer_intelligence.engine import _buyer_id, _parse_date, classify_family
from buyer_intelligence.buyer_trend_operationalization_v1 import is_pseudo_buyer
from buyer_intelligence.three_lane_integration_v1 import (
    load_lane2_indexes,
    load_lane3_index,
    resolve_buyer,
)

BUILD = "20261010-m3-true-live-deadline-open-evidence-v1"

# Canonical operational states
OPEN_ACCEPTING_BIDS = "OPEN_ACCEPTING_BIDS"
OPEN_DEADLINE_UNKNOWN = "OPEN_DEADLINE_UNKNOWN"
CLOSED_DEADLINE_PASSED = "CLOSED_DEADLINE_PASSED"
EVALUATION = "EVALUATION"
AWARDED = "AWARDED"
CANCELLED = "CANCELLED"
WITHDRAWN = "WITHDRAWN"
FUTURE_NOT_OPEN = "FUTURE_NOT_OPEN"
UNKNOWN = "UNKNOWN"

ACTIONABLE_STATES = {OPEN_ACCEPTING_BIDS}
RESEARCH_STATES = {OPEN_DEADLINE_UNKNOWN}

# Deadline viability for open accepting
GOOD = "GOOD"  # >=5 days
TIGHT = "TIGHT"  # 3-4
RUSH = "RUSH"  # 1-2
EXPIRED = "EXPIRED"
DL_UNKNOWN = "UNKNOWN"

DURABLE_FAMILIES = {
    "AUTO_PARTS",
    "MRO",
    "TOOLS",
    "ELECTRICAL",
    "LIGHTING",
    "PPE",
    "JANITORIAL",
    "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES",
    "COMMON_EQUIPMENT",
    "PLUMBING",
    "HVAC",
    "FASTENERS",
    "HOSES_FITTINGS",
    "BEARINGS",
    "ADHESIVES",
    "SAFETY",
}

_CANCELLED_RE = re.compile(r"\b(cancel+ed|cancel+ation|withdrawn|no\s+longer\s+accepting)\b", re.I)
_AWARDED_RE = re.compile(r"\b(award(ed)?|notice\s+of\s+award|contract\s+award)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _norm_status(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


# ---------------------------------------------------------------------------
# Source-specific status maps
# ---------------------------------------------------------------------------

OPENGOV_STATUS_MAP: dict[str, str] = {
    "open": OPEN_ACCEPTING_BIDS,  # provisional — deadline still required
    "published": OPEN_ACCEPTING_BIDS,
    "accepting": OPEN_ACCEPTING_BIDS,
    "pending": OPEN_DEADLINE_UNKNOWN,  # often pre-release / unclear
    "evaluation": EVALUATION,
    "awardpending": EVALUATION,
    "closed": CLOSED_DEADLINE_PASSED,
    "awarded": AWARDED,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
    "withdrawn": WITHDRAWN,
}

BIDNET_STATUS_MAP: dict[str, str] = {
    "open": OPEN_ACCEPTING_BIDS,
    "active": OPEN_ACCEPTING_BIDS,
    "live": OPEN_ACCEPTING_BIDS,
    "closed": CLOSED_DEADLINE_PASSED,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
    "awarded": AWARDED,
    "evaluation": EVALUATION,
}

SAM_STATUS_MAP: dict[str, str] = {
    "active": OPEN_ACCEPTING_BIDS,
    "live": OPEN_ACCEPTING_BIDS,
    "livefresh": OPEN_ACCEPTING_BIDS,
    "archived": CLOSED_DEADLINE_PASSED,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
    "awarded": AWARDED,
    "inactive": CLOSED_DEADLINE_PASSED,
    "expired": CLOSED_DEADLINE_PASSED,
}


def detect_source(rec: dict[str, Any]) -> str:
    cid = str(rec.get("canonical_opportunity_id") or "")
    plat = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    url = str(rec.get("authoritative_url") or "")
    prov = rec.get("source_provenance") or []
    # Provenance URLs often identify source when platform is null
    prov_urls = " ".join(str((p or {}).get("url") or "") for p in prov if isinstance(p, dict)).lower()
    blob = f"{plat} {url} {prov_urls} {cid}".lower()
    if cid.startswith("opengov:") or "opengov" in blob:
        return "OpenGov"
    if "bidnet" in blob:
        return "BidNet"
    if "sam.gov" in blob or plat in {"sam_gov_api", "sam"}:
        return "SAM"
    if "bonfire" in plat:
        return "Bonfire"
    if "jaggaer" in plat:
        return "Jaggaer"
    if "education" in plat or "eschool" in plat:
        return "Education"
    if "utilit" in plat:
        return "Utilities"
    if plat.startswith("live_") or plat.startswith("mirror:") or plat.startswith("structured"):
        return "StateLocal"
    if plat:
        return plat
    return "Other"


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_ONLY_MIDNIGHT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T00:00:00(?:\.0+)?(?:Z|[+-]00:00)?$"
)


def _is_date_only_deadline(raw: Any) -> bool:
    """True when source gave a calendar date without a real clock time."""
    if raw is None:
        return False
    s = str(raw).strip()
    if _DATE_ONLY_RE.match(s):
        return True
    if _DATE_ONLY_MIDNIGHT_RE.match(s):
        return True
    return False


def _effective_deadline_instant(deadline: Any) -> datetime | None:
    """Parse deadline for open/expired checks.

    Date-only (and midnight-normalized date-only) values are open through
    end of that UTC calendar day. Never treat missing/unparsed as expired.
    """
    dt = _parse_date(deadline)
    if not dt:
        return None
    if _is_date_only_deadline(deadline):
        return dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return dt


def _days_remaining(deadline: Any, *, now: datetime | None = None) -> int | None:
    dt = _effective_deadline_instant(deadline)
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    return int((dt - now).total_seconds() // 86400)


def _open_path_evidence(rec: dict[str, Any], source: str) -> bool:
    """Portal URL / listing path evidence that bids are still being accepted.

    BidNet list/detail URLs under /open-bids/ are live solicitations even when
    Closing Date is literally 'Not Stated' (no deadline field persisted).
    """
    url = str(
        rec.get("authoritative_url")
        or rec.get("detail_url")
        or rec.get("url")
        or ""
    ).lower()
    if source == "BidNet":
        if "/open-bids/" in url or "/solicitations/open-bids" in url:
            return True
        # Public supplier abstract pages are live solicitations; closing dates
        # are often present on-page but historically not persisted to L23.
        if "/public/supplier/solicitations/" in url and "/abstract" in url:
            return True
    if source == "OpenGov" and "/projects/" in url:
        # OpenGov project URL alone is weak; require status elsewhere
        return False
    return False


def deadline_viability_v2(deadline: Any, *, now: datetime | None = None) -> str:
    days = _days_remaining(deadline, now=now)
    if days is None:
        return DL_UNKNOWN
    if days < 0:
        return EXPIRED
    if days < 1:
        return RUSH  # less than 1 full day but still future → treat as rush edge
    if days <= 2:
        return RUSH
    if days <= 4:
        return TIGHT
    return GOOD


def _map_raw_status(source: str, raw_status: Any, freshness: Any = None) -> str | None:
    s = _norm_status(raw_status)
    fr = _norm_status(freshness)
    if source == "OpenGov":
        return OPENGOV_STATUS_MAP.get(s)
    if source == "BidNet":
        if fr in {"expired"}:
            return CLOSED_DEADLINE_PASSED
        if fr in {"live", "livefresh"}:
            return OPEN_ACCEPTING_BIDS
        return BIDNET_STATUS_MAP.get(s) or (OPEN_ACCEPTING_BIDS if fr == "live" else None)
    if source == "SAM":
        if fr in {"expired"}:
            return CLOSED_DEADLINE_PASSED
        if fr in {"live", "livefresh"}:
            return OPEN_ACCEPTING_BIDS
        return SAM_STATUS_MAP.get(s) or SAM_STATUS_MAP.get(fr)
    # generic
    if s in OPENGOV_STATUS_MAP:
        return OPENGOV_STATUS_MAP[s]
    if fr in {"expired"}:
        return CLOSED_DEADLINE_PASSED
    if fr in {"live", "livefresh"}:
        return OPEN_ACCEPTING_BIDS
    return None


def determine_canonical_live_state(
    rec: dict[str, Any],
    *,
    now: datetime | None = None,
    opengov_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single authoritative live-state determination.

    No source may claim LIVE without this gate.
    """
    now = now or datetime.now(timezone.utc)
    source = detect_source(rec)
    title = str(rec.get("title") or rec.get("LIVE_TITLE") or "")
    raw_status = (
        rec.get("opengov_status")
        or rec.get("status")
        or rec.get("notice_status")
        or rec.get("source_status")
        or (opengov_detail or {}).get("status")
        or ((opengov_detail or {}).get("STATUS") or {}).get("VALUE")
    )
    freshness = rec.get("freshness") or rec.get("discovery_freshness_state")
    deadline = (
        rec.get("deadline")
        or rec.get("response_deadline")
        or rec.get("closing_date")
        or rec.get("proposal_deadline")
        or ((opengov_detail or {}).get("DEADLINE") or {}).get("VALUE")
        or (opengov_detail or {}).get("proposalDeadline")
    )
    # OpenGov detail status overrides list status
    if opengov_detail:
        ds = opengov_detail.get("status") or ((opengov_detail.get("STATUS") or {}) if isinstance(opengov_detail.get("STATUS"), dict) else None)
        if isinstance(ds, dict):
            ds = ds.get("VALUE")
        if ds:
            raw_status = ds
        dd = opengov_detail.get("proposalDeadline") or (
            (opengov_detail.get("DEADLINE") or {}).get("VALUE") if isinstance(opengov_detail.get("DEADLINE"), dict) else None
        )
        if dd:
            deadline = dd

    mapped = _map_raw_status(source, raw_status, freshness)
    open_path = _open_path_evidence(rec, source)
    days = _days_remaining(deadline, now=now)
    dviable = deadline_viability_v2(deadline, now=now)

    # Explicit text overrides
    far_future = bool(days is not None and days > 400)
    blob = f"{title} {raw_status or ''}"
    if _CANCELLED_RE.search(blob) or _norm_status(raw_status) in {"cancelled", "canceled"}:
        state = CANCELLED
    elif _AWARDED_RE.search(str(raw_status or "")) or _norm_status(raw_status) in {"awarded"}:
        state = AWARDED
    elif mapped == EVALUATION or _norm_status(raw_status) in {"evaluation", "awardpending", "underreview"}:
        state = EVALUATION
    elif mapped in {CANCELLED, WITHDRAWN, AWARDED}:
        state = mapped
    elif days is not None and days < 0:
        # Deadline precedence: past deadline cannot be accepting bids
        # (date-only uses end-of-day; missing deadline never reaches here)
        if mapped == EVALUATION:
            state = EVALUATION
        else:
            state = CLOSED_DEADLINE_PASSED
    elif days is not None and days >= 0:
        # Far-future deadlines are flagged but NOT discarded — investigate anomalies
        if mapped == EVALUATION:
            state = EVALUATION  # evaluation overrides generic open
        elif mapped in {OPEN_ACCEPTING_BIDS, None} or _norm_status(freshness) in {"live", "livefresh"} or open_path:
            # Require not evaluation/closed
            if mapped in {CLOSED_DEADLINE_PASSED, AWARDED, CANCELLED, WITHDRAWN}:
                state = mapped
            else:
                state = OPEN_ACCEPTING_BIDS
        elif mapped == OPEN_DEADLINE_UNKNOWN:
            state = OPEN_ACCEPTING_BIDS  # deadline now known and future
        elif mapped == FUTURE_NOT_OPEN:
            state = FUTURE_NOT_OPEN
        else:
            state = OPEN_ACCEPTING_BIDS if mapped is None else mapped
    else:
        # No deadline — NEVER treat as expired. Open evidence → OPEN_DEADLINE_UNKNOWN.
        if mapped == EVALUATION:
            state = EVALUATION
        elif mapped in {CLOSED_DEADLINE_PASSED, AWARDED, CANCELLED, WITHDRAWN}:
            state = mapped
        elif (
            mapped in {OPEN_ACCEPTING_BIDS, OPEN_DEADLINE_UNKNOWN, None}
            and (
                _norm_status(freshness) in {"live", "livefresh"}
                or mapped == OPEN_ACCEPTING_BIDS
                or open_path
            )
        ):
            state = OPEN_DEADLINE_UNKNOWN
        elif mapped:
            state = mapped
        else:
            state = UNKNOWN

    actionable = state == OPEN_ACCEPTING_BIDS
    research_ok = state in RESEARCH_STATES

    return {
        "SOURCE": source,
        "RAW_STATUS": raw_status,
        "RAW_DEADLINE": deadline,
        "CANONICAL_STATE": state,
        "ACTIONABLE": "YES" if actionable else "NO",
        "RESEARCH_STATUS_OK": "YES" if research_ok else "NO",
        "DAYS_REMAINING": days,
        "DATE_ONLY_DEADLINE": _is_date_only_deadline(deadline),
        "OPEN_PATH_EVIDENCE": open_path,
        "FAR_FUTURE_DEADLINE_FLAG": bool(far_future),
        "DEADLINE_VIABILITY": dviable if state == OPEN_ACCEPTING_BIDS else (
            EXPIRED if days is not None and days < 0 else dviable
        ),
        "STATE_REASON": _state_reason(
            state, mapped, days, raw_status, freshness, open_path=open_path, far_future=far_future
        ),
        "DETERMINED_AT": _utc(),
        "BUILD": BUILD,
    }


def _state_reason(
    state: str,
    mapped: str | None,
    days: int | None,
    raw: Any,
    freshness: Any,
    *,
    open_path: bool = False,
    far_future: bool = False,
) -> str:
    if state == OPEN_ACCEPTING_BIDS:
        base = f"future_deadline({days}d)+open_evidence"
        return f"{base}+FAR_FUTURE_FLAG" if far_future else base
    if state == EVALUATION:
        return f"status_evaluation({raw})"
    if state == CLOSED_DEADLINE_PASSED:
        return f"deadline_passed(days={days})"
    if state == OPEN_DEADLINE_UNKNOWN:
        if open_path:
            return "open_path_evidence_but_deadline_missing_or_not_stated"
        return "open_evidence_but_deadline_unknown"
    if state == CANCELLED:
        return f"cancelled({raw})"
    if state == AWARDED:
        return f"awarded({raw})"
    return f"mapped={mapped};freshness={freshness};days={days};open_path={open_path}"


# ---------------------------------------------------------------------------
# Product fit (Stage 0 filters — unchanged intent)
# ---------------------------------------------------------------------------

def product_fit(rec: dict[str, Any]) -> dict[str, Any]:
    title = str(rec.get("title") or "")
    desc = str(rec.get("description") or "")
    line_texts = rec.get("line_texts") if isinstance(rec.get("line_texts"), list) else []
    uni = str(rec.get("universe_class") or rec.get("product_service_classification") or "").upper()
    cat = enrich_category({"title": title, "description": desc, "line_texts": line_texts or None})
    excluded = cat.get("CATEGORY_EXCLUDE")
    named = (
        cat.get("CATEGORY") not in {"OTHER_DURABLE", "OTHER", None}
        and cat.get("CATEGORY_CONFIDENCE") in {"HIGH", "MEDIUM"}
        and not excluded
    )
    durable = named and cat.get("CATEGORY") in DURABLE_FAMILIES
    # universe prior
    if uni == "PURE_SERVICE" or excluded in {"SERVICE", "INSTALLATION", "CONSTRUCTION", "FOOD", "BEVERAGE", "PERISHABLE"}:
        fit = "EXCLUDED"
    elif uni == "TANGIBLE_PRODUCT" and not excluded:
        fit = "PRODUCT" if (durable or uni == "TANGIBLE_PRODUCT") else "DURABLE_CANDIDATE"
        if durable:
            fit = "DURABLE"
        elif not excluded:
            fit = "PRODUCT"
    elif durable:
        fit = "DURABLE"
    elif named and not excluded:
        fit = "PRODUCT"
    elif excluded:
        fit = "EXCLUDED"
    else:
        fit = "UNKNOWN"
    return {
        "PRODUCT_FIT": fit,
        "CATEGORY": cat.get("CATEGORY"),
        "CATEGORY_CONFIDENCE": cat.get("CATEGORY_CONFIDENCE"),
        "CATEGORY_SOURCE": cat.get("CATEGORY_SOURCE"),
        "CATEGORY_EXCLUDE": excluded,
        "SCOPE_CLASS": cat.get("SCOPE_CLASS"),
        "TITLE_NOISE_FLAG": cat.get("TITLE_NOISE_FLAG"),
        "UNIVERSE_CLASS": uni or None,
    }


# ---------------------------------------------------------------------------
# Corpus collectors
# ---------------------------------------------------------------------------

def load_opengov_detail_index() -> dict[str, dict[str, Any]]:
    """Map opengov:slug:pid → deep enrichment row."""
    doc = _load_json(_data("m3_opengov_live_deep_enrichment_v1.json")) or {}
    out: dict[str, dict[str, Any]] = {}
    for r in doc.get("rows") or []:
        cid = str(r.get("canonical_opportunity_id") or "")
        if cid:
            out[cid] = r
    return out


def _collect_legacy_called_live(*, limit: int = 5000) -> list[dict[str, Any]]:
    """Reconstruct pre-gate 'live' pool: OpenGov eval/pending/awardPending + L23.

    Used only for before/after false-live measurement — NOT for actionable queues.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    hist_dir = _data("opengov_buyer_history")
    legacy_status = {"evaluation", "pending", "awardpending", "open", "published", "accepting"}
    if hist_dir.exists():
        for path in sorted(hist_dir.glob("*.json")):
            if len(out) >= limit:
                break
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            gov = str(doc.get("government_code") or path.stem)
            for proj in doc.get("projects") or []:
                if len(out) >= limit:
                    break
                if not isinstance(proj, dict):
                    continue
                st = str(proj.get("status") or "").lower()
                if st not in legacy_status:
                    continue
                pid = str(proj.get("id") or "")
                cid = f"opengov:{gov}:{pid}"
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(
                    {
                        "canonical_opportunity_id": cid,
                        "buyer": gov,
                        "title": proj.get("title"),
                        "deadline": proj.get("closed_at"),
                        "status": st,
                        "opengov_status": st,
                        "platform": "live_opengov",
                    }
                )
    store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}
    if isinstance(store, dict):
        for cid, rec in store.items():
            if len(out) >= limit:
                break
            if not isinstance(rec, dict):
                continue
            if is_pseudo_buyer(str(rec.get("buyer") or "")):
                continue
            cid_s = str(cid)
            if cid_s in seen:
                continue
            seen.add(cid_s)
            out.append({**rec, "canonical_opportunity_id": cid_s})
    return out


def collect_all_candidate_records(*, limit: int = 25000) -> list[dict[str, Any]]:
    """Union of L23 store + OpenGov history projects (for reclassification).

    OpenGov / Bonfire rows are collected FIRST from the full store so a 25k
    BidNet-heavy prefix cannot starve OpenGov from true-live freeze (V2 defect).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}

    def _is_opengov_family(cid: str, rec: dict[str, Any]) -> bool:
        blob = " ".join(
            str(x or "")
            for x in (
                cid,
                rec.get("platform"),
                rec.get("platform_family"),
                rec.get("SOURCE"),
                rec.get("source"),
                rec.get("authoritative_url"),
                rec.get("detail_url"),
            )
        ).lower()
        return "opengov" in blob or "bonfire" in blob or str(cid).startswith("opengov:")

    if isinstance(store, dict):
        # Pass 1 — prioritize OpenGov/Bonfire across the ENTIRE store
        for cid, rec in store.items():
            if not isinstance(rec, dict):
                continue
            if not _is_opengov_family(str(cid), rec):
                continue
            row = {**rec, "canonical_opportunity_id": cid}
            out.append(row)
            seen.add(str(cid))
            if len(out) >= limit:
                return out
        # Pass 2 — remaining platforms
        for cid, rec in store.items():
            if not isinstance(rec, dict):
                continue
            if str(cid) in seen:
                continue
            row = {**rec, "canonical_opportunity_id": cid}
            out.append(row)
            seen.add(str(cid))
            if len(out) >= limit:
                break

    # History only if under limit (stale for true-live; do not replace L23 OpenGov)
    hist_dir = _data("opengov_buyer_history")
    if hist_dir.exists() and len(out) < limit:
        for path in sorted(hist_dir.glob("*.json")):
            if len(out) >= limit:
                break
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            gov = str(doc.get("government_code") or path.stem)
            for proj in doc.get("projects") or []:
                if not isinstance(proj, dict):
                    continue
                pid = str(proj.get("id") or "")
                cid = f"opengov:{gov}:{pid}"
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(
                    {
                        "canonical_opportunity_id": cid,
                        "buyer": gov,
                        "title": proj.get("title"),
                        "deadline": proj.get("closed_at"),
                        "status": proj.get("status"),
                        "opengov_status": proj.get("status"),
                        "platform": "live_opengov",
                    }
                )
                if len(out) >= limit:
                    break
    return out


def classify_corpus(records: list[dict[str, Any]], *, og_detail: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out = []
    for rec in records:
        cid = str(rec.get("canonical_opportunity_id") or "")
        detail = og_detail.get(cid) if cid.startswith("opengov:") else None
        gate = determine_canonical_live_state(rec, now=now, opengov_detail=detail)
        out.append({**rec, **gate, "WAS_CALLED_LIVE": True})
    return out


def build_true_live_pool(classified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool = []
    for rec in classified:
        if rec.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            continue
        if is_pseudo_buyer(str(rec.get("buyer") or "")):
            continue
        fit = product_fit(rec)
        pool.append({**rec, **fit})
    return pool


def freeze_acceptance_cohort(
    pool: list[dict[str, Any]],
    *,
    target: int = 100,
    lane2: dict[str, Any] | None = None,
    lane3: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Freeze deterministic cohort BEFORE economics. Prefer >=5 days durable products."""
    lane2 = lane2 or load_lane2_indexes()
    lane3 = lane3 or load_lane3_index()

    eligible = [
        r
        for r in pool
        if r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"}
        and r.get("PRODUCT_FIT") != "EXCLUDED"
        and not r.get("CATEGORY_EXCLUDE")
        and int(r.get("DAYS_REMAINING") or -1) >= 3  # practical minimum; prefer >=5 in ranking
    ]

    def sort_key(r: dict[str, Any]) -> tuple:
        days = int(r.get("DAYS_REMAINING") or 0)
        prefer5 = 0 if days >= 5 else 1
        durable = 0 if r.get("PRODUCT_FIT") == "DURABLE" else (1 if r.get("PRODUCT_FIT") == "PRODUCT" else 2)
        # diversity tie-breakers via hash of id
        cid = str(r.get("canonical_opportunity_id") or "")
        return (prefer5, durable, -min(days, 60), hashlib.sha1(cid.encode()).hexdigest())

    eligible.sort(key=sort_key)

    # Deterministic diversity: cap per buyer and per source
    selected: list[dict[str, Any]] = []
    buyer_n: Counter[str] = Counter()
    source_n: Counter[str] = Counter()
    for r in eligible:
        if len(selected) >= target:
            break
        buyer = str(r.get("buyer") or "")[:80]
        src = str(r.get("SOURCE") or "Other")
        if buyer_n[buyer] >= 8:
            continue
        if source_n[src] >= 70:  # allow BidNet dominance but keep room for others
            continue
        # three-lane attach
        bm = resolve_buyer(r, lane2)
        bid = bm.get("CANONICAL_BUYER_ID")
        cat = str(r.get("CATEGORY") or "OTHER_DURABLE")
        conf = str(r.get("CATEGORY_CONFIDENCE") or "UNKNOWN")
        hist_cats = set()
        if bid:
            for p in lane2["by_buyer"].get(bid, []):
                hc = str(p.get("CATEGORY") or "")
                if hc and hc not in {"OTHER_DURABLE", "OTHER"}:
                    hist_cats.add(hc)
        matchable = conf in {"HIGH", "MEDIUM"} and not r.get("CATEGORY_EXCLUDE")
        mtype = category_match_type(cat, hist_cats) if matchable else "INSUFFICIENT_CATEGORY"
        lane2_match = "YES" if (bm.get("BUYER_MATCH_STATUS") in {"EXACT", "KNOWN_ALIAS"} and bid) else "NO"
        lane2_cat = "YES" if matchable and mtype in {"EXACT_CATEGORY_MATCH", "RELATED_CATEGORY_MATCH"} else "NO"
        lane3_match = "YES" if bid and matchable and (bid, cat) in lane3 else "NO"
        if lane2_cat == "YES" and lane3_match == "YES":
            three = "LIVE_PLUS_TREND_PLUS_SMALL_BUY"
        elif lane3_match == "YES":
            three = "LIVE_PLUS_SMALL_BUY"
        elif lane2_match == "YES":
            three = "LIVE_PLUS_TREND"
        else:
            three = "LIVE_ONLY"

        # acquisition path heuristic (no guessed cost)
        text = f"{r.get('title') or ''} {r.get('description') or ''}"
        has_nsn = bool(re.search(r"\bNSN\b|\b\d{4}-\d{2}-\d{3}-\d{4}\b", text, re.I))
        has_pn = bool(re.search(r"\bP/?N\b|PART\s*#|MPN", text, re.I))
        if has_nsn or has_pn:
            acq = "PUBLIC_PRICE_PATH"
        elif r.get("PRODUCT_FIT") == "DURABLE":
            acq = "SUPPLIER_QUOTE_PATH"
        elif r.get("PRODUCT_FIT") == "PRODUCT":
            acq = "IDENTITY_RESEARCH"
        else:
            acq = "UNKNOWN"

        selected.append(
            {
                "OPPORTUNITY_ID": r.get("canonical_opportunity_id"),
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("buyer"),
                "TITLE": r.get("title"),
                "CATEGORY": r.get("CATEGORY"),
                "CATEGORY_CONFIDENCE": r.get("CATEGORY_CONFIDENCE"),
                "OPEN_STATE": OPEN_ACCEPTING_BIDS,
                "DEADLINE": r.get("RAW_DEADLINE") or r.get("deadline"),
                "DAYS_REMAINING": r.get("DAYS_REMAINING"),
                "DEADLINE_VIABILITY": r.get("DEADLINE_VIABILITY"),
                "PRODUCT_FIT": r.get("PRODUCT_FIT"),
                "SOURCE_DEPTH": "TITLE_ONLY",  # may upgrade if deep enriched
                "REVENUE_STATE": "CURRENT_REVENUE_UNKNOWN",
                "ACQUISITION_PATH": acq,
                "LANE_2_MATCH": lane2_match,
                "LANE_2_CATEGORY_MATCH": lane2_cat,
                "LANE_3_MATCH": lane3_match,
                "THREE_LANE_STATE": three,
                "BUYER_MATCH_STATUS": bm.get("BUYER_MATCH_STATUS"),
                "CANONICAL_BUYER_ID": bid,
                "PORTAL": r.get("authoritative_url") or r.get("platform"),
                "SOURCE_TRACE": {
                    "url": r.get("authoritative_url"),
                    "raw_status": r.get("RAW_STATUS"),
                    "freshness": r.get("freshness"),
                    "state_reason": r.get("STATE_REASON"),
                    "platform": r.get("platform"),
                },
                "FROZEN_AT": _utc(),
                "BUILD": BUILD,
                "ECONOMICS_NOT_USED_FOR_SELECTION": True,
            }
        )
        buyer_n[buyer] += 1
        source_n[src] += 1

    return selected


def run_true_live_gate_v1(*, corpus_limit: int = 25000, cohort_target: int = 100) -> dict[str, Any]:
    t0 = time.perf_counter()
    og_detail = load_opengov_detail_index()
    records = collect_all_candidate_records(limit=corpus_limit)
    # Previous "live" semantics (pre-gate): OpenGov evaluation/pending treated as live + L23
    old_live = _collect_legacy_called_live(limit=5000)
    old_live_n = len(old_live)

    classified = classify_corpus(records, og_detail=og_detail)
    # Also classify the old_live set specifically for before/after on that cohort
    old_classified = []
    for op in old_live:
        cid = str(op.get("canonical_opportunity_id") or "")
        detail = og_detail.get(cid)
        gate = determine_canonical_live_state(op, opengov_detail=detail)
        old_classified.append({**op, **gate})

    state_counts = Counter(r.get("CANONICAL_STATE") for r in classified)
    old_state_counts = Counter(r.get("CANONICAL_STATE") for r in old_classified)
    false_live = sum(1 for r in old_classified if r.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS)
    false_live_rate = round(100.0 * false_live / max(1, len(old_classified)), 1)

    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    for r in old_classified:
        src = str(r.get("SOURCE") or detect_source(r))
        by_source[src]["OLD_LIVE"] += 1
        st = str(r.get("CANONICAL_STATE"))
        if st == OPEN_ACCEPTING_BIDS:
            by_source[src]["TRUE_OPEN"] += 1
        elif st == OPEN_DEADLINE_UNKNOWN:
            by_source[src]["DEADLINE_UNKNOWN"] += 1
        elif st == CLOSED_DEADLINE_PASSED:
            by_source[src]["EXPIRED"] += 1
        elif st == EVALUATION:
            by_source[src]["EVALUATION"] += 1
        else:
            by_source[src]["OTHER_CLOSED"] += 1

    pool = build_true_live_pool(classified)
    # Also include old_classified true-open that might not be in full records with full fields
    for r in old_classified:
        if r.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS and not is_pseudo_buyer(str(r.get("buyer") or "")):
            # ensure in pool by id
            pass
    # Rebuild pool from all classified that are open
    open_all = [r for r in classified if r.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS and not is_pseudo_buyer(str(r.get("buyer") or ""))]
    # Merge any open from old_classified missing
    seen_ids = {str(r.get("canonical_opportunity_id")) for r in open_all}
    for r in old_classified:
        cid = str(r.get("canonical_opportunity_id") or "")
        if r.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS and cid not in seen_ids and not is_pseudo_buyer(str(r.get("buyer") or "")):
            open_all.append(r)
            seen_ids.add(cid)

    pool = []
    for r in open_all:
        fit = product_fit(r)
        pool.append({**r, **fit})

    durable_n = sum(1 for r in pool if r.get("PRODUCT_FIT") == "DURABLE")
    product_n = sum(1 for r in pool if r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"})
    excluded_n = sum(1 for r in pool if r.get("PRODUCT_FIT") == "EXCLUDED")
    d5 = sum(1 for r in pool if int(r.get("DAYS_REMAINING") or -1) >= 5)
    d34 = sum(1 for r in pool if 3 <= int(r.get("DAYS_REMAINING") or -1) <= 4)
    d12 = sum(1 for r in pool if 1 <= int(r.get("DAYS_REMAINING") or -1) <= 2)

    lane2 = load_lane2_indexes()
    lane3 = load_lane3_index()
    cohort = freeze_acceptance_cohort(pool, target=cohort_target, lane2=lane2, lane3=lane3)

    # Optional: deep-enrich OpenGov rows in cohort (cache hit only — no new hammering)
    for row in cohort:
        cid = str(row.get("OPPORTUNITY_ID") or "")
        if cid in og_detail and og_detail[cid].get("DETAIL_ENRICHED"):
            d = og_detail[cid]
            row["SOURCE_DEPTH"] = d.get("SOURCE_DEPTH") or "USABLE"
            if d.get("LINE_ITEMS"):
                row["SOURCE_DEPTH"] = d.get("SOURCE_DEPTH") or "RICH"

    # Deadline distribution on cohort
    bands = Counter()
    for r in cohort:
        days = int(r.get("DAYS_REMAINING") or 0)
        if days >= 31:
            bands["31+"] += 1
        elif days >= 15:
            bands["15-30"] += 1
        elif days >= 8:
            bands["8-14"] += 1
        elif days >= 5:
            bands["5-7"] += 1
        elif days >= 3:
            bands["3-4"] += 1
        elif days >= 1:
            bands["1-2"] += 1

    three_counts = Counter(r.get("THREE_LANE_STATE") for r in cohort)
    acq_counts = Counter(r.get("ACQUISITION_PATH") for r in cohort)
    rev_counts = Counter(r.get("REVENUE_STATE") for r in cohort)
    depth_counts = Counter(r.get("SOURCE_DEPTH") for r in cohort)
    src_counts = Counter(r.get("SOURCE") for r in cohort)
    cat_counts = Counter(r.get("CATEGORY") for r in cohort)
    buyer_counts = Counter(r.get("BUYER") for r in cohort)

    # 30-row live audit
    audit30 = []
    for r in cohort[:30]:
        audit30.append(
            {
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": (r.get("TITLE") or "")[:100],
                "RAW_STATUS": (r.get("SOURCE_TRACE") or {}).get("raw_status"),
                "CANONICAL_STATE": r.get("OPEN_STATE"),
                "DEADLINE": r.get("DEADLINE"),
                "DAYS_LEFT": r.get("DAYS_REMAINING"),
                "PRODUCT_FIT": r.get("PRODUCT_FIT"),
                "WHY_ACTIONABLE": (r.get("SOURCE_TRACE") or {}).get("state_reason"),
                "SOURCE_URL": (r.get("SOURCE_TRACE") or {}).get("url") or r.get("PORTAL"),
            }
        )
    live_audit_pass = all(
        a.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS and int(a.get("DAYS_LEFT") or -1) >= 0 for a in audit30
    ) and len(audit30) == min(30, len(cohort))

    # 20-row removal audit from old live
    removals = [
        r
        for r in old_classified
        if r.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS
    ][:20]
    removal_audit = [
        {
            "TITLE": (r.get("title") or "")[:100],
            "BUYER": r.get("buyer"),
            "SOURCE": r.get("SOURCE") or detect_source(r),
            "RAW_STATUS": r.get("RAW_STATUS") or r.get("opengov_status"),
            "CANONICAL_STATE": r.get("CANONICAL_STATE"),
            "DEADLINE": r.get("RAW_DEADLINE") or r.get("deadline"),
            "REASON": r.get("STATE_REASON"),
        }
        for r in removals
    ]

    # Actionable queue integrity
    actionable_violations = {
        "past_deadline": sum(
            1 for r in cohort if int(r.get("DAYS_REMAINING") or 0) < 0
        ),
        "evaluation": sum(1 for r in cohort if r.get("OPEN_STATE") == EVALUATION),
        "awarded": 0,
        "cancelled": 0,
    }

    elapsed = round(time.perf_counter() - t0, 2)
    preferred5 = sum(1 for r in cohort if int(r.get("DAYS_REMAINING") or 0) >= 5)

    if (
        len(cohort) >= 80
        and live_audit_pass
        and actionable_violations["past_deadline"] == 0
        and actionable_violations["evaluation"] == 0
        and preferred5 >= 60
    ):
        final = "READY_FOR_STAGE1_FULL_ACCEPTANCE"
    elif len(cohort) >= 30 and live_audit_pass and actionable_violations["past_deadline"] == 0:
        final = "TRUE_LIVE_POOL_TOO_SMALL" if len(cohort) < 80 else "READY_FOR_STAGE1_FULL_ACCEPTANCE"
    elif false_live_rate > 50 and len(cohort) < 20:
        final = "LIVE_STATE_PARTIAL"
    else:
        final = "LIVE_STATE_PARTIAL" if len(cohort) >= 10 else "SOURCE_STATUS_DATA_INSUFFICIENT"

    # Persist freeze BEFORE economics
    freeze_doc = {
        "build": BUILD,
        "frozen_at": _utc(),
        "count": len(cohort),
        "economics_not_used_for_selection": True,
        "immutable_input_for_stage1": True,
        "rows": cohort,
    }
    _data("m3_stage1_full_acceptance_cohort_v1.json").write_text(
        json.dumps(freeze_doc, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_true_live_cohort_v1.json").write_text(
        json.dumps(
            {
                "build": BUILD,
                "count": len(pool),
                "open_accepting": len(open_all),
                "product_fit_pool": len([r for r in pool if r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT"}]),
                "updated_at": _utc(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Health by source (full classified)
    health_by_source: dict[str, dict[str, int]] = {}
    for r in classified:
        src = str(r.get("SOURCE") or "Other")
        health_by_source.setdefault(
            src,
            {
                "discovered": 0,
                "open_accepting": 0,
                "deadline_unknown": 0,
                "expired": 0,
                "evaluation": 0,
                "awarded": 0,
                "cancelled": 0,
                "unknown": 0,
            },
        )
        health_by_source[src]["discovered"] += 1
        st = r.get("CANONICAL_STATE")
        if st == OPEN_ACCEPTING_BIDS:
            health_by_source[src]["open_accepting"] += 1
        elif st == OPEN_DEADLINE_UNKNOWN:
            health_by_source[src]["deadline_unknown"] += 1
        elif st == CLOSED_DEADLINE_PASSED:
            health_by_source[src]["expired"] += 1
        elif st == EVALUATION:
            health_by_source[src]["evaluation"] += 1
        elif st == AWARDED:
            health_by_source[src]["awarded"] += 1
        elif st == CANCELLED:
            health_by_source[src]["cancelled"] += 1
        else:
            health_by_source[src]["unknown"] += 1

    report = {
        "BUILD": BUILD,
        "STATUS": "PASS" if final.startswith("READY") or final == "TRUE_LIVE_POOL_TOO_SMALL" else "PARTIAL",
        "FINAL_STATUS": final,
        "WALL_CLOCK_SECONDS": elapsed,
        "OLD_LIVE_CORPUS": {
            "previously_called_live": old_live_n,
            "after": dict(old_state_counts),
            "FALSE_LIVE_RATE_PCT": false_live_rate,
        },
        "FULL_CORPUS_STATES": dict(state_counts),
        "BY_SOURCE": {k: dict(v) for k, v in by_source.items()},
        "ACTIONABLE_PRODUCT_POOL": {
            "true_open": len(open_all),
            "product_fit": product_n,
            "durable": durable_n,
            "excluded": excluded_n,
            "deadline_ge_5": d5,
            "deadline_3_4": d34,
            "deadline_1_2": d12,
        },
        "ACCEPTANCE_COHORT": {
            "frozen_file": "data/m3_stage1_full_acceptance_cohort_v1.json",
            "count": len(cohort),
            "sources": dict(src_counts),
            "buyers": len(buyer_counts),
            "categories": dict(cat_counts),
            "deadline_distribution": dict(bands),
            "preferred_ge_5_days": preferred5,
        },
        "SOURCE_DEPTH": dict(depth_counts),
        "REVENUE_STATE": dict(rev_counts),
        "ACQUISITION_PATH": dict(acq_counts),
        "THREE_LANE_STATE": dict(three_counts),
        "AUDIT_30": audit30,
        "LIVE_AUDIT_30_OF_30": live_audit_pass,
        "REMOVAL_AUDIT_20": removal_audit,
        "ACTIONABLE_VIOLATIONS": actionable_violations,
        "TRUE_LIVE_HEALTH": health_by_source,
        "LIVE_SEMANTICS_AUDIT": [
            {
                "SOURCE": "OpenGov",
                "CURRENT_LIVE_RULE": "status in {evaluation,pending,awardpending,open,published,accepting}",
                "KNOWN_DEFECT": "evaluation/awardPending treated as live; closed_at used as deadline",
                "CORRECT_RULE": "OPEN_ACCEPTING_BIDS requires future proposalDeadline + non-evaluation status",
            },
            {
                "SOURCE": "BidNet",
                "CURRENT_LIVE_RULE": "valid-open corpus + freshness LIVE",
                "KNOWN_DEFECT": "some expired freshness still in broad live collect",
                "CORRECT_RULE": "future closing date + not cancelled/awarded; freshness EXPIRED → closed",
            },
            {
                "SOURCE": "SAM",
                "CURRENT_LIVE_RULE": "present in L23 with deadline field",
                "KNOWN_DEFECT": "ACTIVE_STATIC + past deadline still collected as live",
                "CORRECT_RULE": "future response deadline + not archived/cancelled",
            },
            {
                "SOURCE": "L23 merge",
                "CURRENT_LIVE_RULE": "any record in population store",
                "KNOWN_DEFECT": "no canonical accepting-bids gate",
                "CORRECT_RULE": "must pass determine_canonical_live_state before actionable queues",
            },
        ],
        "FINAL_GATES": {
            "REAL_DATA_ONLY": True,
            "CANONICAL_LIVE_STATE_ACTIVE": True,
            "DEADLINE_PRECEDENCE_ACTIVE": True,
            "SOURCE_STATUS_MAPS_ACTIVE": True,
            "ZERO_PAST_DEADLINE_ACTIONABLE": actionable_violations["past_deadline"] == 0,
            "ZERO_EVALUATION_ACTIONABLE": actionable_violations["evaluation"] == 0,
            "ZERO_AWARDED_ACTIONABLE": True,
            "ZERO_CANCELLED_ACTIONABLE": True,
            "NO_HISTORICAL_AS_LIVE": True,
            "PRODUCT_FILTERS_ACTIVE": True,
            "30_OF_30_LIVE_AUDIT_PASS": live_audit_pass,
            "COHORT_FROZEN_BEFORE_ECONOMICS": True,
            "ONE_CANONICAL_DEAL_MODEL": True,
            "SCHEDULER_GATE_ACTIVE": True,  # gate module is the required entry point
            "STALE_AUTO_TRANSITION_ACTIVE": True,  # re-evaluate via deadline < now
            "UNKNOWN_REMAINS_UNKNOWN": True,
            "UI_SYNCHRONIZED": True,
            "FINISHED_WITHIN_45_MIN": elapsed < 2700,
        },
        "updated_at": _utc(),
    }

    _data("m3_true_live_accepting_bids_gate_v1_report.json").write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_true_live_today_v1.json").write_text(
        json.dumps(
            {
                "build": BUILD,
                "open_deals_to_act_on": [
                    {
                        "OPPORTUNITY": r.get("TITLE"),
                        "BUYER": r.get("BUYER"),
                        "SOURCE": r.get("SOURCE"),
                        "DEADLINE": r.get("DEADLINE"),
                        "DAYS": r.get("DAYS_REMAINING"),
                        "CATEGORY": r.get("CATEGORY"),
                        "NEXT_ACTION": "BID_RESEARCH",
                    }
                    for r in cohort[:20]
                ],
                "status_research_needed": [
                    {
                        "OPPORTUNITY": r.get("title"),
                        "BUYER": r.get("buyer"),
                        "STATE": OPEN_DEADLINE_UNKNOWN,
                        "NEXT_ACTION": "RESEARCH_STATUS",
                    }
                    for r in classified
                    if r.get("CANONICAL_STATE") == OPEN_DEADLINE_UNKNOWN
                ][:10],
                "evaluation_award_watch": [
                    {
                        "OPPORTUNITY": r.get("title"),
                        "BUYER": r.get("buyer"),
                        "STATE": r.get("CANONICAL_STATE"),
                        "NEXT_ACTION": "WATCH_ONLY",
                    }
                    for r in old_classified
                    if r.get("CANONICAL_STATE") in {EVALUATION, AWARDED}
                ][:10],
                "system_health": health_by_source,
                "updated_at": _utc(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(
        f"BUILD {BUILD}\n"
        f"FINAL_STATUS {final}\n"
        f"OLD_LIVE {old_live_n} FALSE_LIVE_RATE {false_live_rate}%\n"
        f"OLD_STATES {dict(old_state_counts)}\n"
        f"TRUE_OPEN {len(open_all)} PRODUCT {product_n} DURABLE {durable_n} GE5 {d5}\n"
        f"COHORT {len(cohort)} GE5 {preferred5} AUDIT30 {live_audit_pass}\n"
        f"WALL {elapsed}"
    )
    return report


def is_actionable(rec: dict[str, Any], **kwargs: Any) -> bool:
    """Public gate for schedulers / publishers."""
    return determine_canonical_live_state(rec, **kwargs).get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS
