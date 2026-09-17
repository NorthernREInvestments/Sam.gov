"""Source network audit, leverage ranking, and lifecycle — extends existing registry."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from application_clock import now_utc
from discovery.live_fetchers import PLATFORM_TO_FETCHER, get_fetcher_for_platform
from discovery.platform_detect import detect_platform
from national_discovery_constants import SRC_AUTH, SRC_DEGRADED, SRC_HEALTHY, SRC_QUARANTINED, SRC_UNKNOWN
from procurement_source_registry import ProcurementSourceRegistry

# Audit classifications (UNKNOWN stays UNKNOWN — never tested ≠ broken)
AUDIT_HEALTHY = "HEALTHY_PRODUCTION"
AUDIT_PARTIAL = "PARTIALLY_PRODUCTIVE"
AUDIT_MISSING = "ADAPTER_MISSING"
AUDIT_BROKEN = "ADAPTER_BROKEN"
AUDIT_AUTH = "AUTH_GATED"
AUDIT_BOT = "BOT_PROTECTED"
AUDIT_META_ONLY = "PUBLIC_METADATA_ONLY"
AUDIT_CHANGED = "SOURCE_CHANGED"
AUDIT_TEMP = "TEMPORARILY_UNAVAILABLE"
AUDIT_UNSUPPORTED = "UNSUPPORTED"
AUDIT_UNKNOWN = "UNKNOWN"

LIFECYCLE = (
    "DISCOVERED",
    "FINGERPRINTED",
    "ADAPTER_CANDIDATE",
    "VALIDATING",
    "PARTIALLY_PRODUCTIVE",
    "HEALTHY_PRODUCTION",
    "DEGRADED",
    "BROKEN",
    "RETIRED",
)

# Leverage weights — unlock many sources per reusable adapter
_FAMILY_ACCESS = {
    "Bonfire": 0.85,
    "OpenGov": 0.8,
    "Jaggaer": 0.9,
    "SimpleHTML": 0.75,
    "StateOwned": 0.55,  # more variance
    "JSON": 0.95,
    "BidNet": 0.45,  # often partial/public-metadata
    "PlanetBids": 0.7,
    "PublicPurchase": 0.25,  # often auth/marketing
    "OTHER": 0.2,
    "UNKNOWN": 0.1,
}


def _utc() -> str:
    return now_utc().isoformat()


def classify_source_audit_row(source: dict[str, Any]) -> str:
    """Map registry health + adapter presence to audit class. Untested → UNKNOWN."""
    health = str(source.get("health_state") or SRC_UNKNOWN)
    auth = str(source.get("auth_requirement") or "NONE").upper()
    failure = str(source.get("failure_class") or "").upper()
    fam = source.get("platform_family") or source.get("source_family")
    has_adapter = bool(source.get("adapter_family") or (fam and get_fetcher_for_platform(str(fam))))

    if health == SRC_HEALTHY:
        return AUDIT_HEALTHY
    if health in {"PARTIALLY_PRODUCTIVE", "PUBLIC_METADATA_ONLY"}:
        return AUDIT_PARTIAL if health == "PARTIALLY_PRODUCTIVE" else AUDIT_META_ONLY
    if health == SRC_DEGRADED or failure == "SCHEMA_CHANGE":
        return AUDIT_CHANGED if failure in {"SCHEMA_CHANGE", "SOURCE_CHANGED"} else "DEGRADED"
    if health in {"SOURCE_CHANGED"}:
        return AUDIT_CHANGED
    if health in {"TEMPORARILY_UNAVAILABLE"}:
        return AUDIT_TEMP
    if health in {"REGISTRATION_REQUIRED"} or auth in {"ACCOUNT_REQUIRED"} and health != SRC_HEALTHY:
        if health == "REGISTRATION_REQUIRED" or "REGISTRATION" in failure:
            return AUDIT_AUTH  # gated family
    if health == "BOT_PROTECTED" or failure in {"ANTI_AUTOMATION", "BOT_PROTECTED", "CAPTCHA", "BOT_PROTECTION"}:
        return AUDIT_BOT
    if health == SRC_AUTH or auth in {"LOGIN_REQUIRED", "ACCOUNT_REQUIRED"} or failure == "AUTH_REQUIRED":
        return AUDIT_AUTH
    if failure in {"ANTI_AUTOMATION", "BOT_PROTECTED", "CAPTCHA"}:
        return AUDIT_BOT
    if failure == "PARSER_FAILURE" or health == "BROKEN":
        return AUDIT_BROKEN
    if health in {SRC_QUARANTINED, "UNSUPPORTED"}:
        return AUDIT_UNSUPPORTED
    if not has_adapter and fam not in {None, "OTHER", "UNKNOWN"}:
        # Family known but no mapped fetcher
        if fam and not get_fetcher_for_platform(str(fam)):
            return AUDIT_MISSING
    if health in {SRC_UNKNOWN, "DISCOVERED_UNVALIDATED", "UNKNOWN_WITH_EXPLICIT_REASON"}:
        return AUDIT_UNKNOWN
    return AUDIT_UNKNOWN


def audit_source_network(registry: ProcurementSourceRegistry | None = None) -> dict[str, Any]:
    """ONE targeted audit of all registered sources."""
    reg = registry or ProcurementSourceRegistry()
    rows = []
    for s in reg.all_sources():
        fam = s.get("platform_family") or s.get("source_family") or "UNKNOWN"
        adapter = s.get("adapter_family") or PLATFORM_TO_FETCHER.get(str(fam))
        fp = detect_platform(s.get("discovery_url") or s.get("canonical_base_url"))
        audit_class = classify_source_audit_row(s)
        rows.append(
            {
                "source_id": s["source_id"],
                "entity": s.get("source_name"),
                "jurisdiction": s.get("jurisdiction") or s.get("geographic_scope"),
                "entity_type": s.get("entity_type"),
                "platform_family": fam,
                "current_adapter": adapter,
                "current_health": s.get("health_state"),
                "audit_class": audit_class,
                "public_listing_url": s.get("discovery_url"),
                "authentication_requirement": s.get("auth_requirement") or "NONE",
                "pagination_model": s.get("pagination_method") or "UNKNOWN",
                "checkpoint_capability": bool(s.get("incremental_method")),
                "failure_reason": s.get("failure_class"),
                "last_successful_discovery": s.get("last_success_at") or s.get("last_successful_checkpoint"),
                "last_attempted_discovery": s.get("last_attempted_checkpoint") or s.get("last_failure_at"),
                "records_discovered": s.get("records_discovered"),
                "fingerprint": {
                    "platform": fp.get("platform"),
                    "confidence": fp.get("confidence"),
                    "detection": fp.get("detection"),
                },
                "lifecycle": s.get("lifecycle") or (
                    "HEALTHY_PRODUCTION"
                    if s.get("health_state") == SRC_HEALTHY
                    else "DISCOVERED"
                    if s.get("health_state") == "DISCOVERED_UNVALIDATED"
                    else "ADAPTER_CANDIDATE"
                    if adapter
                    else "DISCOVERED"
                ),
            }
        )

    by_class = Counter(r["audit_class"] for r in rows)
    by_family = Counter(r["platform_family"] for r in rows)
    return {
        "kind": "SourceNetworkAudit",
        "audited_at": _utc(),
        "registered_sources": len(rows),
        "by_audit_class": dict(by_class),
        "by_platform_family": dict(by_family),
        "sources": rows,
        "note": "Untested sources remain UNKNOWN — not classified broken",
        "claim_100_percent_national_coverage": False,
    }


def platform_family_inventory(audit: dict[str, Any] | None = None) -> dict[str, Any]:
    audit = audit or audit_source_network()
    families: dict[str, dict[str, Any]] = {}
    for row in audit["sources"]:
        fam = row["platform_family"] or "UNKNOWN"
        slot = families.setdefault(
            fam,
            {
                "platform_family": fam,
                "registered_count": 0,
                "healthy": 0,
                "partial": 0,
                "unknown": 0,
                "auth_gated": 0,
                "broken": 0,
                "adapter_id": PLATFORM_TO_FETCHER.get(fam),
                "adapter_present": bool(PLATFORM_TO_FETCHER.get(fam)),
                "source_ids": [],
            },
        )
        slot["registered_count"] += 1
        slot["source_ids"].append(row["source_id"])
        ac = row["audit_class"]
        if ac == AUDIT_HEALTHY:
            slot["healthy"] += 1
        elif ac == AUDIT_PARTIAL:
            slot["partial"] += 1
        elif ac == AUDIT_AUTH:
            slot["auth_gated"] += 1
        elif ac == AUDIT_BROKEN:
            slot["broken"] += 1
        elif ac == AUDIT_UNKNOWN:
            slot["unknown"] += 1
    return {
        "kind": "PlatformFamilyInventory",
        "families": sorted(families.values(), key=lambda f: -f["registered_count"]),
        "fabricated_platform_assignments": False,
    }


def platform_leverage_ranking(inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rank adapter work by coverage unlock per unit effort."""
    inv = inventory or platform_family_inventory()
    ranked = []
    for fam in inv["families"]:
        name = fam["platform_family"]
        access = _FAMILY_ACCESS.get(name, 0.3)
        unlockable = fam["registered_count"] - fam["healthy"] - fam["auth_gated"]
        score = fam["registered_count"] * access * (1.0 if fam["adapter_present"] else 0.4)
        ranked.append(
            {
                "platform_family": name,
                "registered_sources": fam["registered_count"],
                "currently_healthy": fam["healthy"],
                "unlockable_estimate": max(0, unlockable),
                "adapter_present": fam["adapter_present"],
                "public_access_score": access,
                "leverage_score": round(score, 2),
                "rationale": (
                    f"{fam['registered_count']} registered; "
                    f"adapter={'yes' if fam['adapter_present'] else 'no'}; "
                    f"access≈{access}"
                ),
            }
        )
    ranked.sort(key=lambda r: (-r["leverage_score"], -r["registered_sources"]))
    return {
        "kind": "PlatformLeverageRanking",
        "ranking": ranked,
        "priority_order": [r["platform_family"] for r in ranked],
        "note": "Do not prioritize merely because a platform is famous",
    }


def sync_adapter_families(registry: ProcurementSourceRegistry) -> dict[str, Any]:
    """Wire adapter_family from platform_family via PLATFORM_TO_FETCHER — no forks."""
    updated = 0
    for s in registry.all_sources():
        fam = s.get("platform_family") or s.get("source_family")
        mapped = PLATFORM_TO_FETCHER.get(str(fam)) if fam else None
        if mapped and s.get("adapter_family") != mapped:
            registry.upsert({**s, "adapter_family": mapped})
            updated += 1
        elif mapped and not s.get("adapter_family"):
            registry.upsert({**s, "adapter_family": mapped})
            updated += 1
    registry.save()
    return {"updated": updated, "registered": len(registry.all_sources())}


def promote_source_lifecycle(
    registry: ProcurementSourceRegistry,
    *,
    source_id: str,
    lifecycle: str,
    health_state: str | None = None,
    records_discovered: int | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    """Promotion requires successful discovery for HEALTHY_PRODUCTION."""
    if lifecycle not in LIFECYCLE:
        raise ValueError(f"invalid_lifecycle:{lifecycle}")
    s = registry.get(source_id)
    if not s:
        return {"error": "source_not_found", "source_id": source_id}
    if lifecycle == "HEALTHY_PRODUCTION":
        if records_discovered is None or records_discovered <= 0:
            return {
                "error": "healthy_requires_real_discovery",
                "source_id": source_id,
                "note": "Registration alone is not productivity",
            }
        health_state = health_state or SRC_HEALTHY
    patch = {
        **s,
        "lifecycle": lifecycle,
        "lifecycle_updated_at": _utc(),
        "lifecycle_evidence": evidence,
    }
    if health_state:
        patch["health_state"] = health_state
    if records_discovered is not None:
        patch["records_discovered"] = records_discovered
        if records_discovered > 0:
            patch["last_success_at"] = _utc()
    out = registry.upsert(patch)
    return {"ok": True, "source": out}


def apply_discovery_result_to_health(
    registry: ProcurementSourceRegistry,
    *,
    source_id: str,
    records_found: int,
    validation_health: str | None,
    failure_class: str | None = None,
    pages_fetched: int = 1,
) -> dict[str, Any]:
    """Update health from a real discovery attempt — honest classification."""
    s = registry.get(source_id)
    if not s:
        return {"error": "missing"}
    patch = dict(s)
    patch["last_attempted_checkpoint"] = _utc()
    patch["records_discovered"] = records_found
    patch["last_pages_fetched"] = pages_fetched
    if failure_class:
        patch["failure_class"] = failure_class
        patch["last_failure_at"] = _utc()
        consec = int(s.get("consecutive_failures") or 0) + 1
        patch["consecutive_failures"] = consec
        patch["consecutive_successes"] = 0
        if consec >= 3:
            patch["health_state"] = "BROKEN"
            patch["lifecycle"] = "BROKEN"
        elif validation_health in {"AUTH_REQUIRED", SRC_AUTH}:
            patch["health_state"] = SRC_AUTH
            patch["lifecycle"] = "DEGRADED"
        else:
            patch["health_state"] = SRC_DEGRADED
            patch["lifecycle"] = "DEGRADED"
    elif records_found > 0:
        patch["failure_class"] = None
        patch["consecutive_failures"] = 0
        patch["consecutive_successes"] = int(s.get("consecutive_successes") or 0) + 1
        patch["last_success_at"] = _utc()
        patch["last_successful_checkpoint"] = _utc()
        patch["health_state"] = SRC_HEALTHY
        patch["lifecycle"] = "HEALTHY_PRODUCTION"
    elif validation_health in {"AUTH_REQUIRED"}:
        patch["health_state"] = SRC_AUTH
        patch["lifecycle"] = "DEGRADED"
        patch["failure_class"] = "AUTH_REQUIRED"
    else:
        # Zero results — do not mark healthy; leave unknown/degraded pending zero-result safety
        patch["lifecycle"] = "VALIDATING"
        if s.get("health_state") == SRC_HEALTHY:
            patch["health_state"] = SRC_DEGRADED
            patch["failure_class"] = "POSSIBLE_PARSER_FAILURE_OR_EMPTY"
    out = registry.upsert(patch)
    return {"ok": True, "source": out}


def coverage_metrics_honest(registry: ProcurementSourceRegistry) -> dict[str, Any]:
    """SOURCE HEALTH separate from MARKET COVERAGE CONFIDENCE."""
    audit = audit_source_network(registry)
    by = audit["by_audit_class"]
    total = audit["registered_sources"]
    productive = by.get(AUDIT_HEALTHY, 0) + by.get(AUDIT_PARTIAL, 0) + by.get(AUDIT_META_ONLY, 0)
    # Honest confidence: never near 100% from registry fraction alone
    known_frac = productive / total if total else 0.0
    market_confidence = min(0.35, round(known_frac * 0.4 + 0.05, 3))  # deliberately capped honesty
    return {
        "kind": "CoverageMetrics",
        "SOURCE_HEALTH": {
            "registered": total,
            "healthy_production": by.get(AUDIT_HEALTHY, 0),
            "partially_productive": by.get(AUDIT_PARTIAL, 0) + by.get(AUDIT_META_ONLY, 0),
            "public_metadata_only": by.get(AUDIT_META_ONLY, 0),
            "degraded": by.get("DEGRADED", 0) + by.get(AUDIT_CHANGED, 0) + by.get(AUDIT_TEMP, 0),
            "auth_gated": by.get(AUDIT_AUTH, 0),
            "registration_required": by.get("REGISTRATION_REQUIRED", 0),
            "bot_protected": by.get(AUDIT_BOT, 0),
            "broken": by.get(AUDIT_BROKEN, 0),
            "unsupported": by.get(AUDIT_UNSUPPORTED, 0),
            "unknown": by.get(AUDIT_UNKNOWN, 0),
            "productive_fraction_of_registry": round(productive / total, 4) if total else 0,
            "note": "Registry productive fraction ≠ national market coverage",
        },
        "MARKET_COVERAGE_CONFIDENCE": {
            "score": market_confidence,
            "claim_100_percent_national_coverage": False,
            "interpretation": "LOW_PARTIAL — major jurisdiction/entity gaps remain",
        },
        "by_platform_family": audit["by_platform_family"],
    }
