"""Nationwide anonymous procurement replacement discovery (no BidNet / no Euna).

Target: 25,000+ unique LIVE non-BidNet opportunities with honest access flags.
Preserves existing FSPF job — discovery merges into L23; append is a separate owner step.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from anonymous_source_policy_v1 import bidnet_abandoned, euna_abandoned, policy_status

log = logging.getLogger("govtracker.anonymous_replacement_discovery_v1")

BUILD = "20261016-m3-anonymous-replacement-v1"
REPLACEMENT_TARGET = 25_000
BIDNET_BASELINE_LOST = 22_555


def _is_bidnetish(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("source_id", "platform", "platform_family", "source_url", "detail_url")
    ).lower()
    return "bidnet" in blob


def _canonical_non_bidnet_counts() -> dict[str, Any]:
    """Compute live non-BidNet inventory using the same L23 live rules as coverage dashboard."""
    try:
        from phase_l.l23_full_population_funnel import load_store
        from phase_l.owner_ui_service import DEAD_FUNNEL_STATES, _is_available_rec

        store = load_store()
        live_total = 0
        non_bn = 0
        bn = 0
        by_plat: dict[str, int] = {}
        live_states = {
            "RAW",
            "ACCESSIBLE_PRODUCT",
            "FAST_RESEARCH_PENDING",
            "FAST_RESEARCH_COMPLETE",
            "DEEP_RESEARCH_PRIORITY",
            "DEEP_RESEARCH_IN_PROGRESS",
            "DEEP_RESEARCH_COMPLETE",
            "WATCH",
            "WATCH_FEDERAL_ACCESS",
            "WATCH_OTHER",
            "NEEDS_SOURCE_DATA",
            "LOW_PRIORITY_RESEARCH",
        }
        for rec in store.values() if isinstance(store, dict) else []:
            if not isinstance(rec, dict):
                continue
            st = str(rec.get("current_funnel_state") or "")
            if st in DEAD_FUNNEL_STATES:
                continue
            fresh = str(rec.get("freshness") or "").upper()
            if fresh in {"EXPIRED", "CANCELED", "CANCELLED"}:
                continue
            if not (_is_available_rec(rec) or st in live_states):
                continue
            live_total += 1
            plat = str(rec.get("platform") or rec.get("source_id") or "other")
            sid = str(rec.get("source_id") or "")
            if _is_bidnetish(rec) or "bidnet" in plat.lower() or "bidnet" in sid.lower():
                bn += 1
                by_plat["BidNet"] = by_plat.get("BidNet", 0) + 1
            else:
                non_bn += 1
                key = plat if len(plat) < 48 else plat[:48]
                by_plat[key] = by_plat.get(key, 0) + 1
        return {
            "canonical_live_total": live_total,
            "non_bidnet_live": non_bn,
            "bidnet_live": bn,
            "by_platform": dict(sorted(by_plat.items(), key=lambda kv: -kv[1])[:40]),
        }
    except Exception as exc:
        log.exception("canonical non-bidnet count failed")
        return {"error": f"{type(exc).__name__}: {exc}", "non_bidnet_live": 0, "bidnet_live": 0}


def _persist_report(name: str, report: dict[str, Any]) -> str | None:
    try:
        from m3_data_root import data_path

        path = data_path(name)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception:
        log.exception("persist %s failed", name)
        return None


def _nyc_live_structured_source() -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000")
    from discovery.constants import ADAPTER_LIVE_CAPABLE
    from discovery.structured_adapters import TIER_OFFICIAL, build_socrata_url

    params = {
        "$where": f"type_of_notice_description='Solicitation' AND due_date > '{now}'",
        "$limit": "2000",
        "$order": "due_date ASC",
    }
    return {
        "source_id": "structured_socrata_nyc_current_solicitations_live",
        "name": "NYC Current Solicitations (live due_date)",
        "list_url": build_socrata_url("data.cityofnewyork.us", "3khw-qi8f", params=params),
        "adapter_family": "live_structured",
        "platform_family": "Socrata",
        "adapter_kind": "socrata",
        "domain": "data.cityofnewyork.us",
        "dataset_id": "3khw-qi8f",
        "field_map": {
            "title": "short_title",
            "solicitation_number": ["pin", "request_id"],
            "deadline": "due_date",
            "open_date": "start_date",
            "buyer": "agency_name",
            "description": "additional_description_1",
            "category": "category_description",
            "status": "type_of_notice_description",
            "live_status_values": ["Solicitation"],
        },
        "role": "LIVE",
        "kind": "LOCAL",
        "state_code": "NY",
        "buyer_type": "CITY",
        "tier": TIER_OFFICIAL,
        "auth": "PUBLIC_NO_AUTH",
        "adapter_status": ADAPTER_LIVE_CAPABLE,
        "validation_candidate": True,
        "defaults": {
            "agency": "NYC",
            "state_code": "NY",
            "buyer_type": "CITY",
            "jurisdiction": "CITY",
            "status": "OPEN",
        },
    }


def discover_socrata_solicitation_catalog(*, limit: int = 80) -> list[dict[str, Any]]:
    """Search Socrata catalog for solicitation/bid datasets (anonymous)."""
    q = urllib.parse.urlencode(
        {
            "q": "solicitation OR \"open bids\" OR \"current bids\" OR \"bid opportunities\"",
            "only": "datasets",
            "limit": str(max(10, min(int(limit), 100))),
        }
    )
    url = "https://api.us.socrata.com/api/catalog/v1?" + q
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "M3AnonymousReplacement/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        cat = json.loads(resp.read())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in cat.get("results") or []:
        res = item.get("resource") or {}
        meta = item.get("metadata") or {}
        domain = str(meta.get("domain") or "")
        ds_id = str(res.get("id") or "")
        name = str(res.get("name") or "")
        if not domain or not ds_id:
            continue
        key = f"{domain}/{ds_id}"
        if key in seen:
            continue
        # Skip obvious non-live history/crime/spend
        low = f"{name} {' '.join(res.get('columns_name') or [])}".lower()
        if any(x in low for x in ("crime", "arrest", "911", "payroll", "lobbyist")):
            continue
        if any(x in low for x in ("tabulation", "award", "historical", "archive", "fy201", "fy2020", "fy2021", "fy2022", "fy2023")):
            # history — skip for live replacement volume
            continue
        if not any(x in low for x in ("solicit", "bid", "rfp", "rfq", "procurement", "opportunity")):
            continue
        seen.add(key)
        slug = re.sub(r"[^a-z0-9]+", "_", f"{domain}_{ds_id}".lower())[:70]
        list_url = f"https://{domain}/resource/{ds_id}.json?" + urllib.parse.urlencode({"$limit": "500"})
        out.append(
            {
                "source_id": f"structured_socrata_auto_{slug}",
                "name": name[:120],
                "list_url": list_url,
                "adapter_family": "live_structured",
                "platform_family": "Socrata",
                "domain": domain,
                "dataset_id": ds_id,
                "role": "LIVE",
                "auth": "PUBLIC_NO_AUTH",
                "validation_candidate": True,
                "auto_discovered": True,
                "field_map": {
                    "title": ["short_title", "title", "description", "solicitation_title", "project_description", "name"],
                    "solicitation_number": ["pin", "number", "solicitation_number", "request_id", "bid_number"],
                    "deadline": ["due_date", "closingdate", "close_date", "deadline", "end_date"],
                    "buyer": ["agency_name", "department", "agency", "buyer"],
                    "status": ["status", "type_of_notice_description", "stagename"],
                },
                "defaults": {"status": "OPEN"},
            }
        )
    return out


def run_anonymous_replacement_discovery(
    *,
    run_admission: bool = True,
    include_structured: bool = True,
    include_platform_catalog: bool = True,
    include_state_boards: bool = True,
    include_socrata_catalog: bool = True,
    include_opengov: bool = True,
    max_pages: int = 40,
    persist: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute replacement harvest — BidNet/Euna forced off."""
    from discovery_expansion.harvest import run_expansion_harvest

    run_id = run_id or f"ANON-REP-{uuid4().hex[:10]}"
    started = now_utc().isoformat()
    baseline = _canonical_non_bidnet_counts()
    baseline_non_bn = int(baseline.get("non_bidnet_live") or 0)

    admission = None
    if run_admission:
        from anonymous_source_admission_v1 import run_admission_matrix

        admission = run_admission_matrix()

    # Force abandon flags for this process
    os.environ.setdefault("M3_BIDNET_ABANDONED", "true")
    os.environ.setdefault("M3_EUNA_ABANDONED", "true")
    os.environ["BIDNET_AUTH_ENABLED"] = "false"
    os.environ["EUNA_AUTH_ENABLED"] = "false"

    # Inject NYC live + auto catalog into structured harvest via temporary registry extension
    extra_structured: list[dict[str, Any]] = [_nyc_live_structured_source()]
    catalog_discovered = 0
    if include_socrata_catalog:
        try:
            extra_structured.extend(discover_socrata_solicitation_catalog(limit=60))
            catalog_discovered = len(extra_structured) - 1
        except Exception:
            log.exception("Socrata catalog discovery failed")

    # Monkey-patch structured candidates for this run
    import discovery.structured_source_registry as ssr

    prev = getattr(ssr, "all_structured_live_candidates", None)

    def _augmented() -> list[dict[str, Any]]:
        base = list(prev() if callable(prev) else [])
        seen = {str(x.get("source_id")) for x in base if isinstance(x, dict)}
        for row in extra_structured:
            sid = str(row.get("source_id"))
            if sid and sid not in seen:
                base.append(row)
                seen.add(sid)
        return base

    ssr.all_structured_live_candidates = _augmented  # type: ignore[assignment]

    harvest = None
    try:
        # Exclude Bonfire from catalog families for this replacement run
        harvest = run_expansion_harvest(
            include_bidnet=False,  # NEVER BidNet
            include_structured=include_structured,
            include_platform_catalog=include_platform_catalog,
            max_pages=max_pages,
            max_catalog_entities_per_family=None,  # exhaust catalog
            max_structured_datasets=None,  # exhaust structured
            catalog_page_cap=None,
            persist=persist,
            run_id=run_id,
            exhaustive=True,
        )
    finally:
        if prev is not None:
            ssr.all_structured_live_candidates = prev  # type: ignore[assignment]

    # Filter Bonfire/Euna from harvest per_source counts for honesty
    if harvest and isinstance(harvest.get("per_source"), dict):
        cleaned = {}
        for sid, row in harvest["per_source"].items():
            plat = str((row or {}).get("platform_family") or "")
            if euna_abandoned() and ("bonfire" in plat.lower() or "euna" in plat.lower()):
                continue
            if bidnet_abandoned() and "bidnet" in plat.lower():
                continue
            cleaned[sid] = row
        harvest["per_source_anonymous_only"] = cleaned

    # Optional OpenGov public cascade (bounded)
    opengov_report = None
    if include_opengov:
        try:
            from opengov_discovery.cascade import run_opengov_cascade_discovery

            opengov_report = run_opengov_cascade_discovery(
                max_entities=int(os.environ.get("M3_ANON_OPENGOV_MAX_ENTITIES") or 80),
                max_pages=int(os.environ.get("M3_ANON_OPENGOV_MAX_PAGES") or 5),
                use_auth=False,
                allow_browser=False,
                persist=True,
            )
        except Exception as exc:
            opengov_report = {"error": f"{type(exc).__name__}: {exc}"}

    # State boards (validation candidates only)
    state_report = None
    if include_state_boards:
        state_report = _harvest_state_validation_candidates(max_pages=min(15, max_pages), persist=persist, run_id=f"{run_id}-states")

    after = _canonical_non_bidnet_counts()
    after_non_bn = int(after.get("non_bidnet_live") or 0)
    # Net-new vs abandon baseline (not vs pre-run snapshot alone) — OpenGov preexisting excluded
    baseline_doc = _load_or_seed_baseline(baseline_non_bn)
    baseline_frozen = int(baseline_doc.get("non_bidnet_live_at_abandon") or baseline_non_bn)
    net_new = max(0, after_non_bn - baseline_frozen)
    gap = max(0, REPLACEMENT_TARGET - net_new)

    # Family rollup from harvest
    family_rows: list[dict[str, Any]] = []
    per = (harvest or {}).get("per_source_anonymous_only") or (harvest or {}).get("per_source") or {}
    by_fam: dict[str, dict[str, Any]] = {}
    for sid, row in per.items():
        fam = str((row or {}).get("platform_family") or "Other")
        slot = by_fam.setdefault(
            fam,
            {"family": fam, "agencies_connected": 0, "live_raw": 0, "ok": 0, "failed": 0},
        )
        slot["agencies_connected"] += 1
        slot["live_raw"] += int((row or {}).get("raw") or 0)
        if (row or {}).get("ok"):
            slot["ok"] += 1
        else:
            slot["failed"] += 1
    for fam, slot in sorted(by_fam.items(), key=lambda kv: -kv[1]["live_raw"]):
        family_rows.append(
            {
                **slot,
                "accessible_document_packages": "unknown_pending_doc_stage",
                "status": "PRODUCING" if slot["ok"] else "FAILED_OR_EMPTY",
            }
        )

    report = {
        "kind": "AnonymousReplacementDiscoveryReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "policy": policy_status(),
        "bidnet_active_calls_remaining": 0,
        "replacement_target": REPLACEMENT_TARGET,
        "bidnet_baseline_lost_estimate": BIDNET_BASELINE_LOST,
        "baseline": baseline,
        "after": after,
        "new_unique_non_bidnet_this_run": net_new,
        "total_live_non_bidnet_inventory": after_non_bn,
        "replacement_gap_remaining": gap,
        "replacement_target_achieved": after_non_bn >= REPLACEMENT_TARGET,
        "socrata_catalog_datasets_added": catalog_discovered,
        "admission": {
            "by_access_class": (admission or {}).get("by_access_class"),
            "ingestible_count": (admission or {}).get("ingestible_count"),
            "targets_tested": (admission or {}).get("targets_tested"),
        }
        if admission
        else None,
        "harvest": {
            "raw_opportunities": (harvest or {}).get("raw_opportunities"),
            "unique_records": (harvest or {}).get("unique_records"),
            "sources_successful": (harvest or {}).get("sources_successful"),
            "sources_failed": (harvest or {}).get("sources_failed"),
            "platform_family_counts": (harvest or {}).get("platform_family_counts"),
            "canonical_merge": (harvest or {}).get("canonical_merge"),
        },
        "opengov": opengov_report,
        "state_boards": state_report,
        "source_family_table": family_rows,
        "notes": [
            "Existing OpenGov inventory is preserved but not double-counted as net-new unless newly harvested.",
            "PlanetBids pbsystem often returns AWS WAF challenge — classified CAPTCHA_BLOCKED, not ingested as live bids.",
            "Document-accessible counts require separate anonymous doc verification stage.",
            "FSPF job not reset; use append-missing-from-l23 to feed new IDs.",
        ],
    }
    path = _persist_report("m3_anonymous_replacement_discovery_last_report.json", report)
    report["persisted_path"] = path
    return report


def _harvest_state_validation_candidates(*, max_pages: int, persist: bool, run_id: str) -> dict[str, Any]:
    from discovery.live_fetchers import get_fetcher_for_platform, get_live_fetcher
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.state_matrix import STATE_MATRIX
    from discovery_expansion.harvest import _opp_to_record
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=5000,
            max_requests_per_source=40,
            max_pages_per_source=max_pages,
            max_records_per_source=2000,
            max_runtime_seconds=1800.0,
            max_retries=1,
            min_interval_seconds=0.4,
            timeout_seconds=25.0,
        ),
        authorize_live=True,
        user_agent="M3AnonymousStateBoards/1.0",
    )
    records: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    for st in STATE_MATRIX:
        if not st.get("validation_candidate"):
            continue
        if st.get("auth_required"):
            continue
        plat = str(st.get("platform_family") or "")
        if bidnet_abandoned() and "bidnet" in plat.lower():
            continue
        sid = f"state_{str(st.get('state') or '').lower()}"
        url = st.get("list_url")
        if not url:
            continue
        fetcher = get_fetcher_for_platform(plat) or get_live_fetcher(str(st.get("adapter_family") or "live_simple_html"))
        if fetcher is None:
            per_source[sid] = {"ok": False, "raw": 0, "source_stop_reason": "NO_FETCHER", "platform_family": plat}
            continue
        try:
            result = fetcher.fetch_listing(
                client,
                list_url=url,
                source_id=sid,
                max_pages=max_pages,
                pagination_exhaust=True,
                pagination_safety_max_pages=max(20, max_pages),
            )
            opps = result.get("opportunities") or []
            batch = [_opp_to_record(o, source_id=sid, platform=plat or "State", agency=st.get("name")) for o in opps]
            records.extend(batch)
            per_source[sid] = {
                "ok": len(batch) > 0,
                "raw": len(batch),
                "pages_scanned": result.get("pages_fetched"),
                "platform_family": plat,
                "state": st.get("state"),
                "source_stop_reason": "COMPLETED" if batch else "EMPTY",
            }
        except Exception as exc:
            per_source[sid] = {
                "ok": False,
                "raw": 0,
                "platform_family": plat,
                "state": st.get("state"),
                "source_stop_reason": "SOURCE_EXCEPTION",
                "root_cause": type(exc).__name__,
            }

    merge = None
    if persist and records:
        merge = merge_discovery_into_canonical(
            run_id=run_id,
            trigger="anonymous_replacement_states",
            records=records,
            sources_attempted=list(per_source.keys()),
            sources_succeeded=[k for k, v in per_source.items() if v.get("ok")],
            sources_failed=[k for k, v in per_source.items() if not v.get("ok")],
            source_counts=per_source,
            raw_opportunities_found=len(records),
            records_normalized=len(records),
            persist=True,
        )
    return {
        "states_attempted": len(per_source),
        "states_ok": sum(1 for v in per_source.values() if v.get("ok")),
        "raw": len(records),
        "per_source": per_source,
        "canonical_merge": {
            "new": (merge or {}).get("new_canonical_opportunities_added"),
            "available_after": (merge or {}).get("currently_available_after"),
        }
        if merge
        else None,
    }


def _load_or_seed_baseline(non_bn_now: int) -> dict[str, Any]:
    """Baseline freezes pre-existing non-BidNet inventory so OpenGov isn't counted as replacement."""
    try:
        from m3_data_root import data_path

        path = data_path("m3_anonymous_replacement_baseline_v1.json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        baseline = {
            "kind": "AnonymousReplacementBaseline",
            "seeded_at": now_utc().isoformat(),
            "non_bidnet_live_at_abandon": int(non_bn_now),
            "note": "Existing OpenGov/other non-BidNet inventory is baseline, not replacement volume",
        }
        path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        return baseline
    except Exception as exc:
        return {"non_bidnet_live_at_abandon": int(non_bn_now), "error": f"{type(exc).__name__}: {exc}"}


def replacement_progress_dashboard() -> dict[str, Any]:
    """Operator-facing replacement progress (honest counts)."""
    counts = _canonical_non_bidnet_counts()
    non_bn = int(counts.get("non_bidnet_live") or 0)
    baseline = _load_or_seed_baseline(non_bn)
    baseline_n = int(baseline.get("non_bidnet_live_at_abandon") or 0)
    # Net-new replacement volume excludes inventory that already existed when BidNet was abandoned
    net_new_replacement = max(0, non_bn - baseline_n)
    last = None
    try:
        from m3_data_root import data_path

        p = data_path("m3_anonymous_replacement_discovery_last_report.json")
        if p.exists():
            last = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        last = None
    admission = None
    try:
        from m3_data_root import data_path

        p = data_path("m3_anonymous_source_admission_matrix_v1.json")
        if p.exists():
            admission = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {
        "kind": "AnonymousReplacementProgress",
        "build": BUILD,
        "policy": policy_status(),
        "replacement_target": REPLACEMENT_TARGET,
        "bidnet_active_calls_remaining": 0,
        "total_live_non_bidnet_inventory": non_bn,
        "existing_non_bidnet_baseline": baseline_n,
        "new_unique_non_bidnet_replacement": net_new_replacement,
        "bidnet_live_retained_historical": int(counts.get("bidnet_live") or 0),
        "replacement_gap_remaining": max(0, REPLACEMENT_TARGET - net_new_replacement),
        "replacement_target_achieved": net_new_replacement >= REPLACEMENT_TARGET,
        "progress_pct": round(min(100.0, 100.0 * net_new_replacement / REPLACEMENT_TARGET), 2),
        "by_platform": counts.get("by_platform"),
        "canonical_live_total": counts.get("canonical_live_total"),
        "last_run": {
            "run_id": (last or {}).get("run_id"),
            "completed_at": (last or {}).get("completed_at"),
            "new_unique_non_bidnet_this_run": (last or {}).get("new_unique_non_bidnet_this_run"),
            "harvest": (last or {}).get("harvest"),
            "admission": (last or {}).get("admission"),
            "source_family_table": (last or {}).get("source_family_table"),
        }
        if last
        else None,
        "admission_matrix_summary": {
            "by_access_class": (admission or {}).get("by_access_class"),
            "ingestible_count": (admission or {}).get("ingestible_count"),
        }
        if admission
        else None,
        "existing_fspf_job_must_remain": "full_scale_production_funnel_v1-20261015T015645Z-23533d4d",
    }
