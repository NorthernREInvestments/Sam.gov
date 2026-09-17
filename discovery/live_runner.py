"""Live discovery runner — listing-first, per-source budget isolated."""

from __future__ import annotations

from typing import Any

from discovery.analytics import analyze_run_results, quality_sample
from discovery.classify import classify_discovery_opportunity, early_reject_reasons
from discovery.deadline import normalize_deadline
from discovery.deadline_viability import compute_deadline_runway, enrich_opportunity_deadline
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


def _candidate_sources(max_sources: int, *, require_verified: bool = False) -> list[dict[str, Any]]:
    from discovery.selection import select_diversified_sources

    return select_diversified_sources(max_sources=max_sources, require_verified=require_verified)


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
) -> dict[str, Any]:
    """
    Controlled live/preview discovery — listing-first by default for TINY/BROAD.
    Per-source budget exhaustion continues to next source.
    Optional on_source_complete(metrics, source_id) for coarse progress heartbeats.
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

    prof = get_profile(profile)
    budget: RequestBudget = prof["budget"]
    do_details = prof.get("fetch_details", False) if fetch_details is None else fetch_details
    do_documents = prof.get("fetch_documents", False) if fetch_documents is None else fetch_documents

    client = PublicProcurementHttpClient(
        budget=budget,
        authorize_live=bool(authorize_live),
        transport=transport,
    )

    source_cap = max_sources if max_sources is not None else prof["max_sources"]
    candidates = _candidate_sources(source_cap)
    if source_ids:
        # Preserve caller order for tests
        pool = {c["source_id"]: c for c in _candidate_sources(999)}
        # Also allow arbitrary ids from selection pool map
        from discovery.selection import _pool_map

        pool.update(_pool_map())
        candidates = [pool[s] for s in source_ids if s in pool][:source_cap]

    metrics: dict[str, Any] = {
        "sources_attempted": 0,
        "sources_successful": 0,
        "sources_failed": 0,
        "raw_records": 0,
        "listing_records": 0,
        "normalized_records": 0,
        "unique_records": 0,
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
        "detail_fetches": 0,  # legacy alias
        "documents_discovered": 0,  # legacy alias = links discovered
        "source_budget_exhausted": False,
        "global_budget_exhausted": False,
        "runtime_budget_exhausted": False,
    }
    collected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    sources_contacted: list[dict[str, Any]] = []

    for cand in candidates:
        if metrics["raw_records"] >= prof["max_records_total"]:
            break
        if metrics["global_budget_exhausted"] or metrics["runtime_budget_exhausted"]:
            break

        metrics["sources_attempted"] += 1
        sid = cand["source_id"]
        fetcher = get_live_fetcher(cand["adapter_family"])
        if not fetcher:
            metrics["sources_failed"] += 1
            metrics["per_source"][sid] = {
                "ok": False,
                "error": "no_fetcher",
                "source_stop_reason": "NO_FETCHER",
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
            )
            metrics["listing_requests"] += 1
            sources_contacted.append(
                {"source_id": sid, "url": cand["list_url"], "name": cand.get("name")}
            )

            validation = result.get("validation") or {}
            if not validation.get("valid"):
                metrics["sources_failed"] += 1
                metrics["parser_warnings"].extend(validation.get("warnings") or [])
                fail_type = (validation.get("failure_type") or "VALIDATION_FAILURE").upper()
                metrics["per_source"][sid] = {
                    "ok": False,
                    "validation": validation,
                    "requests": client.request_count - before,
                    "listing_requests": 1,
                    "source_stop_reason": fail_type,
                    "source_budget_exhausted": False,
                }
                continue

            opps = result.get("opportunities") or []
            metrics["listing_records"] += len(opps)
            metrics["sources_successful"] += 1
            src_raw = 0
            src_unique = 0
            src_product = 0

            for opp in opps:
                if src_raw >= budget.max_records_per_source:
                    source_stop_reason = "SOURCE_RECORD_CAP"
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

                # Document links discovered from listing (no body fetch)
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
                src_raw += 1
                metrics[cls["classification"]] = metrics.get(cls["classification"], 0) + 1

                if dl.get("deadline_passed"):
                    metrics["expired"] += 1

                if reject["reject"] or cls["classification"] == "SERVICE":
                    continue

                gate = should_fetch_detail(
                    opp,
                    classification=cls["classification"],
                    fetch_details_enabled=bool(do_details),
                )
                if gate.get("earned"):
                    metrics["detail_eligible"] += 1

                # Detail fetch only when profile enables AND gate says fetch
                if gate.get("fetch") and opp.detail_url and (authorize_live or transport is not None):
                    if not do_documents:
                        # Detail HTML only — still no document body download policy for preview
                        pass
                    try:
                        detail = fetcher.fetch_detail(client, detail_url=opp.detail_url, source_id=sid)
                        metrics["detail_requests"] += 1
                        metrics["details_fetched"] += 1
                        metrics["detail_fetches"] += 1
                        # Discover more doc links without counting as document_requests
                        for d in detail.get("documents") or []:
                            if d.get("url"):
                                existing = {x.get("url") for x in (opp.document_links or [])}
                                if d["url"] not in existing:
                                    d["discovered_only"] = True
                                    d["document_fetched"] = False
                                    opp.document_links.append(d)
                                    metrics["document_links_discovered"] += 1
                                    metrics["documents_discovered"] += 1
                        # Explicit: never auto-fetch document bodies here
                        if do_documents:
                            metrics["document_requests"] += 0  # reserved — bodies not fetched in this stage
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

            metrics["per_source"][sid] = {
                "ok": True,
                "raw": src_raw,
                "unique": src_unique,
                "product_candidates": src_product,
                "validation": validation.get("health_status"),
                "url": cand["list_url"],
                "requests": client.request_count - before,
                "listing_requests": 1,
                "detail_requests": 0,
                "source_stop_reason": source_stop_reason or "COMPLETED",
                "source_budget_exhausted": source_stop_reason == "SOURCE_BUDGET_EXHAUSTED",
            }

        except SourceBudgetExhausted as exc:
            metrics["source_budget_exhausted"] = True
            metrics["per_source"][sid] = {
                "ok": False,
                "error": str(exc),
                "source_stop_reason": "SOURCE_BUDGET_EXHAUSTED",
                "source_budget_exhausted": True,
                "requests": client.request_count - before,
                "isolated": True,
            }
            # CONTINUE to next source
            continue
        except GlobalBudgetExhausted as exc:
            metrics["global_budget_exhausted"] = True
            metrics["per_source"][sid] = {
                "ok": False,
                "error": str(exc),
                "source_stop_reason": "GLOBAL_BUDGET_EXHAUSTED",
                "requests": client.request_count - before,
            }
            break
        except RuntimeBudgetExhausted as exc:
            metrics["runtime_budget_exhausted"] = True
            metrics["per_source"][sid] = {
                "ok": False,
                "error": str(exc),
                "source_stop_reason": "RUNTIME_BUDGET_EXHAUSTED",
                "requests": client.request_count - before,
            }
            break
        except BudgetExhausted as exc:
            # Backward-compatible: treat unknown BudgetExhausted by message
            msg = str(exc).lower()
            if "per-source" in msg:
                metrics["source_budget_exhausted"] = True
                metrics["per_source"][sid] = {
                    "ok": False,
                    "error": str(exc),
                    "source_stop_reason": "SOURCE_BUDGET_EXHAUSTED",
                    "source_budget_exhausted": True,
                    "isolated": True,
                }
                continue
            if "runtime" in msg or "max runtime" in msg:
                metrics["runtime_budget_exhausted"] = True
                metrics["per_source"][sid] = {
                    "ok": False,
                    "error": str(exc),
                    "source_stop_reason": "RUNTIME_BUDGET_EXHAUSTED",
                }
                break
            metrics["global_budget_exhausted"] = True
            metrics["per_source"][sid] = {
                "ok": False,
                "error": str(exc),
                "source_stop_reason": "GLOBAL_BUDGET_EXHAUSTED",
            }
            break
        except Exception as exc:
            metrics["sources_failed"] += 1
            metrics["per_source"][sid] = {
                "ok": False,
                "error": str(exc),
                "source_stop_reason": "SOURCE_EXCEPTION",
                "isolated": True,
            }
            continue

        if callable(on_source_complete):
            try:
                on_source_complete(dict(metrics), sid)
            except Exception:
                pass

        if metrics["global_budget_exhausted"] or metrics["runtime_budget_exhausted"]:
            break

    unique = metrics["unique_records"] or 0
    product_cands = metrics.get("CORE_PRODUCT", 0) + metrics.get("PRODUCT_PLUS_SERVICE", 0)
    req = client.request_count or 0
    metrics["requests_per_unique_opportunity"] = round(req / unique, 3) if unique else None
    metrics["requests_per_product_candidate"] = round(req / product_cands, 3) if product_cands else None

    analytics = analyze_run_results(collected)
    samples = quality_sample(collected)

    return {
        "profile": prof["name"],
        "preview": not persist,
        "persist": persist,
        "authorize_live": authorize_live,
        "fetch_details": do_details,
        "fetch_documents": do_documents,
        "metrics": metrics,
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
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
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
