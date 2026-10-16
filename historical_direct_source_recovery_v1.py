"""Recover historical BidNet opportunities via original government buyers.

Never calls BidNet. Resolves official portals, recovers public docs when possible,
and onboards verified buyers for recurring anonymous discovery.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application_clock import now_utc

log = logging.getLogger("govtracker.historical_direct_source_recovery_v1")

BUILD = "20261016-m3-historical-direct-source-recovery-v1"

DIRECT_SOURCE_DOCUMENTS_RECOVERED = "DIRECT_SOURCE_DOCUMENTS_RECOVERED"
DIRECT_SOURCE_LISTING_ONLY = "DIRECT_SOURCE_LISTING_ONLY"
ORIGINAL_BUYER_IDENTIFIED = "ORIGINAL_BUYER_IDENTIFIED"
BUYER_NOT_IDENTIFIED = "BUYER_NOT_IDENTIFIED"
SOLICITATION_EXPIRED = "SOLICITATION_EXPIRED"
SOLICITATION_CANCELLED = "SOLICITATION_CANCELLED"
OFFICIAL_SOURCE_NOT_FOUND = "OFFICIAL_SOURCE_NOT_FOUND"
AUTH_OR_PAYMENT_REQUIRED = "AUTH_OR_PAYMENT_REQUIRED"
TECHNICAL_ACCESS_FAILURE = "TECHNICAL_ACCESS_FAILURE"
ALREADY_RECOVERED = "ALREADY_RECOVERED"

_SERVICE = re.compile(
    r"\b(service|services|consulting|construction|renovation|staffing|labor|"
    r"professional\s+services|janitorial\s+service|landscap|roofing)\b",
    re.I,
)
_PRODUCT = re.compile(
    r"\b(supply|supplies|equipment|materials|furniture|paper|toner|ppe|gloves|"
    r"tools|parts|vehicle|fleet|chemical|commodity|goods|product)\b",
    re.I,
)


def _is_bidnet_rec(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("platform", "source_id", "source", "authoritative_url", "detail_url")
    ).lower()
    return "bidnet" in blob


def _deadline_open(rec: dict[str, Any]) -> bool | None:
    raw = str(rec.get("deadline") or rec.get("response_deadline") or "")[:19]
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc)
    except Exception:
        return None


def _priority(rec: dict[str, Any]) -> float:
    score = 0.0
    open_ = _deadline_open(rec)
    if open_ is True:
        score += 10.0
    elif open_ is False:
        score -= 20.0
    title = str(rec.get("title") or "")
    if _PRODUCT.search(title):
        score += 4.0
    if _SERVICE.search(title) and not _PRODUCT.search(title):
        score -= 3.0
    sol = str(rec.get("solicitation_number") or rec.get("solicitation_event_id") or "")
    if sol and len(sol) >= 4:
        score += 3.0
    if rec.get("buyer") or rec.get("agency"):
        score += 3.0
    if rec.get("agency_source_url") or rec.get("original_posting_url"):
        score += 2.0
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
    if chase.get("documents"):
        score += 5.0
    return score


def audit_bidnet_metadata(*, sample_limit: int | None = None) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    stats = Counter()
    buyers: Counter = Counter()
    states: Counter = Counter()
    with_sol = 0
    with_buyer = 0
    with_agency_url = 0
    live_open = 0
    productish = 0
    already_docs = 0
    examined = 0
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_bidnet_rec(rec):
            continue
        examined += 1
        if sample_limit and examined > sample_limit:
            break
        stats["bidnet_associated"] += 1
        buyer = str(rec.get("buyer") or rec.get("agency") or "").strip()
        if buyer:
            with_buyer += 1
            buyers[buyer[:80]] += 1
        sol = str(rec.get("solicitation_number") or rec.get("solicitation_event_id") or "").strip()
        if sol:
            with_sol += 1
        if rec.get("agency_source_url") or rec.get("original_posting_url"):
            with_agency_url += 1
        st = str(rec.get("state") or "")[:2].upper()
        if st:
            states[st] += 1
        open_ = _deadline_open(rec)
        if open_ is True:
            live_open += 1
        title = str(rec.get("title") or "")
        if _PRODUCT.search(title):
            productish += 1
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
        if chase.get("documents") or br.get("official_source", {}).get("official_source_resolved"):
            already_docs += 1 if chase.get("documents") else 0
            if br.get("official_source", {}).get("official_source_resolved"):
                stats["already_official_resolved"] += 1
    return {
        "kind": "BidNetMetadataAudit",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "bidnet_associated_examined": examined,
        "with_buyer": with_buyer,
        "with_solicitation_number": with_sol,
        "with_agency_source_url": with_agency_url,
        "deadline_still_open": live_open,
        "productish_titles": productish,
        "already_have_chase_documents": already_docs,
        "already_official_resolved": int(stats.get("already_official_resolved") or 0),
        "top_buyers": buyers.most_common(25),
        "top_states": states.most_common(20),
    }


def _classify_after_resolve(rec: dict[str, Any], resolution: dict[str, Any], chase: dict[str, Any] | None) -> str:
    open_ = _deadline_open(rec)
    if open_ is False:
        return SOLICITATION_EXPIRED
    fresh = str(rec.get("freshness") or "").upper()
    if fresh in {"CANCELED", "CANCELLED"}:
        return SOLICITATION_CANCELLED
    chase = chase or {}
    docs = chase.get("documents") or []
    status = str(chase.get("status") or "")
    if docs and status in {
        "VALID_FREE_PACKAGE_FOUND",
        "PARTIAL_FREE_PACKAGE_FOUND",
        "FREE_PACKAGE_FOUND",
        "DOCUMENTS_FOUND_PUBLIC",
    }:
        return DIRECT_SOURCE_DOCUMENTS_RECOVERED
    if docs:
        return DIRECT_SOURCE_DOCUMENTS_RECOVERED
    if resolution.get("official_source_resolved"):
        if resolution.get("resolved_opportunity_url"):
            return DIRECT_SOURCE_LISTING_ONLY
        return ORIGINAL_BUYER_IDENTIFIED
    if not (rec.get("buyer") or rec.get("agency")):
        return BUYER_NOT_IDENTIFIED
    return OFFICIAL_SOURCE_NOT_FOUND


def _onboard_buyer(rec: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any] | None:
    """Register verified public buyer portal for recurring discovery."""
    buyer = str(resolution.get("resolved_agency") or rec.get("buyer") or rec.get("agency") or "").strip()
    base = str(resolution.get("resolved_base_url") or resolution.get("resolved_opportunity_url") or "").strip()
    if not buyer or not base:
        return None
    if "bidnet" in base.lower():
        return None
    try:
        from official_source.agency_profile import upsert_profile

        upsert_profile(
            agency_name=buyer,
            platform_family=str(resolution.get("resolved_platform") or "UNKNOWN"),
            base_url=base,
            public_search_url=base,
            government_code=resolution.get("government_code"),
            evidence=["onboarded_from_historical_bidnet_recovery"],
            confidence=0.7,
        )
    except Exception as exc:
        return {"error": type(exc).__name__, "buyer": buyer}
    try:
        from procurement_source_registry import ProcurementSourceRegistry

        reg = ProcurementSourceRegistry()
        slug = re.sub(r"[^a-z0-9]+", "_", buyer.lower()).strip("_")[:60]
        sid = f"direct_buyer_{slug}"
        result = reg.register_discovered_candidate(
            source_id=sid,
            source_name=buyer,
            discovery_url=base,
            entity_type="OTHER_PUBLIC",
            platform_family=str(resolution.get("resolved_platform") or "UNKNOWN"),
            notes="historical_bidnet_direct_source_onboard",
        )
        return {"buyer": buyer, "source_id": sid, "registry": result, "base_url": base}
    except Exception as exc:
        return {"buyer": buyer, "base_url": base, "profile_ok": True, "registry_error": type(exc).__name__}


def run_historical_direct_source_recovery(
    *,
    limit: int = 200,
    resume: bool = True,
    download: bool = True,
    onboard_buyers: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Priority recovery batch — official_source + free chase + buyer onboard."""
    from official_source.batch import run_official_source_batch
    from phase_l.l23_full_population_funnel import load_store, save_store

    run_id = run_id or f"HDSR-{uuid4().hex[:10]}"
    started = now_utc().isoformat()
    meta = audit_bidnet_metadata()

    # Official-source batch recovers OpenGov docs for BidNet-origin rows (no BidNet HTTP).
    osr = run_official_source_batch(
        limit=limit,
        resume=resume,
        download=download,
        run_id=f"{run_id}-osr",
        handoff_line_items=True,
    )
    # If the shared OSR checkpoint already covered the default sample, force a
    # fresh resolve pass on the highest-priority unscored slice (still no BidNet).
    if int(osr.get("attempted") or 0) == 0 and resume:
        osr = run_official_source_batch(
            limit=limit,
            resume=False,
            download=download,
            run_id=f"{run_id}-osr-fresh",
            handoff_line_items=True,
        )
        osr["note"] = "retried_with_resume_false_after_empty_checkpoint_window"

    store = load_store()
    classes: Counter = Counter()
    onboarded: list[dict[str, Any]] = []
    recovered_docs = 0
    samples: list[dict[str, Any]] = []

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_bidnet_rec(rec):
            continue
        scored.append((_priority(rec), str(cid), rec))
    scored.sort(key=lambda x: -x[0])

    for score, cid, rec in scored[: max(limit * 2, 50)]:
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        resolution = br.get("official_source") if isinstance(br.get("official_source"), dict) else {}
        chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
        if chase.get("documents") and not resolution:
            # Already recovered somehow
            cls = ALREADY_RECOVERED
        else:
            cls = _classify_after_resolve(rec, resolution or {}, chase)
        classes[cls] += 1
        if cls == DIRECT_SOURCE_DOCUMENTS_RECOVERED:
            recovered_docs += 1
        if onboard_buyers and resolution.get("official_source_resolved"):
            ob = _onboard_buyer(rec, resolution)
            if ob and not ob.get("error"):
                onboarded.append(ob)
                rec = dict(rec)
                br = dict(br)
                br["direct_source_onboard"] = {
                    "at": now_utc().isoformat(),
                    "class": cls,
                    "onboard": {k: ob.get(k) for k in ("buyer", "source_id", "base_url")},
                }
                rec["bidnet_recovery"] = br
                rec["direct_source_recovery_class"] = cls
                store[cid] = rec
        elif cls not in {BUYER_NOT_IDENTIFIED}:
            rec = dict(rec)
            rec["direct_source_recovery_class"] = cls
            store[cid] = rec
        if len(samples) < 30:
            samples.append(
                {
                    "canonical_id": cid,
                    "class": cls,
                    "buyer": rec.get("buyer") or rec.get("agency"),
                    "title": (rec.get("title") or "")[:120],
                    "solicitation": rec.get("solicitation_number") or rec.get("solicitation_event_id"),
                    "deadline": rec.get("deadline"),
                    "resolved_platform": (resolution or {}).get("resolved_platform"),
                    "resolved_url": (resolution or {}).get("resolved_opportunity_url"),
                    "docs": len((chase or {}).get("documents") or []),
                    "priority": score,
                }
            )

    try:
        save_store(store)
    except Exception:
        log.exception("save_store after historical recovery failed")

    # Deduplicate onboarded buyers
    seen_buyers: set[str] = set()
    unique_onboarded = []
    for ob in onboarded:
        b = str(ob.get("buyer") or "").lower()
        if b and b not in seen_buyers:
            seen_buyers.add(b)
            unique_onboarded.append(ob)

    report = {
        "kind": "HistoricalDirectSourceRecoveryReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "metadata_audit": meta,
        "official_source_batch": {
            "attempted": osr.get("attempted"),
            "official_source_resolved": osr.get("official_source_resolved"),
            "valid_packages": osr.get("valid_packages"),
            "partial_packages": osr.get("partial_packages"),
            "unresolved": osr.get("unresolved"),
        },
        "classification_counts": dict(classes),
        "documents_recovered_count": recovered_docs,
        "buyers_onboarded": len(unique_onboarded),
        "onboarded_sample": unique_onboarded[:40],
        "samples": samples,
        "note": "BidNet HTTP never invoked; recovery uses OfficialSourceResolver + public OpenGov docs",
    }
    try:
        from m3_data_root import data_path

        path = data_path("m3_historical_direct_source_recovery_last_report.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("persist historical recovery report failed")
    return report
