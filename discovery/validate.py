"""Validate discovery sources — controlled live checks; no opportunity persistence."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from discovery.http_client import (
    BudgetExhausted,
    GlobalBudgetExhausted,
    PublicProcurementHttpClient,
    RequestBudget,
    RuntimeBudgetExhausted,
    SourceBudgetExhausted,
)
from discovery.live_fetchers import get_live_fetcher
from discovery.sam_policy import assert_no_broad_sam_discovery
from discovery.selection import select_validation_candidates
from discovery.state_matrix import set_status_override
from discovery.verification import build_validation_evidence, persist_validation_result


def validate_sources(
    session: Any | None = None,
    *,
    authorize_live: bool = False,
    max_sources: int = 15,
    max_requests_per_source: int = 2,
    max_total_requests: int = 30,
    transport: Any | None = None,
    source_ids: list[str] | None = None,
    persist_registry: bool = True,
) -> dict[str, Any]:
    """
    Validate candidate sources. Does NOT persist opportunities.
    May persist registry validation status when session provided.
    """
    pol = assert_no_broad_sam_discovery()
    if not pol["broad_discovery_blocked"]:
        return {"error": "SAM broad discovery blocked required", "LIVE_API_REQUESTS": 0}

    candidates = select_validation_candidates(max_sources=max_sources)
    if source_ids:
        from discovery.selection import _pool_map

        pool = _pool_map()
        candidates = [pool[s] for s in source_ids if s in pool][:max_sources]

    budget = RequestBudget(
        max_total_requests=max_total_requests,
        max_requests_per_source=max_requests_per_source,
        max_pages_per_source=1,
        max_records_per_source=50,
        max_runtime_seconds=180,
        max_retries=1,
        min_interval_seconds=2.0,
        timeout_seconds=20.0,
    )
    client = PublicProcurementHttpClient(
        budget=budget,
        authorize_live=bool(authorize_live),
        transport=transport,
    )

    results: list[dict[str, Any]] = []
    for cand in candidates:
        if client.request_count >= max_total_requests:
            break
        fetcher = get_live_fetcher(cand["adapter_family"])
        if not fetcher:
            results.append(
                {
                    "source_id": cand["source_id"],
                    "adapter_status": "UNSUPPORTED",
                    "error": "no_fetcher",
                    "requests": 0,
                }
            )
            continue
        before = client.request_count
        try:
            result = fetcher.fetch_listing(
                client,
                list_url=cand["list_url"],
                source_id=cand["source_id"],
                max_pages=1,
            )
            opps = result.get("opportunities") or []
            validation = result.get("validation") or {}
            meta = result.get("request_meta") or {}
            evidence = build_validation_evidence(
                source_id=cand["source_id"],
                list_url=cand["list_url"],
                http_status=meta.get("http_status"),
                validation=validation,
                records_found=len(opps),
                opportunities=opps,
                requests=client.request_count - before,
                platform_family=cand.get("platform_family"),
            )
            evidence.update(
                {
                    "name": cand.get("name"),
                    "kind": cand.get("kind"),
                    "state_code": cand.get("state_code"),
                    "jurisdiction": cand.get("kind"),
                    "host": urlparse(cand["list_url"]).netloc,
                    "public_detail_access": any(getattr(o, "detail_url", None) for o in opps),
                    "public_document_access": any(getattr(o, "document_links", None) for o in opps),
                }
            )
            set_status_override(cand["source_id"], evidence["adapter_status"])
            if persist_registry and session is not None:
                persist_validation_result(session, evidence)
            results.append(evidence)
        except SourceBudgetExhausted as exc:
            results.append(
                {
                    "source_id": cand["source_id"],
                    "error": str(exc),
                    "budget_stop": True,
                    "source_budget_exhausted": True,
                    "source_stop_reason": "SOURCE_BUDGET_EXHAUSTED",
                    "requests": client.request_count - before,
                }
            )
            continue
        except GlobalBudgetExhausted as exc:
            results.append(
                {
                    "source_id": cand["source_id"],
                    "error": str(exc),
                    "budget_stop": True,
                    "global_budget_exhausted": True,
                    "source_stop_reason": "GLOBAL_BUDGET_EXHAUSTED",
                    "requests": client.request_count - before,
                }
            )
            break
        except RuntimeBudgetExhausted as exc:
            results.append(
                {
                    "source_id": cand["source_id"],
                    "error": str(exc),
                    "budget_stop": True,
                    "runtime_budget_exhausted": True,
                    "source_stop_reason": "RUNTIME_BUDGET_EXHAUSTED",
                    "requests": client.request_count - before,
                }
            )
            break
        except BudgetExhausted as exc:
            kind = getattr(exc, "kind", "global")
            results.append(
                {
                    "source_id": cand["source_id"],
                    "error": str(exc),
                    "budget_stop": True,
                    "source_stop_reason": f"{kind.upper()}_BUDGET_EXHAUSTED",
                    "requests": client.request_count - before,
                }
            )
            if kind == "source":
                continue
            break
        except Exception as exc:
            set_status_override(cand["source_id"], "BROKEN")
            results.append(
                {
                    "source_id": cand["source_id"],
                    "list_url": cand["list_url"],
                    "adapter_status": "BROKEN",
                    "error": str(exc),
                    "requests": client.request_count - before,
                }
            )

    verified = [r for r in results if r.get("adapter_status") == "LIVE_VERIFIED"]
    return {
        "results": results,
        "sources_attempted": len(results),
        "LIVE_VERIFIED": len(verified),
        "verified_source_ids": [r["source_id"] for r in verified],
        "accounting": client.accounting(),
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
        "opportunities_persisted": 0,
        "note": "Validation only — opportunities not persisted",
    }
