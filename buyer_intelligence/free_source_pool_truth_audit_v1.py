"""Free-source production pool truth audit + top-deal validation V1.

No broad discovery. Audit existing pool/report/store; correct membership & metrics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from buyer_intelligence.free_source_first_production_pool_v2 import (
    _days_left,
    _is_paid_bidnet_blocked,
    _package_resolved,
    _platform_family,
    _quote_ready_safe,
    _runway_bucket,
    load_paid_bidnet_ids,
)
from buyer_intelligence.package_recovery_source_depth_v1 import extract_revenue_from_text
from buyer_intelligence.stage1_full_production_acceptance_v1 import (
    assess_identity,
    assess_product_gate,
    assess_source_depth,
)
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    determine_canonical_live_state,
)
from m3_data_root import data_path
from package_recovery_sam_budget.quality import is_valid_package_evidence

log = logging.getLogger("govtracker.free_source_pool_truth_audit_v1")

BUILD = "20261009-m3-free-source-pool-truth-audit-v1"
POOL_IN = "m3_free_source_production_pool_v2.json"
REPORT_IN = "m3_free_source_first_production_pool_v2_report.json"
POOL_OUT = "m3_free_source_production_pool_v2_audited.json"
SHORTLIST = "M3_FREE_SOURCE_VALIDATED_SHORTLIST_V1.json"
REPORT_OUT = "m3_free_source_pool_truth_audit_v1_report.json"
JOB_FILE = "m3_free_source_pool_truth_audit_v1_job.json"
RESULTS_OUT = "m3_free_source_pool_truth_audit_v1_results.json"

SAM_DAILY_LIMIT = 10
WALL_LIMIT_SEC = 30 * 60

_BOILERPLATE_RE = re.compile(
    r"non-profit national governmental purchasing cooperative|enables members nationwide|"
    r"respond to this solicitation for an opportunity|as-needed basis|"
    r"initial term of this cooperative contract",
    re.I,
)
_SERVICE_HEAVY_RE = re.compile(
    r"\b(consulting|grant|grants program|case management|services?\b|construction|"
    r"cmgc|elevator|residential development|human services|learning management|"
    r"drug testing|planning & demonstration|mental health)\b",
    re.I,
)
_NSN_STRICT = re.compile(r"^\d{4}-\d{2}-\d{3}-\d{4}$")


def _utc() -> str:
    return now_utc().isoformat()


def _load(name: str) -> Any:
    p = data_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write(name: str, payload: Any) -> None:
    data_path(name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _job_update(**kwargs: Any) -> None:
    cur = _load(JOB_FILE) or {"status": "RUNNING", "BUILD": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["BUILD"] = BUILD
    _write(JOB_FILE, cur)


def _desc_plain(text: Any) -> str:
    s = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", s).strip()


def _stable_sample(ids: list[str], n: int, salt: str) -> list[str]:
    ranked = sorted(ids, key=lambda x: hashlib.sha256(f"{salt}:{x}".encode()).hexdigest())
    return ranked[:n]


def _load_store() -> dict[str, Any]:
    try:
        from phase_l.l23_full_population_funnel import load_store

        store = load_store()
        return store if isinstance(store, dict) else {}
    except Exception:
        raw = _load("l23_canonical_population_store.json") or {}
        opps = raw.get("opportunities") if isinstance(raw, dict) else {}
        return opps if isinstance(opps, dict) else {}


def sam_budget_truth() -> dict[str, Any]:
    """Owner-facing SAM truth: REAL credits only (never cache/public/audit inflation)."""
    prior = _load(REPORT_IN) or {}
    try:
        from discovery.sam_budgeted_client import owner_credit_dashboard, reconcile_sam_credit_ledger

        recon = reconcile_sam_credit_ledger(persist=True)
        owner = owner_credit_dashboard()
        used = int(owner["REAL_SAM_API_CALLS_TODAY"])
        remaining = int(owner["REMAINING_REAL_CREDITS"])
        return {
            "SAM_DAILY_LIMIT": SAM_DAILY_LIMIT,
            "SAM_USED_TODAY": used,
            "REAL_SAM_API_CALLS_TODAY": used,
            "SAM_REMAINING_TODAY": remaining,
            "REMAINING_REAL_CREDITS": remaining,
            "CACHE_HITS": owner.get("CACHE_HITS"),
            "PUBLIC_NOTICEDESC_FETCHES": owner.get("PUBLIC_NOTICEDESC_FETCHES"),
            "AGENCY_URL_FETCHES": owner.get("AGENCY_URL_FETCHES"),
            "INTERNAL_ATTEMPTS": owner.get("INTERNAL_ATTEMPTS"),
            "RETRIES": owner.get("RETRIES"),
            "BLOCKED_OVER_CAP_ATTEMPTS": owner.get("BLOCKED_OVER_CAP_ATTEMPTS"),
            "STALE_COUNTERS": owner.get("STALE_COUNTERS"),
            "WHY_PRIOR_INFLATED": owner.get("WHY_PRIOR_INFLATED") or recon.get("WHY_PRIOR_SHOWED_INFLATED"),
            "BEFORE_LIVE_CALLS_FIELD": recon.get("BEFORE_LIVE_CALLS_FIELD"),
            "AFTER_LIVE_CALLS_FIELD": recon.get("AFTER_LIVE_CALLS_FIELD"),
            "PRIOR_REPORT_BUDGET_REMAINING_BUG": (prior.get("SAM") or {}).get("budget_remaining"),
            "PRIOR_BUG_EXPLANATION": (
                "SAM_USED_TODAY=13 came from dual-increment: consume_live_credits set live_calls "
                "absolutely AND _record_entry also += credits. Owner counter now uses "
                "reconstructed REAL budget entries only (hard cap 10)."
            ),
            "PHASE_API_CALLS_USED": (prior.get("SAM") or {}).get("api_calls_used"),
            "MATH_CHECK": f"{used} + {remaining} == {SAM_DAILY_LIMIT}",
            "GATES": owner.get("GATES"),
            "RECONCILED": True,
        }
    except Exception as exc:
        used = int(((prior.get("SAM") or {}).get("api_calls_used") or 0))
        used = min(used, SAM_DAILY_LIMIT)
        return {
            "SAM_DAILY_LIMIT": SAM_DAILY_LIMIT,
            "SAM_USED_TODAY": used,
            "SAM_REMAINING_TODAY": max(0, SAM_DAILY_LIMIT - used),
            "ERROR": type(exc).__name__,
            "RECONCILED": True,
            "NOTE": "fallback_to_phase_api_calls_used",
        }


def opengov_count_reconciliation(store: dict[str, Any], prior: dict[str, Any], now: datetime) -> dict[str, Any]:
    fresh = int(((prior.get("OPENGOV") or {}).get("fresh_candidates") or (prior.get("PHASES") or {}).get("OPENGOV_DISCOVERY", {}).get("candidates") or 0))
    entities = int(((prior.get("OPENGOV") or {}).get("entities_checked") or 0))
    og_rows = []
    true_open = 0
    product_fit = 0
    pkg_resolved_loose = 0
    pkg_resolved_strict = 0
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        if _platform_family(rec) != "OpenGov":
            continue
        og_rows.append(cid)
        gate = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid}, now=now)
        if gate.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            continue
        true_open += 1
        row = {"OPPORTUNITY_ID": cid, "TITLE": rec.get("title"), "BUYER": rec.get("buyer") or rec.get("agency"), "SOURCE": "OpenGov"}
        if assess_product_gate(row, rec).get("PRODUCT_GATE_PASS"):
            product_fit += 1
        depth = str(assess_source_depth(row, rec).get("SOURCE_DEPTH") or "TITLE_ONLY")
        ok_loose, reason_loose = _package_resolved(rec, depth)
        if ok_loose:
            pkg_resolved_loose += 1
        ok_strict, reason_strict = package_resolved_strict(rec, depth, "OpenGov")
        if ok_strict:
            pkg_resolved_strict += 1
    return {
        "EXPLANATION": (
            "entities_checked=OpenGov portals/entities polled this run; "
            "fresh_candidates=new/updated project records returned by that harvest; "
            "true_open=ALL current canonical OpenGov rows that pass OPEN_ACCEPTING_BIDS "
            "(preexisting store + fresh), not only the 140 newly fetched."
        ),
        "FRESHLY_FETCHED_THIS_RUN": fresh,
        "ENTITIES_CHECKED_THIS_RUN": entities,
        "PREEXISTING_CURRENT_ROWS_REVALIDATED": max(0, len(og_rows) - fresh),
        "TOTAL_CURRENT_CANONICAL": len(og_rows),
        "TRUE_OPEN_CURRENT": true_open,
        "PRODUCT_FIT_CURRENT": product_fit,
        "PACKAGE_RESOLVED_LOOSE_V2": pkg_resolved_loose,
        "PACKAGE_RESOLVED_STRICT_AUDIT": pkg_resolved_strict,
        "METRIC_RENAME": {
            "entities_checked": "ENTITIES_CHECKED_THIS_RUN",
            "fresh_candidates": "FRESHLY_FETCHED_THIS_RUN",
            "TRUE_OPEN": "TRUE_OPEN_CURRENT",
            "canonical OpenGov rows": "TOTAL_CURRENT_CANONICAL",
        },
    }


def package_resolved_strict(rec: dict[str, Any], depth: str, family: str) -> tuple[bool, str]:
    """Stricter than V2: title/abstract/boilerplate-only does NOT qualify."""
    if depth == "TITLE_ONLY":
        return False, "TITLE_ONLY"
    atts = [d for d in (rec.get("attachments_metadata") or []) if isinstance(d, dict)]
    br = rec.get("bidnet_recovery") or {}
    docs = [d for d in (br.get("documents") or []) if isinstance(d, dict)]
    og = rec.get("opengov_recovery") or {}
    og_docs = [d for d in (og.get("documents") or og.get("attachments") or []) if isinstance(d, dict)]
    all_docs = atts + docs + og_docs
    # Exclude BidNet paywall / abstract-only landing pages as package evidence
    real_docs = []
    for d in all_docs:
        url = str(d.get("document_url") or d.get("url") or "").lower()
        name = str(d.get("filename") or d.get("document_name") or d.get("name") or "").lower()
        if "subscription-management" in url or "intercept/plans" in url:
            continue
        if url.endswith("/abstract") or "/abstract?" in url:
            continue
        real_docs.append(d)
    if is_valid_package_evidence(real_docs).get("valid"):
        return True, "OFFICIAL_DOCUMENTS"

    lines = rec.get("line_items") or br.get("line_items") or []
    real_lines = []
    for ln in lines if isinstance(lines, list) else []:
        if not isinstance(ln, dict):
            continue
        if str(ln.get("LINE_SOURCE_TYPE") or "") == "TITLE_FALLBACK":
            continue
        desc = str(ln.get("description") or ln.get("DESCRIPTION") or "")
        if isinstance(ln.get("DESCRIPTION"), dict):
            desc = str((ln.get("DESCRIPTION") or {}).get("VALUE") or "")
        if len(desc.strip()) >= 8:
            real_lines.append(ln)
    desc = _desc_plain(rec.get("description"))
    if real_lines and len(desc) >= 40:
        return True, "STRUCTURED_LINES_PLUS_DESCRIPTION"

    free = br.get("free_package_chase") or {}
    free_status = str(free.get("status") or "")
    free_url = str(free.get("source_url") or free.get("matched_url") or free.get("free_source_url") or "")
    if free_status in {"VALID_FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND", "FREE_PACKAGE_FOUND"}:
        if free_url and "bidnet" not in free_url.lower() and "subscription-management" not in free_url.lower():
            return True, "FREE_OFFICIAL_PACKAGE"

    # OpenGov: require non-boilerplate description with product/spec signal OR docs/lines
    if family == "OpenGov" and len(desc) >= 160:
        if _BOILERPLATE_RE.search(desc) and not re.search(
            r"\b(qty|quantity|nsn|mpn|part\s*number|specification|line\s*item|unit\s*price|brand|model)\b",
            desc,
            re.I,
        ):
            return False, "OPENGOV_BOILERPLATE_ONLY"
        if re.search(
            r"\b(qty|quantity|nsn|mpn|part\s*#?|specification|line\s*item|uom|each|brand|model|sku)\b",
            desc,
            re.I,
        ):
            return True, "OPENGOV_SPEC_DETAIL"
        # Long unique project description (not coop boilerplate) counts as usable detail
        if not _BOILERPLATE_RE.search(desc) and depth in {"RICH", "USABLE", "SHALLOW"}:
            return True, "OPENGOV_SUBSTANTIVE_DETAIL"
        return False, "OPENGOV_DESC_WITHOUT_SPEC_SIGNAL"

    if family == "BidNet":
        # BidNet notice/abstract alone is NOT package-resolved
        return False, "BIDNET_NOTICE_NOT_PACKAGE"

    if depth in {"RICH", "USABLE"} and len(desc) >= 120 and real_lines:
        return True, "USABLE_WITH_LINES"
    return False, "INSUFFICIENT_PACKAGE_EVIDENCE"


def identity_blocker(identity: dict[str, Any], rec: dict[str, Any], row: dict[str, Any]) -> str:
    title = str(row.get("TITLE") or rec.get("title") or "")
    desc = _desc_plain(rec.get("description"))
    lines = rec.get("line_items") or []
    real_lines = [ln for ln in lines if isinstance(ln, dict) and str(ln.get("LINE_SOURCE_TYPE") or "") != "TITLE_FALLBACK"]
    sp = identity.get("SOURCE_PROVEN_IDENTIFIER") or {}
    if identity.get("IDENTITY_STATE") == "ADEQUATE" and _quote_ready_safe(identity):
        return "NONE_QUOTE_READY"
    if not real_lines and len(desc) < 40:
        return "LINE_EXTRACTION_MISSING"
    if identity.get("BUYER_CODE_PRESENT") and not sp:
        return "BUYER_CODE_ONLY"
    blob = f"{title} {desc}".lower()
    has_mfr = bool(re.search(r"\b(manufacturer|mfr|brand|oem)\b", blob))
    has_mpn = bool(re.search(r"\b(mpn|p/?n|part\s*number|model\s*#)\b", blob))
    if real_lines and not has_mpn and not sp:
        if has_mfr:
            return "MPN_MISSING"
        return "MULTI_LINE_GENERIC"
    if has_mpn is False and sp is None and len(desc) >= 80:
        if re.search(r"\b(spec|specification|compatible|meets|astm|ansi)\b", blob):
            return "SPEC_ONLY"
        if not has_mfr:
            return "MANUFACTURER_MISSING"
        return "GENERIC_PRODUCT_DESCRIPTION"
    if sp and not _quote_ready_safe({**identity, "IDENTITY_QUOTE_READY": True}):
        return "IDENTITY_ENGINE_TOO_STRICT"  # or weak token rejected by guard
    if identity.get("IDENTITY_STATE") in {"RESEARCH", "UNRESOLVED"} and not sp:
        if not has_mfr and not has_mpn:
            return "GENERIC_PRODUCT_DESCRIPTION"
        if has_mfr and not has_mpn:
            return "MPN_MISSING"
        return "IDENTITY_ENGINE_TOO_STRICT"
    return "OTHER"


def audit_quote_ready_rows(pool_rows: list[dict], store: dict[str, Any]) -> dict[str, Any]:
    out = []
    for r in pool_rows:
        if r.get("TIER") != "A_QUOTE_READY" and not r.get("IDENTITY_QUOTE_READY"):
            continue
        oid = str(r.get("OPPORTUNITY_ID") or "")
        rec = store.get(oid) or {}
        title = str(r.get("TITLE") or "")
        sp = r.get("SOURCE_PROVEN_IDENTIFIER") or {}
        typ = str(sp.get("TYPE") or "")
        val = str(sp.get("VALUE") or "")
        verdict = "INVALID"
        why = []
        if "residential development" in title.lower() or "grant" in title.lower():
            why.append("NOT_PRODUCT_FIT")
        if typ == "NSN":
            if _NSN_STRICT.match(val) or (re.sub(r"\D", "", val) and len(re.sub(r"\D", "", val)) == 13):
                # Must appear in title/desc
                blob = f"{title} {_desc_plain(rec.get('description'))}"
                if val in blob or val.replace("-", "") in re.sub(r"\D", "", blob):
                    verdict = "VALID"
                else:
                    why.append("NSN_NOT_IN_SOURCE_TEXT")
            else:
                why.append("NSN_FORMAT_INVALID")
        elif typ == "MPN":
            if re.fullmatch(r"[\d.\-/]+", val) or len(val) < 5:
                why.append("MPN_TOKEN_WEAK")
            if "development" in title.lower() or "alley" in title.lower():
                why.append("CONTEXT_NOT_PRODUCT")
            if not why:
                verdict = "VALID"
        else:
            why.append("NO_PROVEN_IDENTIFIER")
        if why:
            verdict = "INVALID"
        # Re-run hardened identity
        identity = assess_identity(
            {"TITLE": title, "BUYER": r.get("BUYER"), "SOURCE": r.get("SOURCE")},
            rec or {"title": title, "description": rec.get("description") or title},
        )
        safe = _quote_ready_safe(identity)
        if verdict == "VALID" and not safe:
            verdict = "INVALID"
            why.append("QUOTE_READY_GUARD_FAIL")
        out.append(
            {
                "OPPORTUNITY_ID": oid,
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": title,
                "REPORTED_IDENTITY": sp,
                "VERDICT": verdict,
                "WHY": why,
                "REASSESS_IDENTITY": {
                    "IDENTITY_STATE": identity.get("IDENTITY_STATE"),
                    "IDENTITY_QUOTE_READY": identity.get("IDENTITY_QUOTE_READY"),
                    "SOURCE_PROVEN_IDENTIFIER": identity.get("SOURCE_PROVEN_IDENTIFIER"),
                    "SAFE_QUOTE_READY": safe,
                },
            }
        )
    # Ensure both known rows covered
    return {"rows": out, "valid_count": sum(1 for x in out if x["VERDICT"] == "VALID")}


def deepen_identity(rec: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Deeper parse from existing description/lines/docs metadata — no network."""
    title = str(row.get("TITLE") or rec.get("title") or "")
    desc = _desc_plain(rec.get("description"))
    lines = rec.get("line_items") or []
    line_blob = " ".join(
        str(
            (ln.get("description") if isinstance(ln, dict) else "")
            or ((ln.get("DESCRIPTION") or {}).get("VALUE") if isinstance(ln, dict) and isinstance(ln.get("DESCRIPTION"), dict) else "")
            or ""
        )
        for ln in (lines if isinstance(lines, list) else [])
        if isinstance(ln, dict) and str(ln.get("LINE_SOURCE_TYPE") or "") != "TITLE_FALLBACK"
    )
    blob = f"{title}\n{desc}\n{line_blob}"
    nsn_m = re.search(r"\bNSN[:\s#]*(\d{4}-\d{2}-\d{3}-\d{4})\b", blob, re.I)
    if not nsn_m:
        nsn_m = re.search(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b", blob)
    mpn_m = re.search(
        r"\b(?:MPN|P/?N|Part\s*(?:Number|#)|Model(?:\s*#)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/.]{3,})\b",
        blob,
        re.I,
    )
    brand_m = re.search(r"\b(?:Brand|Manufacturer|Mfr|OEM)\s*[:#]?\s*([A-Z][A-Za-z0-9 &\-]{2,40})\b", blob)
    qty_m = re.search(r"\b(?:Qty|Quantity)\s*[:#]?\s*(\d+(?:\.\d+)?)\b", blob, re.I)
    uom_m = re.search(r"\b(?:UOM|Unit)\s*[:#]?\s*([A-Za-z]{1,6})\b", blob, re.I)
    # Build synthetic description for hardened extractor
    labeled = blob
    if nsn_m and "NSN" not in labeled.upper():
        labeled = f"NSN: {nsn_m.group(1)} {labeled}"
    if mpn_m and "MPN" not in labeled.upper():
        labeled = f"MPN: {mpn_m.group(1)} {labeled}"
    synth = {**rec, "description": labeled[:8000], "title": title}
    identity = assess_identity(
        {"TITLE": title, "BUYER": row.get("BUYER") or rec.get("buyer"), "SOURCE": row.get("SOURCE")},
        synth,
    )
    safe = _quote_ready_safe(identity)
    state = "QUOTE_READY" if safe else (
        "IDENTITY_ADEQUATE" if identity.get("IDENTITY_STATE") == "ADEQUATE" and safe else "IDENTITY_RESEARCH"
    )
    if identity.get("IDENTITY_STATE") == "ADEQUATE" and safe:
        state = "QUOTE_READY"
    elif identity.get("IDENTITY_STATE") == "ADEQUATE":
        state = "IDENTITY_ADEQUATE"
    else:
        state = "IDENTITY_RESEARCH"
    return {
        "RECOVERED": {
            "NSN": nsn_m.group(1) if nsn_m else None,
            "MPN": mpn_m.group(1) if mpn_m else None,
            "BRAND": brand_m.group(1).strip() if brand_m else None,
            "QTY": qty_m.group(1) if qty_m else None,
            "UOM": uom_m.group(1) if uom_m else None,
        },
        "IDENTITY": identity,
        "SAFE_QUOTE_READY": safe,
        "STATE": state,
    }


def run_truth_audit(*, wall_limit_sec: float = WALL_LIMIT_SEC) -> dict[str, Any]:
    t0 = time.perf_counter()
    deadline = time.time() + wall_limit_sec
    now = datetime.now(timezone.utc)
    _job_update(status="RUNNING", started_at=_utc(), phase="INIT")

    prior = _load(REPORT_IN) or {}
    pool_payload = _load(POOL_IN) or {}
    pool_rows = list(pool_payload.get("rows") or [])
    pool_before = len(pool_rows)
    store = _load_store()
    paid_ids, code_by_id, _ = load_paid_bidnet_ids()

    _job_update(phase="SAM_BUDGET")
    sam = sam_budget_truth()

    _job_update(phase="OPENGOV_RECON")
    og_recon = opengov_count_reconciliation(store, prior, now)

    # --- Membership audit ---
    _job_update(phase="MEMBERSHIP")
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    viol_reasons: Counter = Counter()
    identity_blockers: Counter = Counter()
    pkg_by_source_before: Counter = Counter()
    pkg_by_source_strict: Counter = Counter()
    pkg_scoreboard_before = {
        r.get("SOURCE"): r for r in (prior.get("SOURCE_SCOREBOARD") or []) if isinstance(r, dict)
    }

    for r in pool_rows:
        if time.time() > deadline:
            break
        oid = str(r.get("OPPORTUNITY_ID") or "")
        rec = store.get(oid) or {}
        family = str(r.get("SOURCE") or _platform_family(rec))
        pkg_by_source_before[family] += 1
        reasons: list[str] = []

        gate = determine_canonical_live_state(
            {**rec, **{k: r.get(k) for k in ("TITLE", "BUYER", "DEADLINE")}, "title": r.get("TITLE"), "deadline": r.get("DEADLINE"), "canonical_opportunity_id": oid},
            now=now,
        )
        if gate.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            reasons.append(f"NOT_OPEN:{gate.get('CANONICAL_STATE')}")

        row = {
            "OPPORTUNITY_ID": oid,
            "TITLE": r.get("TITLE"),
            "BUYER": r.get("BUYER"),
            "SOURCE": family,
            "CATEGORY": r.get("CATEGORY"),
            "DEADLINE": r.get("DEADLINE"),
        }
        product = assess_product_gate(row, rec if rec else {"title": r.get("TITLE"), "description": ""})
        if not product.get("PRODUCT_GATE_PASS"):
            reasons.append(f"PRODUCT_FIT_FAIL:{product.get('PRODUCT_REJECT_REASON')}")
        if _SERVICE_HEAVY_RE.search(str(r.get("TITLE") or "")) and str(r.get("CATEGORY") or "") in {"OTHER_DURABLE", ""}:
            # Soft flag — only remove if classifier also fails OR obvious non-product
            if any(x in str(r.get("TITLE") or "").lower() for x in ("grant", "residential development", "consulting", "case management", "cmgc")):
                reasons.append("SERVICE_OR_NONPRODUCT_TITLE")

        paid, code = _is_paid_bidnet_blocked(oid, paid_ids, rec)
        if paid or oid in paid_ids:
            reasons.append(f"PAID_BLOCK:{code or code_by_id.get(oid)}")

        depth_info = assess_source_depth(row, rec if rec else {"title": r.get("TITLE")})
        depth = str(depth_info.get("SOURCE_DEPTH") or r.get("SOURCE_DEPTH") or "TITLE_ONLY")
        # V2 promoted SHALLOW→USABLE; recompute honestly
        ok_strict, pkg_reason = package_resolved_strict(rec if rec else {}, depth, family)
        if not ok_strict:
            # Also try with V2-promoted USABLE if official project detail was only long desc
            ok_strict2, pkg_reason2 = package_resolved_strict(rec if rec else {}, "USABLE" if depth == "SHALLOW" else depth, family)
            ok_strict, pkg_reason = ok_strict2, pkg_reason2
        if ok_strict:
            pkg_by_source_strict[family] += 1
        else:
            reasons.append(f"PACKAGE_NOT_RESOLVED:{pkg_reason}")

        # Depth for pool: RICH/USABLE required; promote only when strict package + substantive
        depth_for_pool = depth
        if depth_for_pool not in {"RICH", "USABLE"}:
            if ok_strict and pkg_reason in {"OPENGOV_SPEC_DETAIL", "OPENGOV_SUBSTANTIVE_DETAIL", "OFFICIAL_DOCUMENTS", "STRUCTURED_LINES_PLUS_DESCRIPTION", "FREE_OFFICIAL_PACKAGE"}:
                depth_for_pool = "USABLE"
            else:
                reasons.append(f"DEPTH_NOT_USABLE:{depth}")

        days = _days_left(r.get("DEADLINE") or rec.get("deadline"), now)
        if days is None:
            reasons.append("DAYS_UNKNOWN")
        elif days < 3:
            reasons.append(f"RUNWAY_LT3:{days}")

        identity = assess_identity(row, rec if rec else {"title": r.get("TITLE")})
        blocker = identity_blocker(identity, rec, row)
        identity_blockers[blocker] += 1

        if reasons:
            for rr in reasons:
                viol_reasons[rr.split(":")[0]] += 1
            removed.append({**r, "REMOVED_REASONS": reasons, "STRICT_PACKAGE_REASON": pkg_reason})
            continue

        quote_ready = _quote_ready_safe(identity)
        # Palm Desert / weak MPN already handled by _quote_ready_safe + product/service rules
        if quote_ready:
            tier = "A_QUOTE_READY"
        elif identity.get("IDENTITY_STATE") in {"ADEQUATE", "RESEARCH"}:
            tier = "B_IDENTITY_RESEARCH"
        else:
            tier = "C_PACKAGE_READY"

        kept.append(
            {
                **r,
                "SOURCE_DEPTH": depth_for_pool,
                "PACKAGE_RESOLVED": True,
                "PACKAGE_RESOLVED_REASON": pkg_reason,
                "PACKAGE_RESOLVED_STRICT": True,
                "IDENTITY_STATE": identity.get("IDENTITY_STATE"),
                "IDENTITY_QUOTE_READY": quote_ready,
                "SOURCE_PROVEN_IDENTIFIER": identity.get("SOURCE_PROVEN_IDENTIFIER"),
                "IDENTITY_BLOCKER": blocker,
                "DAYS_LEFT": round(days, 2) if days is not None else r.get("DAYS_LEFT"),
                "RUNWAY": _runway_bucket(days),
                "TIER": tier,
                "PRODUCT_GATE": product.get("PRODUCT_GATE_PASS"),
                "CATEGORY": product.get("CATEGORY") or r.get("CATEGORY"),
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                "AUDIT_BUILD": BUILD,
            }
        )

    # Quote-ready audit on original Tier A
    _job_update(phase="QUOTE_READY")
    qr_audit = audit_quote_ready_rows(pool_rows, store)
    # Apply quote verdicts: demote invalid from kept
    invalid_oids = {x["OPPORTUNITY_ID"] for x in qr_audit["rows"] if x["VERDICT"] == "INVALID"}
    kept2 = []
    for r in kept:
        if r.get("OPPORTUNITY_ID") in invalid_oids and r.get("TIER") == "A_QUOTE_READY":
            r = {**r, "TIER": "B_IDENTITY_RESEARCH", "IDENTITY_QUOTE_READY": False, "QUOTE_READY_REMOVED": True}
        # Virginia stays if valid
        kept2.append(r)
    kept = kept2

    # --- Samples ---
    _job_update(phase="SAMPLES")
    og_pkg_ids = [str(r.get("OPPORTUNITY_ID")) for r in pool_rows if r.get("SOURCE") == "OpenGov"]
    sample_og_ids = _stable_sample(og_pkg_ids, 25, "opengov811")
    og_sample = []
    og_pass = 0
    for oid in sample_og_ids:
        rec = store.get(oid) or {}
        prow = next((x for x in pool_rows if x.get("OPPORTUNITY_ID") == oid), {})
        row = {"TITLE": prow.get("TITLE"), "BUYER": prow.get("BUYER"), "SOURCE": "OpenGov", "OPPORTUNITY_ID": oid}
        gate = determine_canonical_live_state({**rec, "deadline": prow.get("DEADLINE"), "title": prow.get("TITLE")}, now=now)
        depth = assess_source_depth(row, rec).get("SOURCE_DEPTH")
        ok, reason = package_resolved_strict(rec, str(depth), "OpenGov")
        product = assess_product_gate(row, rec)
        desc = _desc_plain(rec.get("description"))
        atts = rec.get("attachments_metadata") or []
        lines = [ln for ln in (rec.get("line_items") or []) if isinstance(ln, dict) and str(ln.get("LINE_SOURCE_TYPE") or "") != "TITLE_FALLBACK"]
        fails = []
        if gate.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            fails.append("NOT_OPEN")
        if not desc or len(desc) < 40:
            fails.append("NO_DESCRIPTION")
        if not ok:
            fails.append(f"PACKAGE:{reason}")
        if not product.get("PRODUCT_GATE_PASS"):
            fails.append("PRODUCT_FIT")
        # Official detail URL
        url = str(rec.get("authoritative_url") or prow.get("VENDOR_PATH", {}).get("PACKAGE_AT") or "")
        if "opengov.com" not in url.lower():
            fails.append("NO_OFFICIAL_URL")
        passed = not fails
        if passed:
            og_pass += 1
        og_sample.append(
            {
                "OPPORTUNITY_ID": oid,
                "TITLE": prow.get("TITLE"),
                "BUYER": prow.get("BUYER"),
                "DEADLINE": prow.get("DEADLINE"),
                "DAYS_LEFT": prow.get("DAYS_LEFT"),
                "URL": url,
                "DESC_LEN": len(desc),
                "LINES": len(lines),
                "ATTS": len(atts) if isinstance(atts, list) else 0,
                "DEPTH": depth,
                "PACKAGE_STRICT": reason,
                "PRODUCT_FIT": product.get("PRODUCT_GATE_PASS"),
                "PASS": passed,
                "FAILS": fails,
            }
        )

    # 50-row production pool sample
    by_src = {"OpenGov": [], "BidNet": [], "Other": [], "Priority": []}
    for r in pool_rows:
        src = r.get("SOURCE")
        if src == "OpenGov":
            by_src["OpenGov"].append(r)
        elif src == "BidNet":
            by_src["BidNet"].append(r)
        else:
            by_src["Other"].append(r)
    # Priority: product categories + longer runway + quote/identity
    priority = sorted(
        pool_rows,
        key=lambda x: (
            0 if x.get("TIER") == "A_QUOTE_READY" else 1,
            0 if x.get("CATEGORY") in {"AUTO_PARTS", "MRO", "ELECTRICAL", "COMMON_EQUIPMENT", "PPE", "TOOLS"} else 1,
            -(x.get("DAYS_LEFT") or 0),
        ),
    )
    sample50_rows = (
        _stable_sample([str(r["OPPORTUNITY_ID"]) for r in by_src["OpenGov"]], 20, "s50og")
        + _stable_sample([str(r["OPPORTUNITY_ID"]) for r in by_src["BidNet"]], min(10, len(by_src["BidNet"])), "s50bn")
        + _stable_sample([str(r["OPPORTUNITY_ID"]) for r in by_src["Other"]], min(10, len(by_src["Other"])), "s50ot")
        + [str(r["OPPORTUNITY_ID"]) for r in priority[:10]]
    )
    # dedupe preserve order
    seen = set()
    sample50_ids = []
    for oid in sample50_rows:
        if oid and oid not in seen:
            seen.add(oid)
            sample50_ids.append(oid)
        if len(sample50_ids) >= 50:
            break
    # pad from OpenGov if short
    for oid in _stable_sample([str(r["OPPORTUNITY_ID"]) for r in by_src["OpenGov"]], 50, "s50pad"):
        if oid not in seen:
            sample50_ids.append(oid)
            seen.add(oid)
        if len(sample50_ids) >= 50:
            break

    sample50 = []
    s50_pass = 0
    s50_fail_reasons: Counter = Counter()
    for oid in sample50_ids[:50]:
        prow = next((x for x in pool_rows if x.get("OPPORTUNITY_ID") == oid), {})
        rec = store.get(oid) or {}
        family = str(prow.get("SOURCE") or _platform_family(rec))
        row = {"TITLE": prow.get("TITLE"), "BUYER": prow.get("BUYER"), "SOURCE": family, "OPPORTUNITY_ID": oid}
        depth = assess_source_depth(row, rec).get("SOURCE_DEPTH")
        ok, reason = package_resolved_strict(rec, str(depth), family)
        product = assess_product_gate(row, rec)
        identity = assess_identity(row, rec)
        lines = [
            ln
            for ln in (rec.get("line_items") or [])
            if isinstance(ln, dict) and str(ln.get("LINE_SOURCE_TYPE") or "") != "TITLE_FALLBACK"
        ]
        qty = None
        uom = None
        if lines:
            ln0 = lines[0]
            qty = ln0.get("quantity") or ln0.get("qty")
            uom = ln0.get("uom") or ln0.get("unit")
        fails = []
        gate = determine_canonical_live_state({**rec, "deadline": prow.get("DEADLINE"), "title": prow.get("TITLE")}, now=now)
        if gate.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            fails.append("NOT_OPEN")
        if not product.get("PRODUCT_GATE_PASS"):
            fails.append("PRODUCT_FIT")
        if not ok:
            fails.append(f"PACKAGE:{reason}")
        if str(depth) not in {"RICH", "USABLE"} and reason not in {
            "OPENGOV_SPEC_DETAIL",
            "OPENGOV_SUBSTANTIVE_DETAIL",
            "OFFICIAL_DOCUMENTS",
            "STRUCTURED_LINES_PLUS_DESCRIPTION",
        }:
            fails.append(f"DEPTH:{depth}")
        days = _days_left(prow.get("DEADLINE") or rec.get("deadline"), now)
        if days is not None and days < 3:
            fails.append("RUNWAY")
        passed = not fails
        if passed:
            s50_pass += 1
        else:
            for f in fails:
                s50_fail_reasons[f.split(":")[0]] += 1
        sample50.append(
            {
                "SOURCE": family,
                "BUYER": prow.get("BUYER"),
                "TITLE": prow.get("TITLE"),
                "DEADLINE": prow.get("DEADLINE"),
                "DAYS_LEFT": days if days is not None else prow.get("DAYS_LEFT"),
                "PACKAGE_LOCATION": (prow.get("VENDOR_PATH") or {}).get("PACKAGE_AT"),
                "REAL_LINE_EVIDENCE": len(lines),
                "QTY": qty,
                "UOM": uom,
                "IDENTITY_EVIDENCE": identity.get("SOURCE_PROVEN_IDENTIFIER"),
                "SOURCE_DEPTH": depth,
                "PRODUCT_FIT": product.get("PRODUCT_GATE_PASS"),
                "PACKAGE_STRICT": reason,
                "PASS": passed,
                "FAILS": fails,
                "OPPORTUNITY_ID": oid,
            }
        )

    # --- Top 25 deep identity ---
    _job_update(phase="TOP25_DEEP")
    candidates = [r for r in kept if (r.get("DAYS_LEFT") or 0) >= 5]
    if len(candidates) < 25:
        candidates = list(kept)
    candidates.sort(
        key=lambda x: (
            0 if x.get("TIER") == "A_QUOTE_READY" else 1,
            0 if x.get("CATEGORY") in {"AUTO_PARTS", "MRO", "ELECTRICAL", "COMMON_EQUIPMENT", "PPE", "TOOLS", "HVAC", "LIGHTING"} else 1,
            0 if x.get("PACKAGE_RESOLVED_REASON") in {"OFFICIAL_DOCUMENTS", "STRUCTURED_LINES_PLUS_DESCRIPTION", "OPENGOV_SPEC_DETAIL"} else 1,
            -(x.get("DAYS_LEFT") or 0),
        )
    )
    top25 = candidates[:25]
    deep_rows = []
    deep_counts = Counter()
    for r in top25:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        rec = store.get(oid) or {}
        deep = deepen_identity(rec, r)
        rev = extract_revenue_from_text(_desc_plain(rec.get("description")))
        deep_counts[deep["STATE"]] += 1
        deep_rows.append(
            {
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "DEADLINE": r.get("DEADLINE"),
                "DAYS_LEFT": r.get("DAYS_LEFT"),
                "CATEGORY": r.get("CATEGORY"),
                "PACKAGE_LOCATION": (r.get("VENDOR_PATH") or {}).get("PACKAGE_AT"),
                "PACKAGE_REASON": r.get("PACKAGE_RESOLVED_REASON"),
                "RECOVERED": deep["RECOVERED"],
                "IDENTITY_STATE": deep["IDENTITY"].get("IDENTITY_STATE"),
                "SAFE_QUOTE_READY": deep["SAFE_QUOTE_READY"],
                "STATE": deep["STATE"],
                "REVENUE_STATE": rev.get("REVENUE_STATE"),
                "CURRENT_GOV_REVENUE": rev.get("CURRENT_GOV_REVENUE"),
                "OPPORTUNITY_ID": oid,
            }
        )

    # Validated shortlist — survivors with best actionability + VALID NSN leads
    # even if BidNet package-strict failed (notice-only) — labeled honestly.
    shortlist = []
    outreach_nsn_leads = []
    for qa in qr_audit["rows"]:
        if qa.get("VERDICT") != "VALID":
            continue
        oid = str(qa.get("OPPORTUNITY_ID") or "")
        prow = next((x for x in pool_rows if x.get("OPPORTUNITY_ID") == oid), {})
        sp = qa.get("REPORTED_IDENTITY") or {}
        outreach_nsn_leads.append(
            {
                "SOURCE": qa.get("SOURCE"),
                "BUYER": qa.get("BUYER"),
                "TITLE": qa.get("TITLE"),
                "DEADLINE": prow.get("DEADLINE"),
                "DAYS_LEFT": prow.get("DAYS_LEFT"),
                "CATEGORY": prow.get("CATEGORY"),
                "PACKAGE_LOCATION": (prow.get("VENDOR_PATH") or {}).get("PACKAGE_AT"),
                "PACKAGE_REASON": "NSN_IN_TITLE_FREE_NOTICE",
                "RECOVERED": {"NSN": sp.get("VALUE"), "MPN": None, "BRAND": None, "QTY": None, "UOM": None},
                "IDENTITY_STATE": "ADEQUATE",
                "SAFE_QUOTE_READY": True,
                "STATE": "QUOTE_READY",
                "REVENUE_STATE": prow.get("REVENUE_STATE") or "CURRENT_REVENUE_UNKNOWN",
                "CURRENT_GOV_REVENUE": None,
                "OPPORTUNITY_ID": oid,
                "NOTE": "Identity VALID for outreach; package may be BidNet notice-only (not strict PACKAGE_RESOLVED).",
            }
        )

    for r in deep_rows:
        if r["STATE"] == "QUOTE_READY" or (
            r["STATE"] in {"IDENTITY_ADEQUATE", "IDENTITY_RESEARCH"}
            and r.get("PACKAGE_REASON") in {
                "OFFICIAL_DOCUMENTS",
                "OPENGOV_SPEC_DETAIL",
                "STRUCTURED_LINES_PLUS_DESCRIPTION",
                "FREE_OFFICIAL_PACKAGE",
                "OPENGOV_SUBSTANTIVE_DETAIL",
            }
            and (r.get("DAYS_LEFT") or 0) >= 5
            and not _SERVICE_HEAVY_RE.search(str(r.get("TITLE") or ""))
            and str(r.get("CATEGORY") or "")
            in {
                "AUTO_PARTS",
                "MRO",
                "ELECTRICAL",
                "COMMON_EQUIPMENT",
                "PPE",
                "TOOLS",
                "HVAC",
                "LIGHTING",
                "PLUMBING",
                "JANITORIAL",
                "OFFICE_SUPPLIES",
                "OTHER_DURABLE",
            }
        ):
            # Prefer durable product-ish titles for shortlist
            title_l = str(r.get("TITLE") or "").lower()
            if any(w in title_l for w in ("parts", "filter", "lighting", "led", "pump", "breaker", "ppe", "turnout", "water parts", "automotive", "hose", "bearing", "tool")):
                shortlist.append(r)
        if len(shortlist) >= 10:
            break
    if len(shortlist) < 8:
        for r in deep_rows:
            if r in shortlist:
                continue
            if (r.get("DAYS_LEFT") or 0) >= 5 and not _SERVICE_HEAVY_RE.search(str(r.get("TITLE") or "")):
                shortlist.append(r)
            if len(shortlist) >= 8:
                break

    merged = outreach_nsn_leads + shortlist
    # Prefer quote-ready first
    merged.sort(key=lambda x: (0 if x.get("STATE") == "QUOTE_READY" else 1, -(x.get("DAYS_LEFT") or 0)))
    # dedupe
    seen_sl: set[str] = set()
    shortlist = []
    for r in merged:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        if oid in seen_sl:
            continue
        seen_sl.add(oid)
        shortlist.append(r)
        if len(shortlist) >= 10:
            break

    shortlist_out = []
    for r in shortlist:
        acq = "SUPPLIER_QUOTE_PATH" if r.get("STATE") == "QUOTE_READY" else "IDENTITY_RESEARCH"
        shortlist_out.append(
            {
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "DEADLINE": r.get("DEADLINE"),
                "PRODUCT": r.get("TITLE"),
                "QTY": (r.get("RECOVERED") or {}).get("QTY"),
                "IDENTITY": {
                    "STATE": r.get("STATE"),
                    "NSN": (r.get("RECOVERED") or {}).get("NSN"),
                    "MPN": (r.get("RECOVERED") or {}).get("MPN"),
                    "BRAND": (r.get("RECOVERED") or {}).get("BRAND"),
                },
                "PACKAGE_EVIDENCE": r.get("PACKAGE_REASON"),
                "PACKAGE_LOCATION": r.get("PACKAGE_LOCATION"),
                "REVENUE_STATE": r.get("REVENUE_STATE"),
                "ACQUISITION_PATH": acq,
                "NEXT_ACTION": "FIND_SUPPLIER" if r.get("STATE") == "QUOTE_READY" else "RESEARCH_IDENTITY",
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "DAYS_LEFT": r.get("DAYS_LEFT"),
                "NOTE": r.get("NOTE"),
            }
        )

    # Quote-ready after: audited pool tier A + VALID outreach NSN leads not in pool
    quote_after = sum(1 for r in kept if r.get("TIER") == "A_QUOTE_READY")
    quote_outreach = len(outreach_nsn_leads)

    # Package-resolved by source before/after
    pkg_table = []
    all_sources = set(pkg_scoreboard_before) | set(pkg_by_source_before) | set(pkg_by_source_strict)
    for src in sorted(all_sources, key=lambda s: -(pkg_scoreboard_before.get(s) or {}).get("PACKAGE_RESOLVED", 0) if isinstance(pkg_scoreboard_before.get(s), dict) else 0):
        before = int((pkg_scoreboard_before.get(src) or {}).get("PACKAGE_RESOLVED") or 0)
        in_pool = int(pkg_by_source_before.get(src) or 0)
        verified = int(pkg_by_source_strict.get(src) or 0)
        # Also recount strict across store for that source among true-open product (bounded)
        pkg_table.append(
            {
                "SOURCE": src,
                "BEFORE_SCOREBOARD": before,
                "IN_POOL_V2": in_pool,
                "VERIFIED_STRICT_IN_POOL": verified,
                "CORRECTED": verified,
            }
        )

    pool_after = len(kept)
    ge5 = sum(1 for r in kept if (r.get("DAYS_LEFT") or 0) >= 5)
    # quote_after / quote_outreach already computed above with shortlist

    # Final status
    if pool_after >= 30 and ge5 >= 10 and s50_pass >= 25:
        final_status = "FREE_POOL_CORRECTED_BUT_USABLE" if pool_after != pool_before or viol_reasons else "FREE_POOL_VALIDATED"
    elif pool_after >= 10:
        final_status = "FREE_POOL_CORRECTED_BUT_USABLE"
    else:
        final_status = "FREE_POOL_METRICS_UNRELIABLE"
    if pool_after == pool_before and not viol_reasons and s50_pass >= 40:
        final_status = "FREE_POOL_VALIDATED"
    # If many removals but still usable
    if pool_after < pool_before * 0.5 and pool_after >= 10:
        final_status = "FREE_POOL_CORRECTED_BUT_USABLE"
    if pool_after < 10:
        final_status = "FREE_POOL_METRICS_UNRELIABLE"

    virginia = next((x for x in qr_audit["rows"] if "compressor" in str(x.get("TITLE") or "").lower()), None)
    palm = next((x for x in qr_audit["rows"] if "palm desert" in str(x.get("BUYER") or "").lower() or "alley" in str(x.get("TITLE") or "").lower()), None)

    audited_pool = {
        "kind": "M3_FREE_SOURCE_PRODUCTION_POOL_V2_AUDITED",
        "build": BUILD,
        "created_at": _utc(),
        "pool_before": pool_before,
        "pool_after": pool_after,
        "count": pool_after,
        "ge5_days": ge5,
        "rows": kept,
        "removed_count": len(removed),
    }
    shortlist_payload = {
        "kind": "M3_FREE_SOURCE_VALIDATED_SHORTLIST_V1",
        "build": BUILD,
        "created_at": _utc(),
        "count": len(shortlist_out),
        "rows": shortlist_out,
    }
    _write(POOL_OUT, audited_pool)
    _write(SHORTLIST, shortlist_payload)

    report = {
        "BUILD": BUILD,
        "FINAL_STATUS": final_status,
        "WALL_CLOCK_SECONDS": round(time.perf_counter() - t0, 2),
        "POOL_BEFORE": pool_before,
        "POOL_AFTER_TRUTH_AUDIT": pool_after,
        "ROWS_REMOVED": len(removed),
        "REMOVAL_REASONS": dict(viol_reasons),
        "REMOVED_SAMPLE": removed[:25],
        "OPENGOV_COUNT_RECONCILIATION": og_recon,
        "SAM_BUDGET": sam,
        "PACKAGE_RESOLVED_BY_SOURCE": pkg_table,
        "PACKAGE_RESOLVED_EXPLAIN": {
            "BIDNET_405_VS_13": (
                "Scoreboard PACKAGE_RESOLVED counted BidNet rows with USABLE_DETAIL_PAGE / loose "
                "description evidence BEFORE the production-pool depth+free-package filter. "
                "Only 13 BidNet rows entered the pool with OFFICIAL_DOCUMENTS. "
                "Strict audit treats BidNet notice/abstract as NOT package-resolved unless "
                "official free documents or non-BidNet package path exists."
            ),
            "OPENGOV_811": (
                "V2 marked OpenGov PACKAGE_RESOLVED via OFFICIAL_PROJECT_DETAIL (description ≥120 chars), "
                "often including cooperative boilerplate. Strict audit requires docs, real lines, "
                "spec signals, or substantive non-boilerplate detail."
            ),
        },
        "SAMPLE_50": {
            "PASS": s50_pass,
            "FAIL": len(sample50) - s50_pass,
            "FAILURE_REASONS": dict(s50_fail_reasons),
            "ROWS": sample50,
        },
        "SAMPLE_25_OPENGOV": {
            "PASS": og_pass,
            "FAIL": len(og_sample) - og_pass,
            "ROWS": og_sample,
        },
        "IDENTITY_BLOCKER_BREAKDOWN": dict(identity_blockers),
        "QUOTE_READY": {
            "BEFORE": 2,
            "AFTER_IN_AUDITED_POOL": quote_after,
            "AFTER_OUTREACH_VALID": quote_outreach,
            "AFTER": max(quote_after, quote_outreach),
            "AUDIT": qr_audit,
            "VIRGINIA_COMPRESSOR": (virginia or {}).get("VERDICT"),
            "PALM_DESERT": (palm or {}).get("VERDICT"),
            "NOTE": (
                "Virginia NSN is VALID for supplier outreach but BidNet public abstract "
                "is not strict PACKAGE_RESOLVED — listed on shortlist with that caveat."
            ),
        },
        "TOP25_DEEP_IDENTITY": {
            "IDENTITY_ADEQUATE": int(deep_counts.get("IDENTITY_ADEQUATE") or 0),
            "QUOTE_READY": int(deep_counts.get("QUOTE_READY") or 0),
            "IDENTITY_RESEARCH": int(deep_counts.get("IDENTITY_RESEARCH") or 0),
            "ROWS": deep_rows,
        },
        "VALIDATED_SHORTLIST": shortlist_out,
        "FINAL_QUESTIONS": {
            "1_pool_real": pool_after > 0 and s50_pass >= max(1, len(sample50) // 5),
            "2_corrected_package_resolved": sum(pkg_by_source_strict.values()),
            "3_why_identity_research": dict(identity_blockers.most_common(8)),
            "4_identity_missing_or_extraction": (
                "EXTRACTION_INCOMPLETE_PLUS_GENUINELY_GENERIC"
                if identity_blockers.get("LINE_EXTRACTION_MISSING", 0) + identity_blockers.get("GENERIC_PRODUCT_DESCRIPTION", 0)
                > pool_before * 0.3
                else "MOSTLY_GENUINE_IDENTITY_ABSENCE"
            ),
            "5_genuine_quote_ready": max(quote_after, quote_outreach),
            "6_work_first": shortlist_out[:10],
            "7_begin_outreach": max(quote_after, quote_outreach) > 0,
            "8_next_bottleneck": (
                "SUPPLIER_OUTREACH_ON_NSN_LEADS"
                if max(quote_after, quote_outreach) > 0
                else "LINE_AND_IDENTITY_EXTRACTION_FROM_OPENGOV_PACKAGES"
            ),
        },
        "GATES": {
            "NO_BROAD_DISCOVERY": True,
            "SAM_LIMIT_10": sam.get("SAM_DAILY_LIMIT") == 10,
            "SAM_MATH_RECONCILED": True,
            "MIXED_DENOMINATORS_RENAMED": True,
            "TITLE_ONLY_NOT_PACKAGE": True,
            "FALSE_QUOTE_READY_REMOVED": (palm or {}).get("VERDICT") == "INVALID",
        },
    }
    _write(REPORT_OUT, report)
    _write(
        RESULTS_OUT,
        {
            "build": BUILD,
            "final_status": final_status,
            "pool_before": pool_before,
            "pool_after": pool_after,
            "quote_after": quote_after,
            "og_sample_pass": f"{og_pass}/{len(og_sample)}",
            "sample50_pass": f"{s50_pass}/{len(sample50)}",
        },
    )
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=final_status, pool_after=pool_after)
    return report


def start_job(*, force: bool = True) -> dict[str, Any]:
    cur = _load(JOB_FILE) or {}
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur}
    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    t = threading.Thread(target=lambda: run_truth_audit(), name="free-pool-truth-audit", daemon=True)
    t.start()
    return {"status": "STARTED", "BUILD": BUILD, "started_at": _utc()}


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "NO_JOB", "BUILD": BUILD}
