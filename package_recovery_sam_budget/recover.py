"""Per-opportunity package recovery for BidNet / SAM / Other / OpenGov."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from package_recovery_sam_budget.memory import remember_buyer_package_source, record_portal_family
from package_recovery_sam_budget.models import (
    PACKAGE_ACQUIRED,
    PACKAGE_ACQUIRED_CACHED,
    PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE,
    PACKAGE_API_CALL_FAILED,
    PACKAGE_API_CREDIT_EXHAUSTED,
    PACKAGE_API_CREDIT_REQUIRED,
    PACKAGE_AUTH_REQUIRED,
    PACKAGE_DEFERRED_SAM_PRIORITY,
    PACKAGE_LOCKED_AGGREGATOR,
    PACKAGE_NOT_FOUND_FREE,
    PACKAGE_OFFICIAL_SOURCE_NOT_FOUND,
    PACKAGE_RETRYABLE,
    PACKAGE_SOURCE_BLOCKED,
    SAM_PACKAGE_ACQUIRED,
    SAM_PACKAGE_CACHED,
    SAM_PACKAGE_DEFERRED_BUDGET,
    SAM_PACKAGE_DEFERRED_LOW_PRIORITY,
    SAM_PACKAGE_FAILED_RETRYABLE,
    SAM_PACKAGE_FAILED_TERMINAL,
    SAM_PACKAGE_QUEUED,
    SAM_PACKAGE_REQUESTED,
)
from package_recovery_sam_budget.quality import inventory_documents, is_valid_package_evidence
from package_recovery_sam_budget.sam_manager import SamDailyCreditManager


def _l23_cid(oid: str) -> str | None:
    if oid.startswith("l23:"):
        return oid.split(":", 1)[1]
    return None


def recover_opengov(meta: dict[str, Any], pkg_index: dict[str, Any]) -> dict[str, Any]:
    oid = meta["opportunity_id"]
    if not oid.startswith("opengov:"):
        return {"opportunity_id": oid, "package_state": PACKAGE_NOT_FOUND_FREE, "source": "OpenGov"}
    parts = oid.split(":")
    folder = data_path(f"opengov_public_docs/documents/{parts[1]}/{parts[2]}") if len(parts) >= 3 else None
    docs = []
    if folder and folder.exists():
        docs = [f for f in folder.iterdir() if f.is_file() and not f.name.endswith(".meta.json")]
    row = pkg_index.get(oid) or {}
    if docs or (row.get("documents")):
        inv = inventory_documents(
            oid,
            [
                {
                    "filename": f.name,
                    "local_path": str(f),
                    "document_url": None,
                }
                for f in docs
            ]
            or (row.get("documents") or []),
        )
        quality = is_valid_package_evidence(
            [{"document_name": d.get("filename"), "local_path": d.get("canonical_storage_reference") or ""} for d in (row.get("documents") or [])]
            or [{"document_name": f.name, "local_path": str(f)} for f in docs]
        )
        state = PACKAGE_ACQUIRED_CACHED if row.get("status") == "PACKAGE_PROVENANCE_COMPLETE" else PACKAGE_ACQUIRED
        remember_buyer_package_source(
            buyer=str(meta.get("buyer") or parts[1]),
            portal="OpenGov",
            source_system="opengov_public_docs",
            success=True,
        )
        record_portal_family(family="OpenGov", success=True, public_free=True)
        return {
            "opportunity_id": oid,
            "source_bucket": "OpenGov",
            "package_state": state,
            "documents": inv,
            "document_count": len(docs) or len(row.get("documents") or []),
            "quality": quality.get("counts"),
            "route": "opengov_local_package",
            "verified": True,
        }
    return {
        "opportunity_id": oid,
        "source_bucket": "OpenGov",
        "package_state": PACKAGE_RETRYABLE,
        "verified": False,
        "route": "opengov_missing_folder",
    }


def recover_bidnet(
    meta: dict[str, Any],
    *,
    l23_row: dict[str, Any],
    store: dict[str, Any],
    force: bool = True,
) -> dict[str, Any]:
    oid = meta["opportunity_id"]
    from bidnet_recovery.free_package_chase import (
        PACKAGE_MATCH_AMBIGUOUS,
        PACKAGE_RECOVERY_RETRYABLE,
        PACKAGE_UNAVAILABLE_FREE,
        PARTIAL_FREE_PACKAGE_FOUND,
        VALID_FREE_PACKAGE_FOUND,
        _opengov_free_match,
        _store_free_duplicate_match,
        chase_free_package,
    )
    from bidnet_recovery.states import FREE_PACKAGE_FOUND

    def _success_from_docs(docs: list, route: str, chase: dict | None = None) -> dict[str, Any] | None:
        quality = is_valid_package_evidence(docs)
        if not quality["valid"]:
            return None
        route_l = str(route or "").lower()
        if "opengov" in route_l or "government-project.s3" in route_l or "cached_opengov" in route_l:
            route_family = "OpenGov"
        elif "ionwave" in route_l:
            route_family = "IonWave"
        elif "agency" in route_l or "buyer" in route_l:
            route_family = "buyer_procurement"
        elif "board" in route_l or "council" in route_l:
            route_family = "board_council"
        elif "state" in route_l:
            route_family = "state_local_portal"
        else:
            route_family = "other_official"
        remember_buyer_package_source(
            buyer=str(meta.get("buyer") or l23_row.get("buyer") or ""),
            portal=route_family,
            source_system=str((chase or {}).get("matched_source") or route_family),
            package_url_pattern=(docs[0].get("document_url") if docs else None),
            success=True,
        )
        record_portal_family(family=route_family, success=True, public_free=True)
        br = l23_row.setdefault("bidnet_recovery", {})
        if chase:
            br["free_package_chase"] = chase
        br["free_package_found"] = True
        l23_row["attachments_metadata"] = quality["accepted"]
        return {
            "opportunity_id": oid,
            "source_bucket": "BidNet",
            "package_state": PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE,
            "documents": inventory_documents(oid, quality["accepted"]),
            "document_count": len(quality["accepted"]),
            "quality": quality["counts"],
            "route": route,
            "route_family": route_family,
            "matched_source": (chase or {}).get("matched_source"),
            "matched_opportunity_id": (chase or {}).get("matched_opportunity_id"),
            "match_confidence": (chase or {}).get("confidence"),
            "cross_portal_match": bool((chase or {}).get("matched_opportunity_id")) or route_family == "OpenGov",
            "verified": True,
            "chase_status": VALID_FREE_PACKAGE_FOUND,
        }

    # Cached prior VALID free chase / official alternate docs
    prior = ((l23_row.get("bidnet_recovery") or {}).get("free_package_chase") or {})
    prior_docs = []
    for d in list(prior.get("documents") or []) + list(l23_row.get("attachments_metadata") or []):
        if not isinstance(d, dict):
            continue
        url = str(d.get("document_url") or d.get("url") or "")
        if "bidnet" in url.lower() and "government-project.s3" not in url.lower():
            continue
        if (
            d.get("free_chase")
            or d.get("free_source")
            or d.get("free_public")
            or d.get("local_path")
            or d.get("opengov_project_id")
            or ("government-project.s3" in url.lower())
            or ("opengov" in url.lower())
            or (url.startswith("http") and "bidnet" not in url.lower())
        ):
            prior_docs.append(d)
    # de-dupe by url/name
    seen_u: set[str] = set()
    deduped = []
    for d in prior_docs:
        key = str(d.get("document_url") or d.get("local_path") or d.get("document_name") or id(d))
        if key in seen_u:
            continue
        seen_u.add(key)
        deduped.append(d)
    prior_docs = deduped

    if (
        prior.get("status") in {VALID_FREE_PACKAGE_FOUND, FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}
        and prior_docs
    ):
        hit = _success_from_docs(
            prior_docs,
            prior.get("recovery_route") or prior.get("matched_source") or "cached_prior_chase",
            chase=prior,
        )
        if hit:
            hit["package_state"] = PACKAGE_ACQUIRED_CACHED
            hit["chase_status"] = prior.get("status")
            return hit
    if prior_docs and any(
        "opengov" in str(d.get("document_url") or "").lower()
        or "government-project.s3" in str(d.get("document_url") or "").lower()
        or d.get("opengov_project_id")
        or d.get("local_path")
        for d in prior_docs
    ):
        hit = _success_from_docs(prior_docs, "cached_opengov_s3", chase=prior)
        if hit:
            hit["package_state"] = PACKAGE_ACQUIRED_CACHED
            hit["route_family"] = "OpenGov"
            return hit

    # Fast path 1: L23 store duplicate (non-BidNet free portal)
    try:
        dup = _store_free_duplicate_match(l23_row, store=store)
        dup_docs = list(dup.get("docs") or dup.get("documents") or [])
        if dup and dup_docs:
            hit = _success_from_docs(
                dup_docs,
                dup.get("recovery_route") or dup.get("matched_source") or "store_duplicate",
                chase={
                    **dup,
                    "matched_source": dup.get("matched_source"),
                    "matched_opportunity_id": dup.get("matched_opportunity_id"),
                    "confidence": dup.get("confidence"),
                },
            )
            if hit:
                hit["cross_portal_match"] = True
                return hit
    except Exception:
        pass

    # Fast path 2: OpenGov public match
    try:
        og = _opengov_free_match(l23_row, parsed=None)
        og_docs = list(og.get("docs") or og.get("documents") or [])
        if og and og_docs:
            hit = _success_from_docs(
                og_docs,
                og.get("recovery_route") or "opengov_free_match",
                chase={
                    **og,
                    "matched_source": og.get("matched_source"),
                    "matched_opportunity_id": og.get("matched_opportunity_id"),
                    "confidence": og.get("confidence"),
                },
            )
            if hit:
                hit["cross_portal_match"] = True
                hit["route_family"] = "OpenGov"
                return hit
    except Exception:
        pass

    # Full chase — refresh BidNet public overview for agency/solicitation clues (no membership)
    store_opps = store.get("opportunities") if isinstance(store, dict) and "opportunities" in store else store
    need_refresh = not ((l23_row.get("bidnet_recovery") or {}).get("overview") or l23_row.get("description"))
    chase = chase_free_package(l23_row, store=store_opps, refresh_overview=need_refresh)
    status = chase.get("status")
    docs = list(chase.get("documents") or chase.get("docs") or [])
    hit = _success_from_docs(docs, chase.get("recovery_route") or chase.get("matched_source") or "full_chase", chase=chase)
    if hit:
        return hit

    quality = is_valid_package_evidence(docs)
    route = chase.get("recovery_route") or chase.get("matched_source") or "unknown"

    # Detect BidNet auth-locked fields from overview refresh / attempts
    locked_signal = False
    for a in chase.get("attempts") or []:
        via = str(a.get("via") or "")
        if via == "bidnet_public_overview_refresh" and not a.get("agency") and not a.get("overview"):
            locked_signal = True
        blob = json.dumps(a, default=str).lower()
        if "member-only" in blob or "registered members only" in blob or "locked" in blob:
            locked_signal = True
    url = str(meta.get("authoritative_url") or l23_row.get("authoritative_url") or "")
    if "/statewide/" in url and status == PACKAGE_RECOVERY_RETRYABLE:
        locked_signal = True

    if status == PACKAGE_MATCH_AMBIGUOUS:
        record_portal_family(family="BidNet", success=False, failure_mode="ambiguous")
        return {
            "opportunity_id": oid,
            "source_bucket": "BidNet",
            "package_state": PACKAGE_RETRYABLE,
            "route": route,
            "chase_status": status,
            "verified": False,
            "note": "ambiguous_cross_portal_match",
        }

    if status == PACKAGE_UNAVAILABLE_FREE or locked_signal:
        attempts = chase.get("attempts") or []
        locked = locked_signal or any("bidnet" in str(a).lower() or "auth" in str(a).lower() for a in attempts)
        state = PACKAGE_LOCKED_AGGREGATOR if locked else PACKAGE_OFFICIAL_SOURCE_NOT_FOUND
        record_portal_family(family="BidNet", success=False, failure_mode=state, public_free=False, auth_required=locked)
        return {
            "opportunity_id": oid,
            "source_bucket": "BidNet",
            "package_state": state,
            "route": route,
            "chase_status": status,
            "quality": quality.get("counts"),
            "rejected_landing": quality.get("counts", {}).get("invalid_landing_rejected", 0),
            "verified": False,
            "note": "bidnet_membership_locked_searching_official" if locked else "official_source_not_found",
        }

    record_portal_family(family="BidNet", success=False, failure_mode=PACKAGE_RETRYABLE)
    return {
        "opportunity_id": oid,
        "source_bucket": "BidNet",
        "package_state": PACKAGE_RETRYABLE if status == PACKAGE_RECOVERY_RETRYABLE else PACKAGE_NOT_FOUND_FREE,
        "route": route,
        "chase_status": status,
        "verified": False,
    }


def recover_other(meta: dict[str, Any], *, l23_row: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    """Official buyer fallback for non-OpenGov/non-BidNet (Jaggaer, etc.)."""
    oid = meta["opportunity_id"]
    # Reuse BidNet chase machinery — it already searches OpenGov + agency sites from title/buyer/sol#
    from bidnet_recovery.free_package_chase import (
        PACKAGE_UNAVAILABLE_FREE,
        PARTIAL_FREE_PACKAGE_FOUND,
        VALID_FREE_PACKAGE_FOUND,
        chase_free_package,
    )
    from bidnet_recovery.states import FREE_PACKAGE_FOUND

    url = str(l23_row.get("authoritative_url") or (l23_row.get("row_ref") or {}).get("detail_url") or "")
    if "login" in url.lower() or "authtoken" in url.lower():
        # Still try official alternate
        pass

    chase = chase_free_package(l23_row, store=store, refresh_overview=False)
    status = chase.get("status")
    docs = chase.get("documents") or []
    quality = is_valid_package_evidence(docs)
    if status in {VALID_FREE_PACKAGE_FOUND, FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND} and quality["valid"]:
        remember_buyer_package_source(
            buyer=str(meta.get("buyer") or l23_row.get("buyer") or ""),
            portal=str(chase.get("matched_source") or "official"),
            source_system=str(l23_row.get("platform") or "other"),
            success=True,
        )
        return {
            "opportunity_id": oid,
            "source_bucket": "Other",
            "package_state": PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE,
            "documents": inventory_documents(oid, quality["accepted"]),
            "document_count": len(quality["accepted"]),
            "quality": quality["counts"],
            "route": chase.get("recovery_route"),
            "verified": True,
        }
    if status == PACKAGE_UNAVAILABLE_FREE:
        return {
            "opportunity_id": oid,
            "source_bucket": "Other",
            "package_state": PACKAGE_OFFICIAL_SOURCE_NOT_FOUND,
            "verified": False,
            "route": chase.get("recovery_route"),
        }
    # Auth-walled aggregator
    plat = str(l23_row.get("platform") or "").lower()
    if any(x in plat for x in ("jaggaer", "bonfire", "euna", "bidnet")):
        return {
            "opportunity_id": oid,
            "source_bucket": "Other",
            "package_state": PACKAGE_LOCKED_AGGREGATOR,
            "verified": False,
            "route": plat,
        }
    return {
        "opportunity_id": oid,
        "source_bucket": "Other",
        "package_state": PACKAGE_RETRYABLE,
        "verified": False,
        "chase_status": status,
    }


def recover_sam(
    meta: dict[str, Any],
    *,
    l23_row: dict[str, Any],
    manager: SamDailyCreditManager,
    priority: dict[str, Any],
    selected: bool,
) -> dict[str, Any]:
    oid = meta["opportunity_id"]
    sol = (
        l23_row.get("solicitation_event_id")
        or (l23_row.get("row_ref") or {}).get("solicitation_number")
        or ""
    )
    title = str(meta.get("title") or l23_row.get("title") or "")

    if not selected:
        if not priority.get("eligible"):
            return {
                "opportunity_id": oid,
                "source_bucket": "SAM",
                "package_state": PACKAGE_DEFERRED_SAM_PRIORITY,
                "sam_queue_state": SAM_PACKAGE_DEFERRED_LOW_PRIORITY,
                "priority_score": priority.get("priority_score"),
                "priority_reasons": priority.get("reasons"),
                "verified": False,
            }
        return {
            "opportunity_id": oid,
            "source_bucket": "SAM",
            "package_state": PACKAGE_DEFERRED_SAM_PRIORITY,
            "sam_queue_state": SAM_PACKAGE_DEFERRED_BUDGET,
            "priority_score": priority.get("priority_score"),
            "priority_reasons": priority.get("reasons"),
            "verified": False,
            "note": "not_selected_today_budget",
        }

    # Selected for today — cache first then optional live
    params: dict[str, Any] = {"limit": 10, "offset": 0}
    if sol:
        params["solicitationNumber"] = str(sol)
    else:
        # title keyword search — still one credit if live
        params["title"] = title[:80] if title else "supply"
        params["ptype"] = "o,k,p,r"  # common notice types

    result = manager.search_cached_or_live(
        params,
        reason=f"package_recovery:{oid}",
        allow_live=True,
        use_reserve=False,
    )

    if result.get("reason") == "PACKAGE_API_CREDIT_EXHAUSTED":
        return {
            "opportunity_id": oid,
            "source_bucket": "SAM",
            "package_state": PACKAGE_API_CREDIT_EXHAUSTED,
            "sam_queue_state": SAM_PACKAGE_DEFERRED_BUDGET,
            "priority_score": priority.get("priority_score"),
            "verified": False,
            "credits_spent": 0,
        }

    if result.get("reason") == "PACKAGE_API_CALL_FAILED":
        return {
            "opportunity_id": oid,
            "source_bucket": "SAM",
            "package_state": PACKAGE_API_CALL_FAILED,
            "sam_queue_state": SAM_PACKAGE_FAILED_RETRYABLE,
            "verified": False,
            "error": result.get("error"),
        }

    payload = result.get("payload") or {}
    # Normalize opportunities list from various shapes
    opps = (
        payload.get("opportunitiesData")
        or payload.get("opportunities")
        or (payload.get("data") or {}).get("opportunitiesData")
        or []
    )
    if isinstance(payload, dict) and payload.get("cached_payload"):
        opps = opps or (payload["cached_payload"].get("opportunitiesData") or [])

    docs = []
    for opp in opps[:5]:
        if not isinstance(opp, dict):
            continue
        for key in ("resourceLinks", "additionalInfoLink", "uiLink", "links"):
            val = opp.get(key)
            if isinstance(val, list):
                for u in val:
                    if isinstance(u, str) and u.startswith("http"):
                        docs.append({"document_url": u, "document_name": Path(u).name or "sam_resource", "source": "sam_api"})
                    elif isinstance(u, dict) and u.get("url"):
                        docs.append({"document_url": u["url"], "document_name": u.get("name") or "sam_resource", "source": "sam_api"})
            elif isinstance(val, str) and val.startswith("http"):
                docs.append({"document_url": val, "document_name": "sam_link", "source": "sam_api"})
        # description as weak evidence only — quality gate will reject if landing-like
        if opp.get("uiLink"):
            docs.append({"document_url": opp["uiLink"], "document_name": "sam_notice", "source": "sam_ui"})

    quality = is_valid_package_evidence(docs)
    sam_state = SAM_PACKAGE_CACHED if result.get("cache_hit") else SAM_PACKAGE_REQUESTED

    if quality["valid"]:
        remember_buyer_package_source(
            buyer=str(meta.get("buyer") or l23_row.get("buyer") or "SAM"),
            portal="SAM",
            source_system="sam_gov_api",
            success=True,
        )
        record_portal_family(family="SAM", success=True, public_free=False, auth_required=False)
        return {
            "opportunity_id": oid,
            "source_bucket": "SAM",
            "package_state": PACKAGE_ACQUIRED_CACHED if result.get("cache_hit") else PACKAGE_ACQUIRED,
            "sam_queue_state": SAM_PACKAGE_ACQUIRED,
            "documents": inventory_documents(oid, quality["accepted"]),
            "document_count": len(quality["accepted"]),
            "quality": quality["counts"],
            "verified": True,
            "cache_hit": result.get("cache_hit"),
            "credits_spent": result.get("credits_spent", 0),
            "priority_score": priority.get("priority_score"),
            "duplicate_prevented": result.get("duplicate_prevented"),
        }

    # API returned but no usable docs
    if result.get("ok") and not quality["valid"]:
        return {
            "opportunity_id": oid,
            "source_bucket": "SAM",
            "package_state": PACKAGE_NOT_FOUND_FREE if not docs else PACKAGE_RETRYABLE,
            "sam_queue_state": SAM_PACKAGE_FAILED_TERMINAL if not docs else SAM_PACKAGE_FAILED_RETRYABLE,
            "verified": False,
            "credits_spent": result.get("credits_spent", 0),
            "cache_hit": result.get("cache_hit"),
            "quality": quality.get("counts"),
            "priority_score": priority.get("priority_score"),
            "note": "api_ok_but_no_solicitation_docs",
        }

    return {
        "opportunity_id": oid,
        "source_bucket": "SAM",
        "package_state": PACKAGE_API_CREDIT_REQUIRED,
        "sam_queue_state": sam_state,
        "verified": False,
        "credits_spent": result.get("credits_spent", 0),
        "priority_score": priority.get("priority_score"),
    }
