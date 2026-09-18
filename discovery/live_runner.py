"""Live discovery runner — listing-first, per-source budget isolated, full eligible coverage."""

from __future__ import annotations

import os
from typing import Any

from discovery.analytics import analyze_run_results, quality_sample
from discovery.classify import classify_discovery_opportunity, early_reject_reasons
from discovery.deadline import normalize_deadline
from discovery.deadline_viability import enrich_opportunity_deadline
from discovery.http_client import (
    BudgetExhausted,
    GlobalBudgetExhausted,
    PublicProcurementHttpClient,
    RequestBudget,
    RuntimeBudgetExhausted,
    SourceBudgetExhausted,
)
from discovery.live_fetchers import get_live_fetcher, should_fetch_detail
from discovery.persist import upsert_canonical_opportunity
from discovery.priority import compute_research_priority
from discovery.profiles import get_profile
from discovery.sam_policy import assert_no_broad_sam_discovery


def _candidate_sources(
    max_sources: int | None,
    *,
    require_verified: bool = False,
    all_eligible: bool = False,
) -> list[dict[str, Any]]:
    from discovery.selection import select_diversified_sources

    return select_diversified_sources(
        max_sources=max_sources,
        require_verified=require_verified,
        all_eligible=all_eligible,
    )


def _explicit_source_state(per: dict[str, Any]) -> str:
    """Map per-source result to an explicit accounting state."""
    if per.get("selection_state") and not per.get("attempt", True):
        return str(per["selection_state"])
    stop = str(per.get("source_stop_reason") or "").upper()
    if stop in {
        "AUTH_REQUIRED",
        "REGISTRATION_REQUIRED",
        "BOT_PROTECTED",
        "BACKOFF",
        "DISABLED",
        "DUPLICATE",
        "HEALTHY_ZERO",
        "PAGINATION_INCOMPLETE",
        "NO_FETCHER",
        "TECHNICAL_FAILURE",
        "SOURCE_EXCEPTION",
        "VALIDATION_FAILURE",
        "SOURCE_BUDGET_EXHAUSTED",
        "GLOBAL_BUDGET_EXHAUSTED",
        "RUNTIME_BUDGET_EXHAUSTED",
    }:
        return stop
    if per.get("ok") and int(per.get("raw") or 0) == 0:
        return "HEALTHY_ZERO"
    if per.get("ok"):
        if stop == "PAGINATION_INCOMPLETE":
            return "PAGINATION_INCOMPLETE"
        return "SUCCESS"
    if "AUTH" in stop:
        return "AUTH_REQUIRED"
    if "BOT" in stop or "CLOUDFLARE" in stop:
        return "BOT_PROTECTED"
    if "REGISTRATION" in stop:
        return "REGISTRATION_REQUIRED"
    return "TECHNICAL_FAILURE"


def _known_active_inventory(session: Any | None) -> dict[str, Any]:
    """Durable open-opportunity universe vs this-run fetches."""
    if session is None:
        return {
            "known_active_market_inventory": None,
            "note": "no_session",
        }
    try:
        from models import DiscoveredOpportunity

        closed = {"EXPIRED", "CANCELLED", "CANCELED", "AWARDED", "CLOSED"}
        rows = session.query(DiscoveredOpportunity).all()
        active = [r for r in rows if str(r.status or "OPEN").upper() not in closed]
        return {
            "known_active_market_inventory": len(active),
            "known_total_discovered": len(rows),
            "known_closed_or_expired": len(rows) - len(active),
        }
    except Exception as exc:
        return {"known_active_market_inventory": None, "error": str(exc)}


def run_live_discovery(
    session: Any | None = None,
    *,
    profile: str = "tiny",
    preview: bool = True,
    persist: bool = False,
    authorize_live: bool = False,
    transport: Any | None = None,
    source_ids: list[str] | None = None,
    max_sources: int | None = None,
    fetch_details: bool | None = None,
    fetch_documents: bool | None = None,
    on_source_complete: Any | None = None,
    candidates_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Controlled live/preview discovery — listing-first by default for TINY/BROAD.
    BROAD/NATIONAL: ALL eligible sources (priority = order only).
    Per-source budget exhaustion continues to next source.
    Optional on_source_complete(metrics, source_id) for coarse progress heartbeats.
    Optional candidates_override for tests (exact candidate list).
    """
    if persist:
        preview = False
    else:
        persist = False
        preview = True

    pol = assert_no_broad_sam_discovery()
    if not pol["broad_discovery_blocked"]:
        return {"error": "SAM broad discovery must remain blocked", "LIVE_API_REQUESTS": 0}

    if persist and session is None:
        return {"error": "persist requires session", "LIVE_API_REQUESTS": 0}

    inventory_before = _known_active_inventory(session)

    prof = get_profile(profile)
    budget: RequestBudget = prof["budget"]
    do_details = prof.get("fetch_details", False) if fetch_details is None else fetch_details
    do_documents = prof.get("fetch_documents", False) if fetch_documents is None else fetch_documents
    all_eligible = bool(prof.get("all_eligible_sources"))
    pagination_exhaust = bool(prof.get("pagination_exhaust"))
    pagination_safety_max_pages = int(prof.get("pagination_safety_max_pages") or budget.max_pages_per_source)

    client = PublicProcurementHttpClient(
        budget=budget,
        authorize_live=bool(authorize_live),
        transport=transport,
    )

    from discovery.selection import select_all_eligible_sources, _pool_map

    selection_bundle = select_all_eligible_sources(include_blocked_accounted=True)
    registered_sources = selection_bundle["registered_in_pool"]
    eligible_full = selection_bundle["eligible"]
    accounted_non_attempt = list(selection_bundle["accounted_non_attempt"])

    source_cap = max_sources if max_sources is not None else prof.get("max_sources")
    if candidates_override is not None:
        candidates = list(candidates_override)
        eligible_full = list(candidates_override)
        accounted_non_attempt = []
        registered_sources = max(registered_sources, len(candidates))
    elif source_ids:
        pool = {c["source_id"]: c for c in _candidate_sources(999, all_eligible=True)}
        pool.update(_pool_map())
        candidates = [pool[s] for s in source_ids if s in pool]
        if isinstance(source_cap, int) and source_cap > 0:
            candidates = candidates[:source_cap]
    elif all_eligible or source_cap is None:
        candidates = list(eligible_full)
    else:
        candidates = _candidate_sources(source_cap, all_eligible=False)

    metrics: dict[str, Any] = {
        "registered_sources": registered_sources,
        "eligible_sources": len(eligible_full),
        "sources_attempted": 0,
        "sources_successful": 0,
        "sources_failed": 0,
        "sources_healthy_zero": 0,
        "sources_auth_blocked": 0,
        "sources_registration_blocked": 0,
        "sources_bot_blocked": 0,
        "sources_backoff": 0,
        "sources_technical_failure": 0,
        "sources_pagination_incomplete": 0,
        "unattempted_eligible": 0,
        "raw_records": 0,
        "listing_records": 0,
        "normalized_records": 0,
        "unique_records": 0,
        "fetched_this_run": 0,
        "CORE_PRODUCT": 0,
        "PRODUCT_PLUS_SERVICE": 0,
        "UNKNOWN": 0,
        "SERVICE": 0,
        "CLEARLY_IRRELEVANT": 0,
        "expired": 0,
        "parser_warnings": [],
        "per_source": {},
        "listing_requests": 0,
        "detail_requests": 0,
        "document_requests": 0,
        "detail_eligible": 0,
        "details_fetched": 0,
        "document_links_discovered": 0,
        "documents_fetched": 0,
        "detail_fetches": 0,
        "documents_discovered": 0,
        "source_budget_exhausted": False,
        "global_budget_exhausted": False,
        "runtime_budget_exhausted": False,
        "pages_fetched_total": 0,
        "authoritative_productive_sources": 0,
        "fallback_productive_sources": 0,
        "productive_discovery_sources": 0,
        "SAM": 0,
        "OpenAI": 0,
        "paid": 0,
    }
    collected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    sources_contacted: list[dict[str, Any]] = []
    attempted_ids: set[str] = set()
    shared_list_urls: dict[str, str] = {}  # normalized list_url → first productive source_id

    # Pre-account blocked/ineligible so they never appear as silent omissions
    for row in accounted_non_attempt:
        sid = row["source_id"]
        metrics["per_source"][sid] = {
            "ok": False,
            "attempt": False,
            "selection_state": row.get("selection_state") or row.get("accounted_reason"),
            "source_stop_reason": row.get("accounted_reason") or row.get("selection_state"),
            "explicit_state": row.get("selection_state") or "DISABLED",
        }

    stop_run_partial = False
    partial_reason = None

    for cand in candidates:
        if metrics["raw_records"] >= prof["max_records_total"]:
            stop_run_partial = True
            partial_reason = "MAX_RECORDS_TOTAL"
            break
        if metrics["global_budget_exhausted"] or metrics["runtime_budget_exhausted"]:
            stop_run_partial = True
            partial_reason = (
                "GLOBAL_BUDGET_EXHAUSTED"
                if metrics["global_budget_exhausted"]
                else "RUNTIME_BUDGET_EXHAUSTED"
            )
            break

        sid = cand["source_id"]
        # Authoritative SAM API sources are handled by federal_sam_ingest — not HTML fetchers
        if cand.get("api_source") or cand.get("adapter_family") == "live_sam_api" or sid == "fed_sam_contract_opportunities":
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": False,
                "selection_state": "DEFERRED_TO_FEDERAL_SAM_INGEST",
                "source_stop_reason": "DEFERRED_TO_FEDERAL_SAM_INGEST",
                "explicit_state": "DEFERRED_API",
            }
            continue

        # Failure-aware backoff (live runs only) — account explicitly, do not hammer
        if authorize_live:
            try:
                from discovery.source_backoff import should_skip_source_for_backoff
                from procurement_source_registry import ProcurementSourceRegistry

                reg_row = ProcurementSourceRegistry().get(sid) or {}
                skip = should_skip_source_for_backoff(reg_row)
                if skip.get("skip"):
                    metrics["sources_backoff"] += 1
                    metrics["per_source"][sid] = {
                        "ok": False,
                        "attempt": False,
                        "source_stop_reason": "BACKOFF",
                        "explicit_state": skip.get("accounted_state") or "BACKOFF",
                        "backoff_until": skip.get("backoff_until"),
                        "root_cause": skip.get("failure_class"),
                    }
                    attempted_ids.add(sid)  # accounted — not unattempted silent skip
                    continue
            except Exception:
                pass

        # Same public listing URL already exhausted this cycle — do not re-download
        list_key = str(cand.get("list_url") or "").rstrip("/").lower()
        if list_key and list_key in shared_list_urls:
            metrics["sources_attempted"] += 1
            attempted_ids.add(sid)
            metrics["per_source"][sid] = {
                "ok": True,
                "attempt": True,
                "raw": 0,
                "unique": 0,
                "source_stop_reason": "SHARED_LIST_URL",
                "explicit_state": "SHARED_WITH_" + shared_list_urls[list_key],
                "shared_with": shared_list_urls[list_key],
            }
            continue

        metrics["sources_attempted"] += 1
        attempted_ids.add(sid)
        fetcher = get_live_fetcher(cand["adapter_family"])
        if not fetcher:
            metrics["sources_failed"] += 1
            metrics["sources_technical_failure"] += 1
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": "no_fetcher",
                "source_stop_reason": "NO_FETCHER",
                "explicit_state": "TECHNICAL_FAILURE",
            }
            continue

        before = client.request_count
        source_stop_reason = None
        try:
            result = fetcher.fetch_listing(
                client,
                list_url=cand["list_url"],
                source_id=sid,
                max_pages=budget.max_pages_per_source,
                pagination_exhaust=pagination_exhaust,
                pagination_safety_max_pages=pagination_safety_max_pages,
            )
            metrics["listing_requests"] += int(result.get("pages_fetched") or 1)
            metrics["pages_fetched_total"] += int(result.get("pages_fetched") or 0)
            sources_contacted.append(
                {"source_id": sid, "url": cand["list_url"], "name": cand.get("name")}
            )

            validation = result.get("validation") or {}
            pag_complete = bool(result.get("pagination_complete"))
            pag_stop = str(result.get("pagination_stop_reason") or "")
            pages_fetched = int(result.get("pages_fetched") or 0)

            if not validation.get("valid"):
                fail_type = (validation.get("failure_type") or "VALIDATION_FAILURE").upper()
                metrics["sources_failed"] += 1
                metrics["parser_warnings"].extend(validation.get("warnings") or [])
                if fail_type == "AUTH_REQUIRED":
                    metrics["sources_auth_blocked"] += 1
                elif fail_type == "BOT_PROTECTED":
                    metrics["sources_bot_blocked"] += 1
                elif fail_type == "REGISTRATION_REQUIRED":
                    metrics["sources_registration_blocked"] += 1
                else:
                    metrics["sources_technical_failure"] += 1
                metrics["per_source"][sid] = {
                    "ok": False,
                    "attempt": True,
                    "validation": validation,
                    "requests": client.request_count - before,
                    "listing_requests": pages_fetched or 1,
                    "pages_fetched": pages_fetched,
                    "records_fetched": int(result.get("records_fetched") or 0),
                    "pagination_complete": pag_complete,
                    "pagination_stop_reason": pag_stop or fail_type,
                    "source_reported_total": result.get("source_reported_total"),
                    "source_stop_reason": fail_type,
                    "source_budget_exhausted": False,
                    "explicit_state": fail_type
                    if fail_type
                    in {"AUTH_REQUIRED", "BOT_PROTECTED", "REGISTRATION_REQUIRED"}
                    else "TECHNICAL_FAILURE",
                    "beyond_page_1": bool(result.get("beyond_page_1")),
                }
                continue

            opps = result.get("opportunities") or []
            metrics["listing_records"] += len(opps)

            if pag_stop == "PAGINATION_INCOMPLETE":
                metrics["sources_pagination_incomplete"] += 1
                source_stop_reason = "PAGINATION_INCOMPLETE"
            elif len(opps) == 0:
                metrics["sources_healthy_zero"] += 1
                metrics["sources_successful"] += 1
                source_stop_reason = "HEALTHY_ZERO"
            else:
                metrics["sources_successful"] += 1

            src_raw = 0
            src_unique = 0
            src_product = 0

            for opp in opps:
                if src_raw >= budget.max_records_per_source:
                    source_stop_reason = source_stop_reason or "SOURCE_RECORD_CAP"
                    break
                if metrics["raw_records"] >= prof["max_records_total"]:
                    break

                opp.source_id = sid
                if cand.get("state_code") and not opp.state_code:
                    opp.state_code = cand["state_code"]
                if cand["kind"] == "STATE":
                    opp.jurisdiction = "STATE"
                    opp.buyer_type = opp.buyer_type or "STATE"
                elif cand["kind"] == "LOCAL":
                    opp.jurisdiction = opp.jurisdiction or "CITY"
                elif cand["kind"] == "COOPERATIVE":
                    opp.jurisdiction = "COOPERATIVE"
                    opp.buyer_type = "COOPERATIVE"
                elif cand["kind"] == "FEDERAL":
                    opp.jurisdiction = "FEDERAL"
                elif cand["kind"] == "NETWORK":
                    opp.jurisdiction = opp.jurisdiction or "MULTI_AGENCY_NETWORK"
                    opp.buyer_type = opp.buyer_type or "MULTI_AGENCY_NETWORK"

                for d in opp.document_links or []:
                    if d.get("url"):
                        metrics["document_links_discovered"] += 1
                        metrics["documents_discovered"] += 1
                        d.setdefault("discovered_only", True)
                        d.setdefault("document_fetched", False)

                cls = classify_discovery_opportunity(
                    title=opp.title, description=opp.description, status=opp.status
                )
                dl = normalize_deadline(
                    opp.deadline_raw,
                    timezone_hint=opp.deadline_timezone,
                    timezone_explicit=opp.deadline_tz_confidence == "KNOWN",
                )
                reject = early_reject_reasons(
                    title=opp.title,
                    description=opp.description,
                    status=opp.status,
                    deadline_passed=bool(dl.get("deadline_passed")),
                    classification=cls["classification"],
                    estimated_value=opp.estimated_value,
                    estimated_value_status=opp.estimated_value_status,
                )

                metrics["raw_records"] += 1
                metrics["normalized_records"] += 1
                metrics["fetched_this_run"] += 1
                src_raw += 1
                metrics[cls["classification"]] = metrics.get(cls["classification"], 0) + 1

                if dl.get("deadline_passed"):
                    metrics["expired"] += 1

                # UNKNOWN survives — only SERVICE / CLEARLY_IRRELEVANT / deadline reject early
                if reject["reject"] or cls["classification"] == "SERVICE":
                    continue

                gate = should_fetch_detail(
                    opp,
                    classification=cls["classification"],
                    fetch_details_enabled=bool(do_details),
                )
                if gate.get("earned"):
                    metrics["detail_eligible"] += 1

                if gate.get("fetch") and opp.detail_url and (authorize_live or transport is not None):
                    try:
                        detail = fetcher.fetch_detail(client, detail_url=opp.detail_url, source_id=sid)
                        metrics["detail_requests"] += 1
                        metrics["details_fetched"] += 1
                        metrics["detail_fetches"] += 1
                        for d in detail.get("documents") or []:
                            if d.get("url"):
                                existing = {x.get("url") for x in (opp.document_links or [])}
                                if d["url"] not in existing:
                                    d["discovered_only"] = True
                                    d["document_fetched"] = False
                                    opp.document_links.append(d)
                                    metrics["document_links_discovered"] += 1
                                    metrics["documents_discovered"] += 1
                        if do_documents:
                            metrics["document_requests"] += 0
                    except SourceBudgetExhausted:
                        source_stop_reason = "SOURCE_BUDGET_EXHAUSTED"
                        metrics["source_budget_exhausted"] = True
                        break
                    except GlobalBudgetExhausted:
                        metrics["global_budget_exhausted"] = True
                        source_stop_reason = "GLOBAL_BUDGET_EXHAUSTED"
                        break
                    except RuntimeBudgetExhausted:
                        metrics["runtime_budget_exhausted"] = True
                        source_stop_reason = "RUNTIME_BUDGET_EXHAUSTED"
                        break
                    except Exception:
                        pass

                if metrics["global_budget_exhausted"] or metrics["runtime_budget_exhausted"]:
                    break

                pri = compute_research_priority(
                    classification=cls["classification"],
                    estimated_value=opp.estimated_value,
                    estimated_value_status=opp.estimated_value_status,
                    trust_tier=opp.trust_tier,
                    document_count=len(opp.document_links or []),
                    reject=False,
                )

                key = f"{sid}|{opp.external_id}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    metrics["unique_records"] += 1
                    src_unique += 1
                    if cls["classification"] in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}:
                        src_product += 1
                    row = {
                        **opp.to_dict(),
                        "product_classification": cls["classification"],
                        "research_priority": pri["research_priority"],
                        "source_id": sid,
                        "deadline_tz_confidence": dl.get("timezone_confidence"),
                        "document_link_discovered": bool(opp.document_links),
                        "document_fetched": False,
                    }
                    row = enrich_opportunity_deadline(row)
                    collected.append(row)
                    if persist and session is not None:
                        upsert_canonical_opportunity(session, opp, dry_run=False)

            explicit = _explicit_source_state(
                {
                    "ok": True,
                    "raw": src_raw,
                    "source_stop_reason": source_stop_reason
                    or ("HEALTHY_ZERO" if src_raw == 0 else "SUCCESS"),
                }
            )
            metrics["per_source"][sid] = {
                "ok": True,
                "attempt": True,
                "raw": src_raw,
                "unique": src_unique,
                "product_candidates": src_product,
                "validation": validation.get("health_status"),
                "url": cand["list_url"],
                "requests": client.request_count - before,
                "listing_requests": pages_fetched or 1,
                "detail_requests": 0,
                "pages_fetched": pages_fetched,
                "records_fetched": int(result.get("records_fetched") or src_raw),
                "pagination_complete": pag_complete,
                "pagination_stop_reason": pag_stop or source_stop_reason or "COMPLETED",
                "source_reported_total": result.get("source_reported_total"),
                "beyond_page_1": bool(result.get("beyond_page_1")),
                "source_stop_reason": source_stop_reason
                or ("HEALTHY_ZERO" if src_raw == 0 else "COMPLETED"),
                "source_budget_exhausted": source_stop_reason == "SOURCE_BUDGET_EXHAUSTED",
                "explicit_state": explicit,
                "bootstrap_complete": bool(pag_complete)
                and explicit in {"SUCCESS", "HEALTHY_ZERO"},
            }
            if list_key and (src_raw > 0 or pages_fetched > 0):
                shared_list_urls[list_key] = sid

        except SourceBudgetExhausted as exc:
            metrics["source_budget_exhausted"] = True
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": str(exc),
                "source_stop_reason": "SOURCE_BUDGET_EXHAUSTED",
                "source_budget_exhausted": True,
                "requests": client.request_count - before,
                "isolated": True,
                "explicit_state": "SOURCE_BUDGET_EXHAUSTED",
            }
            continue
        except GlobalBudgetExhausted as exc:
            metrics["global_budget_exhausted"] = True
            stop_run_partial = True
            partial_reason = "GLOBAL_BUDGET_EXHAUSTED"
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": str(exc),
                "source_stop_reason": "GLOBAL_BUDGET_EXHAUSTED",
                "requests": client.request_count - before,
                "explicit_state": "GLOBAL_BUDGET_EXHAUSTED",
            }
            break
        except RuntimeBudgetExhausted as exc:
            metrics["runtime_budget_exhausted"] = True
            stop_run_partial = True
            partial_reason = "RUNTIME_BUDGET_EXHAUSTED"
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": str(exc),
                "source_stop_reason": "RUNTIME_BUDGET_EXHAUSTED",
                "requests": client.request_count - before,
                "explicit_state": "RUNTIME_BUDGET_EXHAUSTED",
            }
            break
        except BudgetExhausted as exc:
            msg = str(exc).lower()
            if "per-source" in msg:
                metrics["source_budget_exhausted"] = True
                metrics["per_source"][sid] = {
                    "ok": False,
                    "attempt": True,
                    "error": str(exc),
                    "source_stop_reason": "SOURCE_BUDGET_EXHAUSTED",
                    "source_budget_exhausted": True,
                    "isolated": True,
                    "explicit_state": "SOURCE_BUDGET_EXHAUSTED",
                }
                continue
            if "runtime" in msg or "max runtime" in msg:
                metrics["runtime_budget_exhausted"] = True
                stop_run_partial = True
                partial_reason = "RUNTIME_BUDGET_EXHAUSTED"
                metrics["per_source"][sid] = {
                    "ok": False,
                    "attempt": True,
                    "error": str(exc),
                    "source_stop_reason": "RUNTIME_BUDGET_EXHAUSTED",
                    "explicit_state": "RUNTIME_BUDGET_EXHAUSTED",
                }
                break
            metrics["global_budget_exhausted"] = True
            stop_run_partial = True
            partial_reason = "GLOBAL_BUDGET_EXHAUSTED"
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": str(exc),
                "source_stop_reason": "GLOBAL_BUDGET_EXHAUSTED",
                "explicit_state": "GLOBAL_BUDGET_EXHAUSTED",
            }
            break
        except Exception as exc:
            metrics["sources_failed"] += 1
            metrics["sources_technical_failure"] += 1
            metrics["per_source"][sid] = {
                "ok": False,
                "attempt": True,
                "error": str(exc),
                "source_stop_reason": "SOURCE_EXCEPTION",
                "isolated": True,
                "explicit_state": "TECHNICAL_FAILURE",
            }
            continue

        if callable(on_source_complete):
            try:
                on_source_complete(dict(metrics), sid)
            except Exception:
                pass

        if metrics["global_budget_exhausted"] or metrics["runtime_budget_exhausted"]:
            stop_run_partial = True
            partial_reason = partial_reason or (
                "GLOBAL_BUDGET_EXHAUSTED"
                if metrics["global_budget_exhausted"]
                else "RUNTIME_BUDGET_EXHAUSTED"
            )
            break

    # --- Federal SAM authoritative enumeration (opt-in; scarcity-gated separately) ---
    federal_sam_meta: dict[str, Any] = {"executed": False}
    if authorize_live and prof["name"] in {"BROAD", "NATIONAL"} and not stop_run_partial:
        try:
            from discovery.federal_sam_ingest import (
                federal_sam_discovery_enabled,
                reconcile_sam_page_counts,
                run_federal_sam_bootstrap,
            )
            from discovery.dla_product_extract import enrich_with_dla_structure, classify_federal_product_cheap

            # NATIONAL/BROAD: Federal SAM is first-class when API key present (opt-out via SAM_FEDERAL_DISCOVERY_ENABLED=0)
            _fed_env = (os.environ.get("SAM_FEDERAL_DISCOVERY_ENABLED") or "").strip().lower()
            _fed_opt_out = _fed_env in {"0", "false", "no", "off"}
            _fed_on = (
                (not _fed_opt_out)
                and (
                    federal_sam_discovery_enabled(authorize=False)
                    or _fed_env in {"1", "true", "yes"}
                    or bool((os.environ.get("SAM_GOV_API_KEY") or "").strip())
                )
            )
            if _fed_on:
                # Use remaining budget; NATIONAL may raise SAM_API_CALL_LIMIT in env for bootstrap
                fb_sam = run_federal_sam_bootstrap(
                    authorize_live=True,
                    authorize_federal_sam=True,
                    max_api_calls=None,
                    days_back=int(os.environ.get("SAM_FEDERAL_DAYS_BACK") or "30"),
                    chunk_days=int(os.environ.get("SAM_FEDERAL_CHUNK_DAYS") or "7"),
                    resume=True,
                )
                federal_sam_meta = {k: v for k, v in fb_sam.items() if k != "opportunities"}
                federal_sam_meta["count_reconciliation"] = reconcile_sam_page_counts(
                    fb_sam.get("authoritative_window_totals") or [],
                    int(fb_sam.get("unique_new") or 0),
                )
                metrics["SAM"] = int(metrics.get("SAM") or 0) + int(fb_sam.get("LIVE_SAM_CALLS") or 0)
                sid = "fed_sam_contract_opportunities"
                src_raw = 0
                src_unique = 0
                for raw_row in fb_sam.get("opportunities") or []:
                    if metrics["raw_records"] >= prof["max_records_total"]:
                        break
                    row = enrich_with_dla_structure(dict(raw_row))
                    screen = classify_federal_product_cheap(row)
                    row.update(screen)
                    cls_label = screen.get("product_classification") or "UNKNOWN"
                    # Do not early-reject UNKNOWN Federal notices
                    dl = normalize_deadline(row.get("deadline_raw"))
                    if dl.get("deadline_passed") and row.get("notice_semantic_class") != "AWARD_OR_HISTORY":
                        metrics["expired"] = metrics.get("expired", 0) + 1
                        # Awards/historical already classified — skip bid pipeline
                    if row.get("notice_semantic_class") == "AWARD_OR_HISTORY":
                        # Keep in Federal metrics but do not inflate bid-ready survivors
                        metrics["raw_records"] += 1
                        src_raw += 1
                        metrics["AWARD_OR_HISTORY"] = metrics.get("AWARD_OR_HISTORY", 0) + 1
                        continue
                    metrics["raw_records"] += 1
                    metrics["fetched_this_run"] += 1
                    metrics["normalized_records"] += 1
                    src_raw += 1
                    metrics[cls_label] = metrics.get(cls_label, 0) + 1
                    if cls_label == "SERVICE":
                        continue
                    key = f"{sid}|{row.get('external_id') or row.get('notice_id')}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        metrics["unique_records"] += 1
                        src_unique += 1
                        row["source_id"] = sid
                        row["jurisdiction"] = "FEDERAL"
                        row = enrich_opportunity_deadline(row)
                        collected.append(row)
                metrics["per_source"][sid] = {
                    "ok": bool(fb_sam.get("executed")) and src_raw > 0,
                    "attempt": True,
                    "raw": src_raw,
                    "unique": src_unique,
                    "source_stop_reason": fb_sam.get("stop_reason")
                    or ("COMPLETED" if fb_sam.get("pagination_complete") else "PAGINATION_INCOMPLETE"),
                    "pagination_complete": bool(fb_sam.get("pagination_complete")),
                    "explicit_state": "SUCCESS" if src_unique > 0 else "HEALTHY_ZERO",
                    "coverage_state": fb_sam.get("coverage_state"),
                }
                if src_unique > 0:
                    metrics["sources_successful"] += 1
                    metrics["productive_discovery_sources"] += 1
                    metrics["authoritative_productive_sources"] += 1
        except Exception as exc:  # noqa: BLE001
            federal_sam_meta = {"executed": False, "error": str(exc)[:300]}

    # Persist Federal/DLA coverage snapshot when SAM enumeration ran
    if federal_sam_meta.get("executed"):
        try:
            from discovery.dla_reconciliation import (
                build_dla_coverage_matrix,
                build_dla_from_sam,
                build_dla_source_reconciliation,
                build_federal_discovery_gap_queue,
            )
            from discovery.dla_source_map import build_dla_source_map_report, classify_dibbs_access_from_metrics
            from discovery.federal_dla_coverage import build_coverage_snapshot

            sam_opps = []
            # Re-collect Federal rows already in collected for this source
            sam_opps = [r for r in collected if r.get("source_id") == "fed_sam_contract_opportunities"]
            dla_pack = build_dla_from_sam(sam_opps)
            dibbs_metrics = metrics["per_source"].get("fed_dla_dibbs_rfq") or {}
            dibbs_state = classify_dibbs_access_from_metrics(dibbs_metrics)
            recon = build_dla_source_reconciliation(sam_dla=dla_pack.get("opportunities") or [], dibbs_rows=[])
            matrix = build_dla_coverage_matrix(
                sam_dla=dla_pack.get("opportunities") or [],
                dibbs_access_state=dibbs_state,
            )
            gaps = build_federal_discovery_gap_queue(
                sam_result=federal_sam_meta,
                dibbs_probe={"access_state": dibbs_state},
                recon=recon,
                matrix=matrix,
            )
            # Product class counts across all federal
            from collections import Counter

            fed_prod = Counter(
                str(r.get("federal_product_class") or r.get("product_classification") or "UNKNOWN")
                for r in sam_opps
            )
            federal_sam_meta["federal_product_counts"] = dict(fed_prod)
            federal_sam_meta["by_semantic"] = federal_sam_meta.get("by_semantic") or {}
            snap = build_coverage_snapshot(
                federal_sam={**federal_sam_meta, "unique_new": len(sam_opps)},
                dla_from_sam=dla_pack,
                dibbs_probe={"access_state": dibbs_state},
                reconciliation=recon,
                coverage_matrix=matrix,
                gap_queue=gaps,
            )
            snap["federal_product_likely"] = fed_prod.get("FEDERAL_PRODUCT_LIKELY", 0) + fed_prod.get(
                "CORE_PRODUCT", 0
            )
            snap["federal_unknown"] = fed_prod.get("FEDERAL_UNKNOWN", 0) + fed_prod.get("UNKNOWN", 0)
            from discovery.federal_dla_coverage import save_federal_dla_coverage

            save_federal_dla_coverage(snap)
            federal_sam_meta["dla_source_map"] = build_dla_source_map_report(
                dibbs_probe={"access_state": dibbs_state},
                sam_dla_count=int(dla_pack.get("current_unique") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            federal_sam_meta["coverage_persist_error"] = str(exc)[:200]

    # --- Federal/DLA fallback when direct DIBBS is blocked ---
    dla_fallback_meta: dict[str, Any] = {"executed": False}
    dibbs_ids = {"fed_dla_dibbs_rfq", "fed_dla_dibbs_rfq_by_fsc"}
    dibbs_blocked = False
    for did in dibbs_ids:
        row = metrics["per_source"].get(did) or {}
        stop = str(row.get("source_stop_reason") or row.get("explicit_state") or "").upper()
        if stop in {"BOT_PROTECTED", "AUTH_REQUIRED", "HTTP_403", "VALIDATION_FAILURE"} or (
            row.get("ok") is False and did in attempted_ids
        ):
            dibbs_blocked = True
        if did not in attempted_ids:
            dibbs_blocked = True
    if authorize_live and prof["name"] in {"BROAD", "NATIONAL"} and not stop_run_partial:
        try:
            from discovery.dla_fallback import run_dla_discovery_fallback

            fb = run_dla_discovery_fallback(
                authorize_live=True,
                allow_web_search=True,
                dibbs_blocked=dibbs_blocked or True,
            )
            dla_fallback_meta = {k: v for k, v in fb.items() if k != "opportunities"}
            metrics["SAM"] = int(fb.get("LIVE_SAM_CALLS") or 0)
            metrics["OpenAI"] = int(fb.get("OpenAI") or 0)
            metrics["paid"] = int(fb.get("paid") or 0)
            fb_opps = fb.get("opportunities") or []
            sid = "fed_dla_sam_cross_publish"
            src_raw = 0
            src_unique = 0
            for opp in fb_opps:
                if metrics["raw_records"] >= prof["max_records_total"]:
                    break
                opp.jurisdiction = "FEDERAL"
                cls = classify_discovery_opportunity(
                    title=opp.title, description=opp.description, status=opp.status
                )
                dl = normalize_deadline(opp.deadline_raw)
                reject = early_reject_reasons(
                    title=opp.title,
                    description=opp.description,
                    status=opp.status,
                    deadline_passed=bool(dl.get("deadline_passed")),
                    classification=cls["classification"],
                )
                metrics["raw_records"] += 1
                metrics["fetched_this_run"] += 1
                metrics["normalized_records"] += 1
                src_raw += 1
                metrics[cls["classification"]] = metrics.get(cls["classification"], 0) + 1
                if reject["reject"] or cls["classification"] == "SERVICE":
                    continue
                pri = compute_research_priority(
                    classification=cls["classification"],
                    estimated_value=opp.estimated_value,
                    estimated_value_status=opp.estimated_value_status or "UNKNOWN",
                    trust_tier=opp.trust_tier,
                    document_count=len(opp.document_links or []),
                    reject=False,
                )
                key = f"{opp.source_id}|{opp.external_id}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    metrics["unique_records"] += 1
                    src_unique += 1
                    row = {
                        **opp.to_dict(),
                        "product_classification": cls["classification"],
                        "research_priority": pri["research_priority"],
                        "source_id": opp.source_id,
                        "discovery_provenance_tier": (opp.raw_metadata or {}).get("provenance_tier"),
                        "authority_verification_state": (opp.raw_metadata or {}).get(
                            "authority_verification_state"
                        ),
                    }
                    row = enrich_opportunity_deadline(row)
                    collected.append(row)
                    if persist and session is not None:
                        upsert_canonical_opportunity(session, opp, dry_run=False)
            if src_raw > 0:
                metrics["sources_successful"] += 1
                metrics["sources_attempted"] += 1
                attempted_ids.add(sid)
                metrics["per_source"][sid] = {
                    "ok": True,
                    "attempt": True,
                    "raw": src_raw,
                    "unique": src_unique,
                    "source_stop_reason": "COMPLETED",
                    "explicit_state": "SUCCESS",
                    "fallback_route": True,
                    "provenance_tier": "TIER_A_AUTHORITATIVE",
                    "routes_tried": fb.get("routes_tried"),
                }
                metrics["authoritative_productive_sources"] = (
                    metrics.get("authoritative_productive_sources") or 0
                ) + 1
            elif fb.get("executed"):
                metrics["per_source"][sid] = {
                    "ok": False,
                    "attempt": True,
                    "source_stop_reason": "HEALTHY_ZERO",
                    "explicit_state": "HEALTHY_ZERO",
                    "fallback_route": True,
                    "sam_result": fb.get("sam_result"),
                }
        except Exception as exc:
            dla_fallback_meta = {"executed": False, "error": str(exc)}

    # Classify all attempted sources with precise taxonomy
    try:
        from discovery.source_failure_taxonomy import classify_root_cause

        for sid, prow in list(metrics["per_source"].items()):
            if not isinstance(prow, dict):
                continue
            cls = classify_root_cause(prow)
            prow["root_cause"] = cls.get("primary")
            prow["access_outcome"] = cls.get("access_outcome")
            prow["software_fixable"] = cls.get("software_fixable")
            prow["external_access_block"] = cls.get("external_access_block")
            prow["backoff_hours"] = cls.get("backoff_hours")
    except Exception:
        pass

    # Remaining eligible sources not attempted (cost/runtime/record cap)
    remaining = [c for c in candidates if c["source_id"] not in attempted_ids]
    # Also count eligible_full not in candidates when TINY caps — those are intentional for tiny only
    if all_eligible or source_cap is None:
        for c in remaining:
            metrics["unattempted_eligible"] += 1
            metrics["per_source"][c["source_id"]] = {
                "ok": False,
                "attempt": False,
                "source_stop_reason": "UNATTEMPTED_ELIGIBLE",
                "explicit_state": "UNATTEMPTED_ELIGIBLE",
                "partial_reason": partial_reason or "RUN_STOPPED",
            }
        if remaining:
            stop_run_partial = True
            partial_reason = partial_reason or "UNATTEMPTED_ELIGIBLE_REMAIN"

    # Productive coverage (attempted ≠ covered)
    productive_ids = [
        sid
        for sid, prow in metrics["per_source"].items()
        if isinstance(prow, dict) and prow.get("ok") and int(prow.get("raw") or 0) > 0
    ]
    metrics["productive_discovery_sources"] = len(productive_ids)
    metrics["authoritative_productive_sources"] = metrics.get("authoritative_productive_sources") or sum(
        1
        for sid in productive_ids
        if not (metrics["per_source"].get(sid) or {}).get("fallback_route")
        or (metrics["per_source"].get(sid) or {}).get("provenance_tier") == "TIER_A_AUTHORITATIVE"
    )
    # Count SAM cross-publish as authoritative productive separately already incremented

    inventory_after = _known_active_inventory(session)

    unique = metrics["unique_records"] or 0
    product_cands = metrics.get("CORE_PRODUCT", 0) + metrics.get("PRODUCT_PLUS_SERVICE", 0)
    req = client.request_count or 0
    metrics["requests_per_unique_opportunity"] = round(req / unique, 3) if unique else None
    metrics["requests_per_product_candidate"] = round(req / product_cands, 3) if product_cands else None
    metrics["fetched_this_run"] = metrics["raw_records"]
    metrics["known_active_market_inventory_before"] = inventory_before.get("known_active_market_inventory")
    metrics["known_active_market_inventory"] = inventory_after.get("known_active_market_inventory")
    metrics["known_total_discovered"] = inventory_after.get("known_total_discovered")

    run_status = "PARTIAL_DISCOVERY_RUN" if stop_run_partial else "COMPLETE"
    if stop_run_partial and not partial_reason:
        partial_reason = "PARTIAL"

    analytics = analyze_run_results(collected)
    samples = quality_sample(collected)

    completeness = {
        "run_status": run_status,
        "partial_reason": partial_reason,
        "registered_sources": registered_sources,
        "eligible_sources": len(eligible_full),
        "attempted_sources": metrics["sources_attempted"],
        "successful_sources": metrics["sources_successful"],
        "productive_discovery_sources": metrics["productive_discovery_sources"],
        "authoritative_productive_sources": metrics["authoritative_productive_sources"],
        "fallback_productive_sources": metrics["fallback_productive_sources"],
        "healthy_zero_sources": metrics["sources_healthy_zero"],
        "auth_blocked": metrics["sources_auth_blocked"],
        "registration_blocked": metrics["sources_registration_blocked"],
        "bot_blocked": metrics["sources_bot_blocked"],
        "backoff": metrics["sources_backoff"],
        "technical_failures": metrics["sources_technical_failure"],
        "pagination_incomplete": metrics["sources_pagination_incomplete"],
        "unattempted_eligible": metrics["unattempted_eligible"],
        "remaining_eligible_count": len(remaining) if (all_eligible or source_cap is None) else 0,
        "fetched_this_run": metrics["fetched_this_run"],
        "unique_records_fetched": metrics["unique_records"],
        "known_active_market_inventory": metrics["known_active_market_inventory"],
        "pages_fetched_total": metrics["pages_fetched_total"],
        "note": "attempted≠productive≠market_covered",
    }

    # Persist one-shot baseline with classifications when metrics present
    try:
        from discovery.source_baseline import build_eligible_universe_snapshot, persist_baseline

        snap = build_eligible_universe_snapshot(
            per_source_metrics=metrics["per_source"],
            run_id=None,
        )
        persist_baseline(snap)
    except Exception:
        pass

    return {
        "profile": prof["name"],
        "preview": not persist,
        "persist": persist,
        "authorize_live": authorize_live,
        "fetch_details": do_details,
        "fetch_documents": do_documents,
        "all_eligible_sources": all_eligible or source_cap is None,
        "pagination_exhaust": pagination_exhaust,
        "metrics": metrics,
        "completeness": completeness,
        "run_status": run_status,
        "partial_reason": partial_reason,
        "dla_fallback": dla_fallback_meta,
        "federal_sam": federal_sam_meta,
        "analytics": analytics,
        "quality_samples": samples if not persist else {"note": "available in preview"},
        "opportunities": collected if not persist else [],
        "handoff_records": list(collected),
        "opportunity_count_persisted": len(collected) if persist else 0,
        "accounting": client.accounting(),
        "sources_contacted": sources_contacted,
        "sources_selected": [
            {"source_id": c["source_id"], "url": c["list_url"], "name": c.get("name")} for c in candidates
        ],
        "source_budget_exhausted": metrics["source_budget_exhausted"],
        "global_budget_exhausted": metrics["global_budget_exhausted"],
        "runtime_budget_exhausted": metrics["runtime_budget_exhausted"],
        "SAM": metrics.get("SAM") or 0,
        "OpenAI": metrics.get("OpenAI") or 0,
        "USAspending": 0,
        "paid": metrics.get("paid") or 0,
        "LIVE_API_REQUESTS": client.request_count if authorize_live else 0,
    }


def handoff_documents_to_package(session: Any, contract_id: int, document_links: list[dict[str, Any]]) -> dict[str, Any]:
    """Store discovered URLs into existing solicitation document architecture — no duplicate system."""
    from models import SolicitationDocument

    added = 0
    for d in document_links or []:
        url = d.get("url")
        if not url:
            continue
        existing = (
            session.query(SolicitationDocument)
            .filter_by(contract_id=contract_id, url=url)
            .first()
        )
        if existing:
            continue
        session.add(
            SolicitationDocument(
                contract_id=contract_id,
                document_type=d.get("kind") or "attachment",
                source="discovery_handoff",
                url=url,
                filename=d.get("filename"),
                text_extraction_status="UNKNOWN",
                review_status="UNKNOWN",
                current=True,
            )
        )
        added += 1
    session.flush()
    return {"added": added, "LIVE_API_REQUESTS": 0}
