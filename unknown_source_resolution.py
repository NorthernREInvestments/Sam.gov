"""Round 2 unknown-source resolution — UNKNOWN → INVESTIGATE → PRODUCTIVE OR EXPLAINED."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from application_clock import now_utc
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.live_fetchers import get_fetcher_for_platform, get_live_fetcher
from discovery.platform_detect import detect_platform
from discovery.validation import validate_listing_response
from family_adapter_contract import (
    classify_document_access,
    evaluate_zero_result,
    fingerprint_platform,
)
from national_discovery_constants import SRC_AUTH, SRC_DEGRADED, SRC_HEALTHY, SRC_UNKNOWN
from procurement_source_registry import ProcurementSourceRegistry

# Resolution outcomes
RES_HEALTHY = "HEALTHY_PRODUCTION"
RES_PARTIAL = "PARTIALLY_PRODUCTIVE"
RES_META = "PUBLIC_METADATA_ONLY"
RES_AUTH = "AUTH_GATED"
RES_REG = "REGISTRATION_REQUIRED"
RES_BOT = "BOT_PROTECTED"
RES_DEGRADED = "DEGRADED"
RES_CHANGED = "SOURCE_CHANGED"
RES_TEMP = "TEMPORARILY_UNAVAILABLE"
RES_UNSUPPORTED = "UNSUPPORTED"
RES_BROKEN = "BROKEN"
RES_UNKNOWN_REASON = "UNKNOWN_WITH_EXPLICIT_REASON"


def _utc() -> str:
    return now_utc().isoformat()


def classify_probe_result(
    *,
    source: dict[str, Any],
    status_code: int | None,
    body: str | None,
    records_found: int,
    structure_recognized: bool,
    validation: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Map probe evidence to a specific resolution — never invent BROKEN from untested."""
    text = (body or "").lower()
    val = validation or {}
    health = str(val.get("health") or "").upper()

    if error:
        err_l = error.lower()
        if "getaddrinfo" in err_l or "name or service not known" in err_l or "nodename" in err_l:
            return {"resolution": RES_CHANGED, "reason": "dns_or_host_unresolved", "evidence": error[:200]}
        if "timed out" in err_l or "timeout" in err_l:
            return {"resolution": RES_TEMP, "reason": "timeout", "evidence": error[:200]}
        if "connection" in err_l:
            return {"resolution": RES_TEMP, "reason": "connection_error", "evidence": error[:200]}
        return {"resolution": RES_UNKNOWN_REASON, "reason": "probe_exception", "evidence": error[:200]}

    if status_code in {401, 403} or health in {"AUTH_REQUIRED"}:
        if "register" in text and ("login" in text or "sign in" in text):
            return {"resolution": RES_REG, "reason": "registration_or_login_required", "evidence": f"http_{status_code}"}
        return {"resolution": RES_AUTH, "reason": "auth_required_to_view_listings", "evidence": f"http_{status_code}"}

    if status_code in {404, 410}:
        return {"resolution": RES_CHANGED, "reason": "url_not_found", "evidence": f"http_{status_code}"}

    if status_code and status_code >= 500:
        return {"resolution": RES_TEMP, "reason": "server_error", "evidence": f"http_{status_code}"}

    if health in {"BLOCKED"} or ("captcha" in text and len(text) < 8000):
        return {"resolution": RES_BOT, "reason": "bot_protection_or_captcha", "evidence": health or "captcha_signal"}

    if "just a moment" in text and ("cloudflare" in text or "challenge-platform" in text or "cf-ray" in text):
        return {"resolution": RES_BOT, "reason": "cloudflare_challenge", "evidence": f"http_{status_code}"}

    if records_found > 0 and structure_recognized:
        # Enough for identity?
        return {
            "resolution": RES_HEALTHY,
            "reason": "public_listing_parsed_with_records",
            "evidence": f"records={records_found}",
            "records_found": records_found,
        }

    if records_found > 0 and not structure_recognized:
        return {
            "resolution": RES_PARTIAL,
            "reason": "records_parsed_but_structure_weak",
            "evidence": f"records={records_found}",
            "records_found": records_found,
        }

    # Zero records
    zr = evaluate_zero_result(
        status_code=status_code,
        body=body,
        records_found=0,
        structure_recognized=structure_recognized,
        prior_avg_records=source.get("records_discovered"),
    )
    if zr["state"] == "VALID_ZERO_RESULTS":
        return {
            "resolution": RES_HEALTHY,
            "reason": "valid_empty_listing",
            "evidence": zr.get("reason"),
            "records_found": 0,
            "zero_result_state": zr["state"],
        }

    # Metadata-only signals: titles/links but weak identity
    if structure_recognized and ("bid" in text or "solicitation" in text or "rfp" in text):
        # Check for login-for-docs / public list chrome
        if "login" in text and ("document" in text or "download" in text):
            return {
                "resolution": RES_META,
                "reason": "public_listing_markers_docs_may_require_login",
                "evidence": "structure_recognized_zero_parsed",
            }
        return {
            "resolution": RES_PARTIAL,
            "reason": "structure_recognized_but_zero_parsed",
            "evidence": zr.get("reason"),
            "zero_result_state": zr["state"],
        }

    if "must log in" in text or "login required to view" in text:
        return {"resolution": RES_AUTH, "reason": "login_required_copy", "evidence": "page_copy"}

    if "free registration" in text and "best deal" in text:
        return {"resolution": RES_REG, "reason": "marketing_registration_landing", "evidence": "publicpurchase_style"}

    if not structure_recognized and status_code == 200:
        return {
            "resolution": RES_UNSUPPORTED if not source.get("adapter_family") else RES_DEGRADED,
            "reason": "public_page_but_no_solicitation_structure",
            "evidence": "structure_not_recognized",
            "zero_result_state": zr["state"],
        }

    return {
        "resolution": RES_UNKNOWN_REASON,
        "reason": "insufficient_probe_evidence",
        "evidence": f"http={status_code}; records=0; structure={structure_recognized}",
    }


def apply_resolution_to_registry(
    registry: ProcurementSourceRegistry,
    *,
    source_id: str,
    resolution: dict[str, Any],
) -> dict[str, Any]:
    s = registry.get(source_id)
    if not s:
        return {"error": "missing"}
    res = resolution.get("resolution")
    patch = dict(s)
    patch["resolution_state"] = res
    patch["resolution_reason"] = resolution.get("reason")
    patch["resolution_evidence"] = resolution.get("evidence")
    patch["resolved_at"] = _utc()
    patch["last_attempted_checkpoint"] = _utc()
    n = int(resolution.get("records_found") or 0)

    health_map = {
        RES_HEALTHY: SRC_HEALTHY,
        RES_PARTIAL: "PARTIALLY_PRODUCTIVE",
        RES_META: "PUBLIC_METADATA_ONLY",
        RES_AUTH: SRC_AUTH,
        RES_REG: "REGISTRATION_REQUIRED",
        RES_BOT: "BOT_PROTECTED",
        RES_DEGRADED: SRC_DEGRADED,
        RES_CHANGED: "SOURCE_CHANGED",
        RES_TEMP: "TEMPORARILY_UNAVAILABLE",
        RES_UNSUPPORTED: "UNSUPPORTED",
        RES_BROKEN: "BROKEN",
        RES_UNKNOWN_REASON: SRC_UNKNOWN,
    }
    patch["health_state"] = health_map.get(res, SRC_UNKNOWN)
    if res == RES_HEALTHY and n > 0:
        patch["last_success_at"] = _utc()
        patch["last_successful_checkpoint"] = _utc()
        patch["records_discovered"] = n
        patch["consecutive_successes"] = int(s.get("consecutive_successes") or 0) + 1
        patch["consecutive_failures"] = 0
        patch["failure_class"] = None
        patch["lifecycle"] = "HEALTHY_PRODUCTION"
    elif res in {RES_PARTIAL, RES_META}:
        patch["lifecycle"] = "PARTIALLY_PRODUCTIVE"
        if n > 0:
            patch["records_discovered"] = n
            patch["last_success_at"] = _utc()
    elif res in {RES_AUTH, RES_REG}:
        patch["auth_requirement"] = "LOGIN_REQUIRED" if res == RES_AUTH else "ACCOUNT_REQUIRED"
        patch["lifecycle"] = "DEGRADED"
        patch["failure_class"] = res
    elif res in {RES_BOT, RES_CHANGED, RES_TEMP, RES_DEGRADED, RES_BROKEN, RES_UNSUPPORTED}:
        patch["lifecycle"] = "DEGRADED" if res != RES_BROKEN else "BROKEN"
        patch["failure_class"] = res
        patch["last_failure_at"] = _utc()
        patch["consecutive_failures"] = int(s.get("consecutive_failures") or 0) + 1
    else:
        patch["lifecycle"] = "VALIDATING"
        patch["failure_class"] = resolution.get("reason")

    out = registry.upsert(patch)
    return {"ok": True, "source": out, "resolution": res}


def probe_source(
    source: dict[str, Any],
    *,
    client: PublicProcurementHttpClient | None = None,
    authorize_live: bool = True,
) -> dict[str, Any]:
    """Single conservative public GET + parse attempt."""
    url = source.get("discovery_url") or source.get("canonical_base_url")
    sid = source["source_id"]
    fam = source.get("platform_family") or source.get("source_family")
    fp = fingerprint_platform(url=url, registry_family=str(fam) if fam else None)
    adapter_id = source.get("adapter_family")
    fetcher = get_live_fetcher(adapter_id) if adapter_id else get_fetcher_for_platform(str(fam or ""))

    base = {
        "source_id": sid,
        "entity": source.get("source_name"),
        "jurisdiction": source.get("jurisdiction"),
        "entity_type": source.get("entity_type"),
        "url": url,
        "platform_family": fam,
        "fingerprint": fp,
        "adapter_id": adapter_id or (fetcher.source_id if fetcher else None),
    }
    if not url:
        resolution = {"resolution": RES_UNSUPPORTED, "reason": "no_discovery_url", "evidence": None}
        return {**base, **resolution, "probe": False}

    own_client = client is None
    client = client or PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=3, max_requests_per_source=2),
        authorize_live=authorize_live,
    )
    try:
        # Always do one public GET for classification evidence (budget-aware).
        resp = client.get(url, source_id=sid)
        body = resp.text or ""
        live_reqs = 0 if not authorize_live else 1
        if not fetcher:
            det = detect_platform(url, html_snippet=body[:4000], content_type=resp.meta.content_type)
            resolution = classify_probe_result(
                source=source,
                status_code=resp.status_code,
                body=body,
                records_found=0,
                structure_recognized=False,
            )
            if det.get("platform") and det.get("platform") != "UNKNOWN":
                resolution["detected_platform"] = det
            return {
                **base,
                **resolution,
                "status_code": resp.status_code,
                "records_found": 0,
                "LIVE_API_REQUESTS": live_reqs,
            }

        # Prefer parse of already-fetched body to avoid double-hit when possible
        opps = fetcher.parse_listing(body, list_url=url, meta=resp.meta.to_dict())
        structure = fetcher.structure_recognized(body, list_url=url)
        from discovery.opportunity_gate import is_structurally_valid_opportunity

        valid_opps = []
        for o in opps:
            gate = is_structurally_valid_opportunity(
                {
                    "title": o.title,
                    "solicitation_number": o.solicitation_number,
                    "external_id": o.external_id,
                    "deadline_raw": o.deadline_raw,
                    "detail_url": o.detail_url,
                    "agency": o.agency,
                    "status": o.status,
                }
            )
            if gate.get("valid"):
                valid_opps.append(o)
        validation = validate_listing_response(
            status_code=resp.status_code,
            content_type=resp.meta.content_type,
            body=body,
            records_found=len(valid_opps),
            expected_kind=getattr(fetcher, "expected_kind", "html"),
            structure_recognized=structure or len(valid_opps) > 0,
        )
        resolution = classify_probe_result(
            source=source,
            status_code=resp.status_code,
            body=body,
            records_found=len(valid_opps),
            structure_recognized=structure or len(valid_opps) > 0,
            validation=validation,
        )
        # PUBLIC_METADATA_ONLY when BidNet-style public meta + gated docs
        if (
            resolution.get("resolution") == RES_HEALTHY
            and valid_opps
            and any((o.raw_metadata or {}).get("public_metadata_only") for o in valid_opps)
        ):
            # Still productive discovery — keep HEALTHY but annotate; optional META if weak identity
            weak = sum(1 for o in valid_opps if not (o.solicitation_number or o.external_id))
            if weak == len(valid_opps):
                resolution["resolution"] = RES_META
                resolution["reason"] = "public_titles_without_stable_ids"
            else:
                resolution["document_access_note"] = "AUTH_GATED_PACKAGES"
        doc_state = classify_document_access(
            doc_urls=[d.get("url") for o in valid_opps for d in (o.document_links or []) if isinstance(d, dict)],
            detail_requires_login=resolution.get("resolution") in {RES_AUTH, RES_REG},
        )
        return {
            **base,
            **resolution,
            "status_code": resp.status_code,
            "records_found": len(valid_opps),
            "sample_titles": [o.title for o in valid_opps[:5]],
            "validation_health": validation.get("health"),
            "document_access": doc_state,
            "pages_fetched": 1,
            "LIVE_API_REQUESTS": live_reqs,
            "opportunities": valid_opps,
        }
    except Exception as exc:  # noqa: BLE001
        resolution = classify_probe_result(
            source=source,
            status_code=None,
            body=None,
            records_found=0,
            structure_recognized=False,
            error=str(exc),
        )
        return {**base, **resolution, "LIVE_API_REQUESTS": 0, "error": str(exc)[:300]}
    finally:
        if own_client:
            pass


def resolve_unknown_sources(
    registry: ProcurementSourceRegistry | None = None,
    *,
    authorize_live: bool = True,
    limit: int | None = None,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Investigate UNKNOWN (and DISCOVERED_UNVALIDATED) sources; persist specific classifications."""
    reg = registry or ProcurementSourceRegistry()
    targets = []
    for s in reg.all_sources():
        if source_ids and s["source_id"] not in source_ids:
            continue
        if s.get("health_state") in {SRC_UNKNOWN, "DISCOVERED_UNVALIDATED", None}:
            targets.append(s)
        if limit and len(targets) >= limit:
            break

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=max(30, len(targets) * 2 + 10),
            max_requests_per_source=2,
            min_interval_seconds=1.5,
        ),
        authorize_live=authorize_live,
    )
    results = []
    by_res: Counter[str] = Counter()
    requests = 0
    for s in targets:
        probe = probe_source(s, client=client, authorize_live=authorize_live)
        applied = apply_resolution_to_registry(reg, source_id=s["source_id"], resolution=probe)
        requests += int(probe.get("LIVE_API_REQUESTS") or 0)
        row = dict(probe)
        row["applied_health"] = (applied.get("source") or {}).get("health_state")
        results.append(row)
        by_res[probe.get("resolution") or "UNKNOWN"] += 1

    reg.save()
    return {
        "kind": "UnknownSourceResolution",
        "resolved_at": _utc(),
        "attempted": len(results),
        "by_resolution": dict(by_res),
        "LIVE_API_REQUESTS": requests,
        "results": results,
        "accounts_created": 0,
        "auth_bypassed": False,
    }


def round2_leverage_ranking(registry: ProcurementSourceRegistry) -> dict[str, Any]:
    """Rank remaining work after UNKNOWN resolution."""
    by_fam: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"registered": 0, "healthy": 0, "partial": 0, "unknown": 0, "blocked": 0, "source_ids": []}
    )
    for s in registry.all_sources():
        fam = s.get("platform_family") or "UNKNOWN"
        slot = by_fam[fam]
        slot["registered"] += 1
        slot["source_ids"].append(s["source_id"])
        h = s.get("health_state")
        if h == SRC_HEALTHY:
            slot["healthy"] += 1
        elif h in {"PARTIALLY_PRODUCTIVE", "PUBLIC_METADATA_ONLY"}:
            slot["partial"] += 1
        elif h in {SRC_AUTH, "REGISTRATION_REQUIRED", "BOT_PROTECTED"}:
            slot["blocked"] += 1
        elif h in {SRC_UNKNOWN, "DISCOVERED_UNVALIDATED"}:
            slot["unknown"] += 1
    ranked = []
    access = {
        "OpenGov": 0.85,
        "BidNet": 0.55,
        "PlanetBids": 0.75,
        "PublicPurchase": 0.3,
        "Bonfire": 0.8,
        "Jaggaer": 0.85,
        "SimpleHTML": 0.7,
        "StateOwned": 0.55,
        "JSON": 0.9,
    }
    for fam, slot in by_fam.items():
        unlockable = slot["registered"] - slot["healthy"] - slot["blocked"]
        score = unlockable * access.get(fam, 0.4) + (5 if slot["healthy"] == 0 and slot["registered"] >= 2 else 0)
        ranked.append(
            {
                "platform_family": fam,
                **{k: slot[k] for k in ("registered", "healthy", "partial", "unknown", "blocked")},
                "unlockable_estimate": max(0, unlockable),
                "zero_healthy_priority_boost": slot["healthy"] == 0 and slot["registered"] >= 2,
                "leverage_score": round(score, 2),
            }
        )
    ranked.sort(key=lambda r: (-r["leverage_score"], -r["registered"]))
    return {"kind": "PlatformRound2LeverageRanking", "ranking": ranked, "priority_order": [r["platform_family"] for r in ranked]}
