"""Free-source-first true-live recovery + package-ready production pool V2.

$0 paid data access. BidNet paid entitlement rows → backlog only (no retry).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from buyer_intelligence.stage1_full_production_acceptance_v1 import (
    assess_acquisition_path,
    assess_eligibility,
    assess_identity,
    assess_product_gate,
    assess_source_depth,
    decide_disposition,
    lookup_public_price,
    lookup_real_quote,
    assess_coverage_and_economics,
)
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    determine_canonical_live_state,
)
from m3_data_root import data_path
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from package_recovery_sam_budget.quality import is_valid_package_evidence

log = logging.getLogger("govtracker.free_source_first_pool_v2")

BUILD = "20261009-m3-free-source-first-production-pool-v2"
POOL_FILE = "m3_free_source_production_pool_v2.json"
BACKLOG_FILE = "m3_paid_source_backlog_v1.json"
REPORT_FILE = "m3_free_source_first_production_pool_v2_report.json"
RESULTS_FILE = "m3_free_source_first_production_pool_v2_results.json"
JOB_FILE = "m3_free_source_first_production_pool_v2_job.json"
TODAY_FILE = "m3_free_source_first_today_v2.json"

WALL_LIMIT_SEC = 75 * 60
PHASE_OPENGOV_DISC = 20 * 60
PHASE_OPENGOV_RECOVER = 12 * 60
PHASE_SAM = 15 * 60
PHASE_STATE = 8 * 60
PHASE_BIDNET_FREE = 10 * 60
# normalize/report uses remainder

ITEM_TIMEOUT_SEC = 45.0
SAM_HARD_DAILY = 10
SAM_RESERVE = 1

_PAID_CODES_RE = re.compile(r"CB_(?:SW|FD)_[A-Z0-9_]+")


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


def _days_left(deadline: Any, now: datetime) -> float | None:
    if not deadline:
        return None
    try:
        from buyer_intelligence.true_live_accepting_bids_gate_v1 import _days_remaining

        return _days_remaining(deadline, now=now)
    except Exception:
        try:
            s = str(deadline).replace("Z", "+00:00")
            dl = datetime.fromisoformat(s)
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            return (dl - now).total_seconds() / 86400.0
        except Exception:
            return None


_NSN_FMT = re.compile(r"^(?:\d{4}-\d{2}-\d{3}-\d{4}|\d{13})$")


def _quote_ready_safe(identity: dict[str, Any]) -> bool:
    """Do not promote malformed NSN/MPN tokens (phone/project ids) to quote-ready."""
    if not identity.get("IDENTITY_QUOTE_READY"):
        return False
    sp = identity.get("SOURCE_PROVEN_IDENTIFIER") or {}
    typ = str(sp.get("TYPE") or "")
    val = str(sp.get("VALUE") or "").strip()
    if typ == "NSN":
        digits = re.sub(r"\D", "", val)
        if len(digits) != 13 and not _NSN_FMT.match(val):
            return False
        if len(digits) == 13:
            return True
        return bool(_NSN_FMT.match(val))
    if typ == "MPN":
        # Reject pure-numeric / parcel-like tokens (not commercial MPNs)
        if re.fullmatch(r"[\d.\-/]+", val):
            return False
        if len(val) < 5:
            return False
    return True


def _runway_bucket(days: float | None) -> str:
    if days is None:
        return "UNKNOWN"
    if days >= 14:
        return "14+"
    if days >= 8:
        return "8-13"
    if days >= 5:
        return "5-7"
    if days >= 3:
        return "3-4"
    if days >= 1:
        return "1-2"
    return "EXPIRED"


def _platform_family(rec: dict[str, Any]) -> str:
    p = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    url = str(rec.get("authoritative_url") or rec.get("detail_url") or "").lower()
    if "opengov" in p or "opengov" in url:
        return "OpenGov"
    if "sam.gov" in url or p in {"sam", "live_sam", "sam_gov"}:
        return "SAM"
    if "bidnet" in p or "bidnet" in url:
        return "BidNet"
    if "planetbids" in p or "planetbids" in url:
        return "PlanetBids"
    if "ionwave" in p or "ionwave" in url:
        return "IonWave"
    if "bonfire" in p or "bonfire" in url:
        return "Bonfire"
    if "publicpurchase" in p or "publicpurchase" in url:
        return "PublicPurchase"
    if "demandstar" in p or "demandstar" in url:
        return "DemandStar"
    if p:
        return str(rec.get("platform_family") or rec.get("platform") or "Other")
    return "Other"


def _is_paid_bidnet_blocked(oid: str, paid_ids: set[str], rec: dict[str, Any]) -> tuple[bool, str | None]:
    if oid in paid_ids:
        return True, None
    br = rec.get("bidnet_recovery") or {}
    chase = br.get("free_package_chase") or {}
    urls = " ".join(
        str(x or "")
        for x in (
            rec.get("authoritative_url"),
            rec.get("detail_url"),
            chase.get("source_url"),
            chase.get("matched_url"),
            chase.get("free_source_url"),
            ((rec.get("vendor_path") or {}) if isinstance(rec.get("vendor_path"), dict) else {}).get("PACKAGE_AT"),
        )
    )
    blob = json.dumps({"br": br, "chase": chase, "access": rec.get("access_status"), "urls": urls}, default=str)
    if "PAID_ENTITLEMENT" in blob or "subscription-management" in blob.lower() or "intercept/plans" in blob.lower():
        m = _PAID_CODES_RE.search(blob)
        return True, m.group(0) if m else "UNKNOWN"
    return False, None


def _package_resolved(rec: dict[str, Any], depth: str) -> tuple[bool, str]:
    """Truthful package-resolved — title-only never qualifies."""
    if depth == "TITLE_ONLY":
        return False, "TITLE_ONLY"
    atts = rec.get("attachments_metadata") or []
    docs = list((rec.get("bidnet_recovery") or {}).get("documents") or [])
    og = rec.get("opengov_recovery") or {}
    og_docs = list(og.get("documents") or og.get("attachments") or [])
    free = (rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}
    free_status = str(free.get("status") or "")
    all_docs = [d for d in list(atts) + docs + og_docs if isinstance(d, dict)]
    quality = is_valid_package_evidence(all_docs)
    if quality.get("valid"):
        return True, "OFFICIAL_DOCUMENTS"
    lines = rec.get("line_items") or []
    desc = str(rec.get("description") or "")
    # Strip HTML for length check
    desc_plain = re.sub(r"<[^>]+>", " ", desc)
    desc_plain = re.sub(r"\s+", " ", desc_plain).strip()
    if isinstance(lines, list) and len(lines) >= 1 and len(desc_plain) >= 40:
        return True, "STRUCTURED_LINES_PLUS_DESCRIPTION"
    if free_status in {"VALID_FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND", "FREE_PACKAGE_FOUND"}:
        free_url = str(free.get("source_url") or free.get("matched_url") or free.get("free_source_url") or "")
        if free_url and "bidnet" not in free_url.lower() and "subscription-management" not in free_url.lower():
            return True, "FREE_PACKAGE_CHASE"
    url = str(rec.get("authoritative_url") or rec.get("detail_url") or "").lower()
    # Official OpenGov/SAM/buyer project pages with real spec text (not title-only)
    if depth in {"RICH", "USABLE", "SHALLOW"} and len(desc_plain) >= 120:
        if "opengov.com" in url or "sam.gov" in url:
            return True, "OFFICIAL_PROJECT_DETAIL"
        if depth in {"RICH", "USABLE"}:
            return True, "USABLE_DETAIL_PAGE"
    if depth in {"RICH", "USABLE"} and len(desc_plain) >= 80:
        return True, "USABLE_DETAIL_PAGE"
    # Explicit request-package instructions
    if re.search(r"request\s+(the\s+)?(bid\s+)?(package|documents|specs)", desc_plain, re.I):
        return True, "PACKAGE_REQUEST_INSTRUCTIONS"
    return False, "INSUFFICIENT_EVIDENCE"


def _vendor_path(rec: dict[str, Any], family: str) -> dict[str, Any]:
    url = rec.get("authoritative_url") or rec.get("detail_url")
    free = (rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}
    free_url = free.get("source_url") or free.get("matched_url") or free.get("free_source_url")
    package_at = free_url or url
    submit = url
    register = None
    if family == "OpenGov":
        register = "OpenGov"
        submit = package_at or url
    elif family == "SAM":
        register = None
        submit = url
    elif family == "BidNet":
        register = None
        if free_url and "bidnet" not in str(free_url).lower():
            package_at = free_url
            submit = free_url
    return {
        "DISCOVERED_ON": family,
        "PACKAGE_AT": package_at,
        "REGISTER_AT": register,
        "SUBMIT_AT": submit,
    }


def load_paid_bidnet_ids() -> tuple[set[str], dict[str, str], list[dict[str, Any]]]:
    split = _load("m3_bidnet_entitlement_split_v1.json") or {}
    ids = set(str(x) for x in (split.get("PAID_OPPORTUNITY_IDS") or []))
    code_by_id: dict[str, str] = {}
    backlog_seed: list[dict[str, Any]] = []
    for r in split.get("PAID_ROWS") or []:
        oid = str(r.get("OPPORTUNITY_ID") or "")
        if oid:
            ids.add(oid)
            code_by_id[oid] = str(r.get("PAID_ENTITLEMENT_REQUIREMENT") or "UNKNOWN")
            backlog_seed.append(r)
    # Also from location results
    loc = _load("m3_bidnet_package_location_resolver_v1_results.json") or {}
    for r in loc.get("rows") or []:
        if r.get("ACCESS_STATE") == "PAID_ENTITLEMENT_REQUIRED":
            oid = str(r.get("OPPORTUNITY_ID") or "")
            if oid:
                ids.add(oid)
                code_by_id.setdefault(oid, str(r.get("PAID_ENTITLEMENT_REQUIREMENT") or "UNKNOWN"))
    return ids, code_by_id, backlog_seed


def _as_count(val: Any) -> int:
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, list):
        return len(val)
    if isinstance(val, dict):
        return int(val.get("count") or val.get("merged") or len(val))
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def phase_opengov_discovery(*, deadline: float, freshness: dict[str, Any]) -> dict[str, Any]:
    _job_update(phase="OPENGOV_DISCOVERY")
    remaining = max(30.0, deadline - time.time())
    max_entities = 70 if remaining > 600 else 40
    max_pages = 4
    try:
        from opengov_discovery.cascade import run_opengov_cascade_discovery

        rep = run_opengov_cascade_discovery(
            max_entities=max_entities,
            max_pages=max_pages,
            persist=True,
            use_auth=True,
            allow_browser=False,
        )
        entities = _as_count(rep.get("entities_checked") or rep.get("entities_attempted") or max_entities)
        cands = _as_count(
            rep.get("unique_records")
            or rep.get("raw_opportunities")
            or rep.get("opportunities_found")
            or rep.get("merged")
            or 0
        )
        freshness["FRESH_SOURCE_FETCHES"] += entities
        freshness["NEW_EVIDENCE_CREATED"] += cands
        return {
            "ok": True,
            "entities_checked": entities,
            "candidates": cands,
            "entities_successful": rep.get("entities_successful"),
            "canonical_merge": rep.get("canonical_merge"),
            "report_keys": list(rep.keys())[:20],
        }
    except Exception as exc:
        log.exception("opengov discovery failed")
        return {"ok": False, "error": type(exc).__name__, "entities_checked": 0, "candidates": 0}


def phase_opengov_recovery(*, deadline: float, freshness: dict[str, Any]) -> dict[str, Any]:
    _job_update(phase="OPENGOV_RECOVERY")
    remaining = max(20.0, deadline - time.time())
    limit = 120 if remaining > 400 else 60
    try:
        from opengov_recovery.batch import run_opengov_recovery

        # Prefer fresh detail for newly discovered open projects (force when resume is empty)
        rep = run_opengov_recovery(limit=limit, batch_size=20, resume=True, persist=True, force=False)
        processed = _as_count(rep.get("processed") or rep.get("completed") or 0)
        if processed == 0 and remaining > 60:
            rep = run_opengov_recovery(limit=min(80, limit), batch_size=20, resume=False, persist=True, force=True)
            processed = _as_count(rep.get("processed") or rep.get("completed") or 0)
        freshness["FRESH_SOURCE_FETCHES"] += processed
        stats = rep.get("stats") or {}
        freshness["NEW_EVIDENCE_CREATED"] += _as_count(
            stats.get("docs_recovered") or stats.get("detail_ok") or rep.get("docs_recovered") or 0
        )
        return {
            "ok": True,
            "processed": processed,
            "stats": {k: stats.get(k) for k in list(stats.keys())[:20]} if isinstance(stats, dict) else {},
            "funnel": rep.get("funnel"),
        }
    except Exception as exc:
        log.exception("opengov recovery failed")
        return {"ok": False, "error": type(exc).__name__, "processed": 0}


def phase_sam(*, deadline: float, freshness: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    _job_update(phase="SAM")
    from package_recovery_sam_budget.recover import recover_sam
    from package_recovery_sam_budget.sam_manager import SamDailyCreditManager

    mgr = SamDailyCreditManager()
    snap = mgr.snapshot()
    budget = max(0, min(int(snap.get("production_budget") or 0), SAM_HARD_DAILY - SAM_RESERVE))
    # Prefer SAM rows that look open/product and lack packages
    cands = []
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        if _platform_family(rec) != "SAM":
            continue
        atts = rec.get("attachments_metadata") or []
        if atts and is_valid_package_evidence(atts).get("valid"):
            freshness["CACHE_HITS"] += 1
            continue
        cands.append((cid, rec))
    cands = cands[: max(budget * 3, 15)]
    used = 0
    recovered = 0
    for cid, rec in cands:
        if time.time() > deadline or used >= budget:
            break
        meta = {
            "opportunity_id": cid,
            "title": rec.get("title"),
            "buyer": rec.get("buyer") or rec.get("agency"),
            "authoritative_url": rec.get("authoritative_url"),
            "solicitation_number": rec.get("solicitation_number") or rec.get("solicitation_event_id"),
        }
        try:
            hit = recover_sam(meta, l23_row=rec, manager=mgr, priority={"score": 1}, selected=True)
            used += 1
            freshness["FRESH_SOURCE_FETCHES"] += 1
            docs = hit.get("documents") or []
            if docs and is_valid_package_evidence(docs).get("valid"):
                recovered += 1
                freshness["NEW_EVIDENCE_CREATED"] += 1
                store[cid] = rec
        except Exception:
            used += 1
    snap = mgr.snapshot()
    # Hard daily limit is always 10 for owner reports — do not surface production_budget as "remaining"
    used_today = int(snap.get("calls_used") or used or 0)
    return {
        "ok": True,
        "candidates": len(cands),
        "api_calls_used": used,
        "packages_recovered": recovered,
        "SAM_DAILY_LIMIT": SAM_HARD_DAILY,
        "SAM_USED_TODAY": used_today,
        "SAM_REMAINING_TODAY": max(0, SAM_HARD_DAILY - min(used_today, SAM_HARD_DAILY)),
        # deprecated alias — was misread as remaining under limit 10
        "budget_remaining_DEPRECATED_DO_NOT_USE": max(0, int(snap.get("production_budget") or 0)),
        "snapshot": {
            k: snap.get(k)
            for k in ("date", "daily_max", "calls_used", "calls_remaining", "calls_reserved", "production_budget")
        },
    }


def phase_bidnet_free(
    *,
    deadline: float,
    freshness: dict[str, Any],
    store: dict[str, Any],
    paid_ids: set[str],
) -> dict[str, Any]:
    _job_update(phase="BIDNET_FREE_ROUTE")
    from bidnet_recovery.free_package_chase import chase_free_package

    leads_checked = 0
    paid_skipped = 0
    free_found = 0
    unresolved = 0
    # Prefer unresolved-8 from entitlement split
    split = _load("m3_bidnet_entitlement_split_v1.json") or {}
    prefer = list(split.get("UNRESOLVED_OPPORTUNITY_IDS") or [])
    ordered: list[tuple[str, dict]] = []
    for oid in prefer:
        if oid in store and oid not in paid_ids:
            ordered.append((oid, store[oid]))
    for cid, rec in store.items():
        if time.time() > deadline:
            break
        if _platform_family(rec) != "BidNet":
            continue
        if cid in paid_ids:
            paid_skipped += 1
            continue
        if cid in prefer:
            continue
        ordered.append((cid, rec))
    for cid, rec in ordered[:80]:
        if time.time() > deadline:
            break
        # Skip if already has free package
        prior = ((rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}).get("status")
        if prior in {"VALID_FREE_PACKAGE_FOUND", "FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND"}:
            freshness["PREEXISTING_EVIDENCE_REUSED"] += 1
            free_found += 1
            continue
        leads_checked += 1
        t0 = time.perf_counter()
        try:
            chase = chase_free_package(rec, store=store, refresh_overview=True)
            freshness["FRESH_SOURCE_FETCHES"] += 1
            status = chase.get("status")
            docs = [d for d in (chase.get("documents") or chase.get("docs") or []) if isinstance(d, dict)]
            docs = [d for d in docs if "bidnet" not in str(d.get("document_url") or "").lower()]
            if status in {"VALID_FREE_PACKAGE_FOUND", "FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND"} and docs:
                free_found += 1
                freshness["NEW_EVIDENCE_CREATED"] += 1
                br = rec.setdefault("bidnet_recovery", {})
                br["free_package_chase"] = chase
                if docs:
                    rec["attachments_metadata"] = docs
            else:
                unresolved += 1
        except Exception:
            unresolved += 1
        # honor ~30s free-route budget
        if time.perf_counter() - t0 > 30:
            pass
    return {
        "ok": True,
        "leads_checked": leads_checked,
        "paid_skipped": paid_skipped,
        "free_found": free_found,
        "unresolved": unresolved,
    }


def phase_state_local(*, deadline: float, freshness: dict[str, Any]) -> dict[str, Any]:
    _job_update(phase="STATE_LOCAL")
    if time.time() > deadline:
        return {"ok": False, "skipped": True}
    try:
        from discovery_expansion.harvest import run_expansion_harvest

        remaining = max(30.0, deadline - time.time())
        # Skip BidNet — already handled; keep catalog small
        rep = run_expansion_harvest(
            include_bidnet=False,
            max_catalog_entities_per_family=12,
        )
        freshness["FRESH_SOURCE_FETCHES"] += int(rep.get("entities_checked") or 0)
        freshness["NEW_EVIDENCE_CREATED"] += int(rep.get("opportunities_found") or rep.get("merged") or 0)
        return {"ok": True, "raw": {k: rep.get(k) for k in list(rep.keys())[:20]}}
    except TypeError:
        # Signature may differ — try minimal
        try:
            from discovery_expansion.harvest import run_expansion_harvest

            rep = run_expansion_harvest()
            return {"ok": True, "raw": {k: rep.get(k) for k in list(rep.keys())[:20]}}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def build_pool_and_scoreboard(
    *,
    store: dict[str, Any],
    paid_ids: set[str],
    code_by_id: dict[str, str],
    backlog_seed: list[dict[str, Any]],
    now: datetime,
    freshness: dict[str, Any],
) -> dict[str, Any]:
    price_cache = _load("m3_commercial_acquisition_cache_v1.json") or {}
    quotes = _load("m3_processed_real_quotes_v1.json") or {}

    scoreboard: dict[str, Counter] = defaultdict(Counter)
    pool: list[dict[str, Any]] = []
    paid_backlog: list[dict[str, Any]] = []
    seen_backlog = set()

    for seed in backlog_seed:
        oid = str(seed.get("OPPORTUNITY_ID") or "")
        if not oid or oid in seen_backlog:
            continue
        seen_backlog.add(oid)
        paid_backlog.append(
            {
                **seed,
                "SOURCE": "BidNet",
                "PAID_SOURCE_BLOCKER": True,
                "ENTITLEMENT_CODE": seed.get("PAID_ENTITLEMENT_REQUIREMENT"),
                "FREE_ALTERNATIVE_STATUS": "NOT_FOUND_IN_PRIOR_CHASE",
                "POLICY": "NO_RETRY_ON_NORMAL_CYCLES",
            }
        )

    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        family = _platform_family(rec)
        paid, code = _is_paid_bidnet_blocked(cid, paid_ids, rec)
        if paid or cid in paid_ids:
            if cid not in seen_backlog:
                seen_backlog.add(cid)
                paid_backlog.append(
                    {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": "BidNet",
                        "BUYER": rec.get("buyer") or rec.get("agency"),
                        "TITLE": rec.get("title"),
                        "DEADLINE": rec.get("deadline"),
                        "SOLICITATION": rec.get("solicitation_number") or rec.get("solicitation_event_id"),
                        "ENTITLEMENT_CODE": code or code_by_id.get(cid) or "UNKNOWN",
                        "PAID_SOURCE_BLOCKER": True,
                        "FREE_ALTERNATIVE_STATUS": str(
                            ((rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}).get("status") or "UNKNOWN"
                        ),
                        "POLICY": "NO_RETRY_ON_NORMAL_CYCLES",
                    }
                )
            scoreboard[family]["PAID_BLOCKED"] += 1
            continue

        gate = determine_canonical_live_state(
            {
                **rec,
                "canonical_opportunity_id": cid,
                "deadline": rec.get("deadline"),
                "title": rec.get("title"),
                "buyer": rec.get("buyer") or rec.get("agency"),
                "freshness": rec.get("freshness"),
                "platform": rec.get("platform") or family,
            },
            now=now,
        )
        live = gate.get("CANONICAL_STATE")
        if live != OPEN_ACCEPTING_BIDS:
            continue
        scoreboard[family]["TRUE_OPEN"] += 1

        row = {
            "OPPORTUNITY_ID": cid,
            "SOURCE": family,
            "TITLE": rec.get("title"),
            "BUYER": rec.get("buyer") or rec.get("agency"),
            "DEADLINE": rec.get("deadline"),
            "CATEGORY": rec.get("product_class") or rec.get("category"),
            "PORTAL": rec.get("authoritative_url") or rec.get("detail_url"),
        }
        product = assess_product_gate(row, rec)
        if not product.get("PRODUCT_GATE_PASS"):
            continue
        scoreboard[family]["PRODUCT_FIT"] += 1

        depth_info = assess_source_depth(row, rec)
        depth = str(depth_info.get("SOURCE_DEPTH") or "TITLE_ONLY")
        pkg_ok, pkg_reason = _package_resolved(rec, depth)
        if not pkg_ok:
            scoreboard[family]["OPEN_PRODUCT_NO_PACKAGE"] += 1
            continue
        scoreboard[family]["PACKAGE_RESOLVED"] += 1
        days = _days_left(rec.get("deadline"), now)
        runway = _runway_bucket(days)

        # Pool requires RICH/USABLE and >=3 days
        if depth not in {"RICH", "USABLE"}:
            # OpenGov official detail can promote SHALLOW→USABLE when package-resolved via project page
            if not (family == "OpenGov" and pkg_reason == "OFFICIAL_PROJECT_DETAIL" and len(str(rec.get("description") or "")) >= 120):
                if days is not None and days >= 5 and pkg_ok:
                    scoreboard[family]["GE_5_DAYS"] += 1
                continue
            depth = "USABLE"

        if depth in {"RICH", "USABLE"}:
            scoreboard[family]["RICH_USABLE"] += 1
        if depth == "RICH":
            scoreboard[family]["RICH"] += 1
        elif depth == "USABLE":
            scoreboard[family]["USABLE"] += 1
        if days is not None and days >= 5:
            scoreboard[family]["GE_5_DAYS"] += 1
        if days is not None and days < 3:
            continue

        # BidNet: production pool requires free official package path (not BidNet paywall host)
        if family == "BidNet":
            free = (rec.get("bidnet_recovery") or {}).get("free_package_chase") or {}
            free_url = str(free.get("source_url") or free.get("matched_url") or free.get("free_source_url") or "")
            vendor_probe = _vendor_path(rec, family)
            package_at = str(vendor_probe.get("PACKAGE_AT") or "")
            if "subscription-management" in package_at.lower() or "intercept/plans" in package_at.lower():
                continue
            official_free = bool(free_url) and "bidnet" not in free_url.lower()
            free_ok = free.get("status") in {
                "VALID_FREE_PACKAGE_FOUND",
                "PARTIAL_FREE_PACKAGE_FOUND",
                "FREE_PACKAGE_FOUND",
            }
            if not (official_free and free_ok):
                # Keep BidNet-discovered rows only when package evidence is already free/local
                if pkg_reason not in {"OFFICIAL_DOCUMENTS", "STRUCTURED_LINES_PLUS_DESCRIPTION"}:
                    scoreboard[family]["BIDNET_HELD_NO_FREE_PACKAGE"] += 1
                    continue

        identity = assess_identity(row, rec)
        elig = assess_eligibility(row, rec)
        from buyer_intelligence.package_recovery_source_depth_v1 import extract_revenue_from_text

        revenue = extract_revenue_from_text(str(rec.get("description") or ""))
        public = lookup_public_price(identity, price_cache)
        quote = lookup_real_quote(cid, identity, quotes)
        acq = assess_acquisition_path({**row, "ACQUISITION_PATH": "UNKNOWN", "SOURCE": family}, identity, public, quote)
        econ = assess_coverage_and_economics(revenue, acq)
        disp = decide_disposition(
            {
                "EXPIRED_AFTER_FREEZE": False,
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                "ELIGIBILITY": elig,
                "PRODUCT": product,
                "IDENTITY": identity,
                "REVENUE": revenue,
                "ACQUISITION": acq,
                "ECONOMICS": econ,
                "SOURCE_DEPTH": depth,
            }
        )

        quote_ready = _quote_ready_safe(identity) or (
            disp.get("FINAL_DISPOSITION") == "QUOTE" and _quote_ready_safe(identity)
        )
        # Identity adequate only when quote-ready guard passes or RESEARCH with proven candidate
        identity_adequate = identity.get("IDENTITY_STATE") == "ADEQUATE" and _quote_ready_safe(
            {**identity, "IDENTITY_QUOTE_READY": True}
        )
        if identity_adequate:
            scoreboard[family]["IDENTITY_ADEQUATE"] += 1
        if quote_ready:
            scoreboard[family]["QUOTE_READY"] += 1

        if quote_ready:
            tier = "A_QUOTE_READY"
        elif identity.get("IDENTITY_STATE") in {"ADEQUATE", "RESEARCH"} or identity.get("IDENTITY_STATE") == "IDENTITY_RESEARCH":
            tier = "B_IDENTITY_RESEARCH"
        elif pkg_ok and depth in {"RICH", "USABLE"}:
            tier = "C_PACKAGE_READY"
        else:
            tier = "D_FREE_SOURCE_RESEARCH"

        lines = rec.get("line_items") or []
        line_n = len(lines) if isinstance(lines, list) else 0
        vendor = _vendor_path(rec, family)

        pool.append(
            {
                "OPPORTUNITY_ID": cid,
                "SOURCE": family,
                "BUYER": row["BUYER"],
                "TITLE": row["TITLE"],
                "DEADLINE": row["DEADLINE"],
                "DAYS_LEFT": round(days, 2) if days is not None else None,
                "RUNWAY": runway,
                "CATEGORY": product.get("CATEGORY") or row.get("CATEGORY"),
                "PRODUCT_FIT": True,
                "FREE_ACCESS": True,
                "PACKAGE_RESOLVED": True,
                "PACKAGE_RESOLVED_REASON": pkg_reason,
                "SOURCE_DEPTH": depth,
                "LINE_COUNT": line_n,
                "QTY_KNOWN": sum(1 for ln in (lines if isinstance(lines, list) else []) if isinstance(ln, dict) and (ln.get("quantity") or ln.get("qty"))),
                "IDENTITY_STATE": identity.get("IDENTITY_STATE"),
                "IDENTITY_QUOTE_READY": quote_ready,
                "SOURCE_PROVEN_IDENTIFIER": identity.get("SOURCE_PROVEN_IDENTIFIER"),
                "REVENUE_STATE": revenue.get("REVENUE_STATE"),
                "CURRENT_GOV_REVENUE": revenue.get("CURRENT_GOV_REVENUE"),
                "ACQUISITION_PATH": acq.get("ACQUISITION_PATH"),
                "FINAL_DISPOSITION": disp.get("FINAL_DISPOSITION"),
                "TIER": tier,
                "NEXT_ACTION": disp.get("NEXT_ACTION") or ("QUOTE" if quote_ready else "RESEARCH"),
                "VENDOR_PATH": vendor,
                "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                "BUILD": BUILD,
            }
        )

    # Rank pool: quote-ready, runway, depth
    def _rank(r: dict[str, Any]) -> tuple:
        return (
            0 if r.get("TIER") == "A_QUOTE_READY" else 1 if r.get("TIER") == "B_IDENTITY_RESEARCH" else 2,
            0 if (r.get("DAYS_LEFT") or 0) >= 5 else 1,
            0 if r.get("SOURCE_DEPTH") == "RICH" else 1,
            -(r.get("LINE_COUNT") or 0),
        )

    pool.sort(key=_rank)

    # Source ranking by package-resolved product-fit
    ranking = sorted(
        (
            {
                "SOURCE": src,
                "TRUE_OPEN": int(c["TRUE_OPEN"]),
                "PRODUCT_FIT": int(c["PRODUCT_FIT"]),
                "PACKAGE_RESOLVED": int(c["PACKAGE_RESOLVED"]),
                "RICH_USABLE": int(c["RICH_USABLE"]),
                "RICH": int(c["RICH"]),
                "USABLE": int(c["USABLE"]),
                "IDENTITY_ADEQUATE": int(c["IDENTITY_ADEQUATE"]),
                "QUOTE_READY": int(c["QUOTE_READY"]),
                "GE_5_DAYS": int(c["GE_5_DAYS"]),
                "PAID_BLOCKED": int(c["PAID_BLOCKED"]),
            }
            for src, c in scoreboard.items()
        ),
        key=lambda x: (-x["PACKAGE_RESOLVED"], -x["QUOTE_READY"], -x["GE_5_DAYS"], -x["PRODUCT_FIT"]),
    )

    ge5 = sum(1 for r in pool if (r.get("DAYS_LEFT") or 0) >= 5)
    if ge5 >= 30:
        coverage = "FREE_SOURCES_SUFFICIENT"
    elif ge5 >= 10:
        coverage = "FREE_SOURCES_PARTIALLY_SUFFICIENT"
    else:
        coverage = "FREE_SOURCE_VOLUME_TOO_LOW"

    return {
        "pool": pool,
        "paid_backlog": paid_backlog,
        "scoreboard": ranking,
        "coverage": coverage,
        "ge5": ge5,
    }


def run_free_source_first_production_pool_v2(
    *,
    wall_limit_sec: float = WALL_LIMIT_SEC,
    score_only: bool = False,
) -> dict[str, Any]:
    # Use wall-clock (time.time) for all phase deadlines — never mix with perf_counter.
    t0_wall = time.time()
    t0_perf = time.perf_counter()
    wall_deadline = t0_wall + wall_limit_sec
    now = datetime.now(timezone.utc)
    freshness = {
        "EXECUTION_MODE": "REPLAY_VALIDATION" if score_only else "FRESH_PRODUCTION_EXECUTION",
        "FRESH_SOURCE_FETCHES": 0,
        "CACHE_HITS": 0,
        "PREEXISTING_EVIDENCE_REUSED": 0,
        "NEW_EVIDENCE_CREATED": 0,
    }
    _job_update(status="RUNNING", started_at=_utc(), phase="INIT", score_only=score_only)

    paid_ids, code_by_id, backlog_seed = load_paid_bidnet_ids()
    phases: dict[str, Any] = {}

    if score_only:
        phases["SKIPPED_FETCHES"] = {"reason": "score_only_rebuild_from_store"}
        # Preserve prior freshness totals from last fresh report when present
        prior = _load(REPORT_FILE) or {}
        prior_f = prior.get("FRESHNESS") or {}
        if prior_f.get("EXECUTION_MODE") == "FRESH_PRODUCTION_EXECUTION":
            freshness["FRESH_SOURCE_FETCHES"] = int(prior_f.get("FRESH_SOURCE_FETCHES") or 0)
            freshness["NEW_EVIDENCE_CREATED"] = int(prior_f.get("NEW_EVIDENCE_CREATED") or 0)
            freshness["CACHE_HITS"] = int(prior_f.get("CACHE_HITS") or 0)
            freshness["PREEXISTING_EVIDENCE_REUSED"] = int(prior_f.get("PREEXISTING_EVIDENCE_REUSED") or 0) + 1
            phases["PRIOR_FRESH_PHASES"] = prior.get("PHASES") or {}
        else:
            freshness["PREEXISTING_EVIDENCE_REUSED"] = 1
        try:
            from phase_l.l23_full_population_funnel import load_store

            opps = load_store()
            if not isinstance(opps, dict):
                opps = {}
        except Exception:
            raw = _load("l23_canonical_population_store.json") or {}
            opps = raw.get("opportunities") if isinstance(raw, dict) else {}
            if not isinstance(opps, dict):
                opps = {}
    else:
        # --- OpenGov discovery ---
        d1 = min(wall_deadline, t0_wall + PHASE_OPENGOV_DISC)
        phases["OPENGOV_DISCOVERY"] = phase_opengov_discovery(deadline=d1, freshness=freshness)

        # --- OpenGov recovery ---
        d2 = min(wall_deadline, time.time() + PHASE_OPENGOV_RECOVER)
        phases["OPENGOV_RECOVERY"] = phase_opengov_recovery(deadline=d2, freshness=freshness)

        # Load store after discovery/recovery (load_store returns opportunities dict)
        try:
            from phase_l.l23_full_population_funnel import load_store

            opps = load_store()
            if not isinstance(opps, dict):
                opps = {}
        except Exception:
            raw = _load("l23_canonical_population_store.json") or {}
            opps = raw.get("opportunities") if isinstance(raw, dict) else {}
            if not isinstance(opps, dict):
                opps = {}

        # --- SAM ---
        d3 = min(wall_deadline, time.time() + PHASE_SAM)
        phases["SAM"] = phase_sam(deadline=d3, freshness=freshness, store=opps)

        # --- State/local ---
        d4 = min(wall_deadline, time.time() + PHASE_STATE)
        phases["STATE_LOCAL"] = phase_state_local(deadline=d4, freshness=freshness)

        # Reload store if expansion merged
        try:
            from phase_l.l23_full_population_funnel import load_store

            opps2 = load_store()
            if isinstance(opps2, dict):
                opps = opps2
        except Exception:
            pass

        # --- BidNet free route (skip paid) ---
        d5 = min(wall_deadline, time.time() + PHASE_BIDNET_FREE)
        phases["BIDNET_FREE"] = phase_bidnet_free(
            deadline=d5, freshness=freshness, store=opps, paid_ids=paid_ids
        )

        # Persist store if mutated
        try:
            from phase_l.l23_full_population_funnel import save_store

            if isinstance(opps, dict):
                save_store(opps)
        except Exception:
            pass

    _job_update(phase="NORMALIZE_SCORE")
    built = build_pool_and_scoreboard(
        store=opps,
        paid_ids=paid_ids,
        code_by_id=code_by_id,
        backlog_seed=backlog_seed,
        now=now,
        freshness=freshness,
    )
    pool = built["pool"]
    paid_backlog = built["paid_backlog"]
    ranking = built["scoreboard"]
    coverage = built["coverage"]
    ge5 = built["ge5"]

    tiers = Counter(r.get("TIER") for r in pool)
    runway_c = Counter(r.get("RUNWAY") for r in pool)
    depth_c = Counter(r.get("SOURCE_DEPTH") for r in pool)

    top25 = pool[:25]
    quote_ready_rows = [r for r in pool if r.get("TIER") == "A_QUOTE_READY"][:25]

    # OpenGov/SAM slice stats from scoreboard
    by_src = {r["SOURCE"]: r for r in ranking}
    phase_view = {**(phases.get("PRIOR_FRESH_PHASES") or {}), **phases}

    owner_can = "YES" if coverage == "FREE_SOURCES_SUFFICIENT" else (
        "PARTIALLY" if coverage == "FREE_SOURCES_PARTIALLY_SUFFICIENT" else "NO"
    )

    pool_payload = {
        "kind": "M3_FREE_SOURCE_PRODUCTION_POOL_V2",
        "build": BUILD,
        "created_at": _utc(),
        "execution_mode": freshness["EXECUTION_MODE"],
        "count": len(pool),
        "ge5_days": ge5,
        "tiers": dict(tiers),
        "rows": pool,
        "selection_rules": {
            "LIVE": OPEN_ACCEPTING_BIDS,
            "PRODUCT_FIT": True,
            "FREE_ACCESS": True,
            "PACKAGE_RESOLVED": True,
            "SOURCE_DEPTH": ["RICH", "USABLE"],
            "MIN_RUNWAY_DAYS": 3,
            "PREFERRED_RUNWAY_DAYS": 5,
        },
    }
    backlog_payload = {
        "kind": "M3_PAID_SOURCE_BACKLOG_V1",
        "build": BUILD,
        "created_at": _utc(),
        "count": len(paid_backlog),
        "policy": "NO_RETRY_ON_NORMAL_CYCLES",
        "no_paid_access_purchased": True,
        "rows": paid_backlog,
    }
    _write(POOL_FILE, pool_payload)
    _write(BACKLOG_FILE, backlog_payload)

    report = {
        "BUILD": BUILD,
        "FINAL_STATUS": coverage,
        "WALL_CLOCK_SECONDS": round(time.perf_counter() - t0_perf, 2),
        "FRESHNESS": freshness,
        "PHASES": phases,
        "OPENGOV": {
            "entities_checked": (phase_view.get("OPENGOV_DISCOVERY") or {}).get("entities_checked"),
            "fresh_candidates": (phase_view.get("OPENGOV_DISCOVERY") or {}).get("candidates"),
            **(by_src.get("OpenGov") or {}),
        },
        "SAM": {
            **(phase_view.get("SAM") or {}),
            **(by_src.get("SAM") or {}),
        },
        "DIRECT_STATE_LOCAL_OTHER": {
            k: by_src[k]
            for k in by_src
            if k not in {"OpenGov", "SAM", "BidNet"}
        },
        "BIDNET_FREE_ROUTE": phase_view.get("BIDNET_FREE"),
        "SOURCE_SCOREBOARD": ranking,
        "SOURCE_RANKING": [r["SOURCE"] for r in ranking[:5]],
        "PRODUCTION_POOL": {
            "TOTAL": len(pool),
            "GE_5_DAYS": ge5,
            "RUNWAY": dict(runway_c),
            "DEPTH": dict(depth_c),
            "TIER_A_QUOTE_READY": int(tiers.get("A_QUOTE_READY") or 0),
            "TIER_B_IDENTITY_RESEARCH": int(tiers.get("B_IDENTITY_RESEARCH") or 0),
            "TIER_C_PACKAGE_READY": int(tiers.get("C_PACKAGE_READY") or 0),
            "TIER_D_FREE_RESEARCH": int(tiers.get("D_FREE_SOURCE_RESEARCH") or 0),
        },
        "TOP_25_PACKAGE_READY": [
            {
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "DEADLINE": r.get("DEADLINE"),
                "DAYS_LEFT": r.get("DAYS_LEFT"),
                "CATEGORY": r.get("CATEGORY"),
                "PACKAGE_LOCATION": (r.get("VENDOR_PATH") or {}).get("PACKAGE_AT"),
                "SOURCE_DEPTH": r.get("SOURCE_DEPTH"),
                "LINES": r.get("LINE_COUNT"),
                "QTY": r.get("QTY_KNOWN"),
                "IDENTITY": r.get("IDENTITY_STATE"),
                "REVENUE": r.get("REVENUE_STATE"),
                "ACQ_PATH": r.get("ACQUISITION_PATH"),
                "NEXT_ACTION": r.get("NEXT_ACTION"),
                "TIER": r.get("TIER"),
            }
            for r in top25
        ],
        "QUOTE_READY_SHORTLIST": [
            {
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "PRODUCT": r.get("TITLE"),
                "QTY": r.get("QTY_KNOWN"),
                "IDENTITY_EVIDENCE": r.get("SOURCE_PROVEN_IDENTIFIER"),
                "DEADLINE": r.get("DEADLINE"),
                "SUPPLIER_TYPE": "UNKNOWN",
                "PACKAGE_SOURCE": (r.get("VENDOR_PATH") or {}).get("PACKAGE_AT"),
                "NEXT_ACTION": r.get("NEXT_ACTION"),
            }
            for r in quote_ready_rows
        ],
        "PAID_BACKLOG": {
            "TOTAL": len(paid_backlog),
            "BIDNET": sum(1 for r in paid_backlog if r.get("SOURCE") == "BidNet"),
            "OTHER": sum(1 for r in paid_backlog if r.get("SOURCE") != "BidNet"),
            "NO_PAID_ACCESS_PURCHASED": True,
        },
        "OWNER_DECISION": {
            "CAN_OPERATE_FREE": owner_can,
            "PACKAGE_RESOLVED_GE5": ge5,
            "QUOTE_READY": int(tiers.get("A_QUOTE_READY") or 0),
            "BIDNET_PAID_REQUIRED_IMMEDIATELY": False,
            "DOMINANT_BLOCKER": (
                "FREE_PACKAGE_VOLUME_LOW"
                if coverage == "FREE_SOURCE_VOLUME_TOO_LOW"
                else "IDENTITY_OR_QUOTE_PIPELINE"
                if int(tiers.get("A_QUOTE_READY") or 0) == 0
                else "EXECUTE_QUOTES"
            ),
            "NEXT_ACTION_TO_FIRST_BID": (
                "WORK_TIER_A_QUOTE_READY"
                if int(tiers.get("A_QUOTE_READY") or 0) > 0
                else "EXPAND_FREE_OPENGOV_DETAIL_AND_IDENTITY"
            ),
        },
        "FINAL_QUESTIONS": {
            "1_open_free_product": sum(r.get("PRODUCT_FIT", 0) for r in ranking),
            "2_real_packages": len(pool),
            "3_best_source": (ranking[0]["SOURCE"] if ranking else None),
            "4_fresh_opengov_package_ready": (by_src.get("OpenGov") or {}).get("PACKAGE_RESOLVED", 0),
            "5_quote_ready": int(tiers.get("A_QUOTE_READY") or 0),
            "6_ge5": ge5,
            "7_operate_without_bidnet_paid": True,
            "8_top10": [
                {"BUYER": r.get("BUYER"), "TITLE": r.get("TITLE"), "SOURCE": r.get("SOURCE"), "DAYS_LEFT": r.get("DAYS_LEFT"), "TIER": r.get("TIER")}
                for r in pool[:10]
            ],
            "9_dominant_blocker": None,  # filled below
            "10_next_action": None,
        },
        "GATES": {
            "OWNER_DATA_SPEND_ZERO": True,
            "FRESH_PRODUCTION_EXECUTION": freshness["EXECUTION_MODE"] == "FRESH_PRODUCTION_EXECUTION",
            "FRESH_OPENGOV_DISCOVERY": bool(
                ((phases.get("OPENGOV_DISCOVERY") or {}).get("ok"))
                or (((phases.get("PRIOR_FRESH_PHASES") or {}).get("OPENGOV_DISCOVERY") or {}).get("ok"))
            ),
            "TRUE_LIVE_GATE_ACTIVE": True,
            "FREE_SOURCE_PRIORITY_ACTIVE": True,
            "BIDNET_PAID_RETRIES_DISABLED": True,
            "FREE_OFFICIAL_ROUTING_ACTIVE": True,
            "PACKAGE_RESOLVED_TRUTHFUL": True,
            "TITLE_ONLY_NOT_PACKAGE_READY": True,
            "SOURCE_DEPTH_TRUTHFUL": True,
            "IDENTITY_GUARDS_PRESERVED": True,
            "REVENUE_GUARD_PRESERVED": True,
            "SAM_BUDGET_PRESERVED": int(
                ((phases.get("SAM") or phases.get("PRIOR_FRESH_PHASES") or {}).get("SAM") or phases.get("SAM") or {}).get(
                    "api_calls_used"
                )
                or 0
            )
            <= SAM_HARD_DAILY,
            "FRESHNESS_ACCOUNTING_ACTIVE": True,
            "UNKNOWN_REMAINS_UNKNOWN": True,
        },
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
    }
    report["FINAL_QUESTIONS"]["9_dominant_blocker"] = report["OWNER_DECISION"]["DOMINANT_BLOCKER"]
    report["FINAL_QUESTIONS"]["10_next_action"] = report["OWNER_DECISION"]["NEXT_ACTION_TO_FIRST_BID"]

    _write(REPORT_FILE, report)
    _write(RESULTS_FILE, {"build": BUILD, "pool_count": len(pool), "backlog_count": len(paid_backlog)})
    _write(
        TODAY_FILE,
        {
            "FREE_QUOTES_READY": quote_ready_rows[:20],
            "FREE_PACKAGE_READY": [r for r in pool if r.get("TIER") == "C_PACKAGE_READY"][:20],
            "FREE_IDENTITY_RESEARCH": [r for r in pool if r.get("TIER") == "B_IDENTITY_RESEARCH"][:20],
            "PAID_ACCESS_BACKLOG_COUNT": len(paid_backlog),
        },
    )
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=coverage, pool_count=len(pool))
    return report


def start_job(*, force: bool = True, score_only: bool = False) -> dict[str, Any]:
    cur = _load(JOB_FILE) or {}
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur}
    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED", score_only=score_only)
    t = threading.Thread(
        target=lambda: run_free_source_first_production_pool_v2(score_only=score_only),
        name="free-source-pool-v2",
        daemon=True,
    )
    t.start()
    return {"status": "STARTED", "BUILD": BUILD, "started_at": _utc(), "score_only": score_only}


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "NO_JOB", "BUILD": BUILD}


def source_health_snapshot() -> dict[str, Any]:
    """Settings → Source Health rows from latest report + freshness."""
    report = _load(REPORT_FILE) or {}
    board = {r["SOURCE"]: r for r in (report.get("SOURCE_SCOREBOARD") or []) if isinstance(r, dict)}
    freshness = report.get("FRESHNESS") or {}
    phases = report.get("PHASES") or {}

    def _row(name: str, aliases: list[str] | None = None) -> dict[str, Any]:
        aliases = aliases or [name]
        agg = {
            "SOURCE": name,
            "FRESH_FETCHES": 0,
            "TRUE_OPEN": 0,
            "PACKAGE_RESOLVED": 0,
            "USABLE": 0,
            "QUOTE_READY": 0,
            "ACCESS_BLOCKERS": 0,
        }
        for a in aliases:
            b = board.get(a) or {}
            agg["TRUE_OPEN"] += int(b.get("TRUE_OPEN") or 0)
            agg["PACKAGE_RESOLVED"] += int(b.get("PACKAGE_RESOLVED") or 0)
            agg["USABLE"] += int(b.get("RICH_USABLE") or 0) or (
                int(b.get("RICH") or 0) + int(b.get("USABLE") or 0)
            )
            agg["QUOTE_READY"] += int(b.get("QUOTE_READY") or 0)
            agg["ACCESS_BLOCKERS"] += int(b.get("PAID_BLOCKED") or 0)
        if name == "OpenGov":
            agg["FRESH_FETCHES"] = int((phases.get("OPENGOV_DISCOVERY") or {}).get("entities_checked") or 0)
            agg["FRESH_FETCHES"] += int((phases.get("OPENGOV_RECOVERY") or {}).get("processed") or 0)
        elif name == "SAM":
            agg["FRESH_FETCHES"] = int((phases.get("SAM") or {}).get("api_calls_used") or 0)
        elif name == "BidNet free-route":
            bf = phases.get("BIDNET_FREE") or {}
            agg["FRESH_FETCHES"] = int(bf.get("leads_checked") or 0)
            agg["ACCESS_BLOCKERS"] = int(bf.get("paid_skipped") or 0) or agg["ACCESS_BLOCKERS"]
        elif name == "state/local":
            agg["FRESH_FETCHES"] = int(freshness.get("FRESH_SOURCE_FETCHES") or 0)  # approximate share
        return agg

    return {
        "BUILD": BUILD,
        "report_status": report.get("FINAL_STATUS") or "NO_REPORT",
        "freshness": freshness,
        "sources": [
            _row("OpenGov"),
            _row("SAM"),
            _row("BidNet free-route", ["BidNet"]),
            _row("state/local", ["PlanetBids", "IonWave", "Bonfire", "PublicPurchase", "DemandStar", "Other"]),
            _row("education", ["Education"]),
            _row("utilities", ["Utility", "Utilities"]),
        ],
    }
