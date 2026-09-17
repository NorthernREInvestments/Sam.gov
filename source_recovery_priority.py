"""SOURCE_RECOVERY_SCORE + TOP_SOURCE_RECOVERY_QUEUE.

Prioritize highest-value source recovery — not popularity, not equal effort.
Maximize valuable procurement coverage via measurable signals + portal-family leverage.
"""

from __future__ import annotations

from typing import Any

from discovery.selection import select_all_eligible_sources, _pool_map
from discovery.source_failure_taxonomy import (
    AUTH_REQUIRED,
    BOT_CHALLENGE,
    CAPTCHA,
    HTTP_403,
    HTTP_401,
    PARSER_FAILURE,
    REGISTRATION_REQUIRED,
    STALE_URL,
    VALID_HEALTHY_ZERO,
    classify_root_cause,
)

# Explicit high-value families (priority order from mission)
FAMILY_PRIORITY: dict[str, int] = {
    "SAM_GOV": 100,
    "SAM": 100,
    "DLA": 98,
    "DIBBS": 95,
    "PIEE": 90,
    "STATEWIDE": 85,
    "JAGGAER": 88,
    "JAGGAER_SCIQUEST": 88,
    "SCIQUEST": 88,
    "BIDNET": 82,
    "BONFIRE": 78,
    "PUBLIC_PURCHASE": 74,
    "IONWAVE": 72,
    "UNIVERSITY": 68,
    "CITY": 55,
    "COUNTY": 55,
    "UTILITY": 50,
    "AIRPORT": 50,
    "TRANSIT": 50,
}

# Classification buckets for blocked sources
WORTH_FIXING_NOW = "worth_fixing_now"
WORTH_FIXING_LATER = "worth_fixing_later"
OPERATOR_CREDENTIAL_REQUIRED = "operator_credential_required"
PERMANENTLY_BLOCKED = "permanently_blocked_without_external_action"
LOW_VALUE = "low_value"

# Operator-facing health states (Phase 9)
HEALTH_STATES = {
    "PRODUCTIVE",
    "HEALTHY_ZERO",
    "PARSER_FAILURE",
    "AUTH_REQUIRED",
    "REGISTRATION_REQUIRED",
    "BOT_PROTECTED",
    "TECHNICAL_FAILURE",
    "STALE_ROUTE",
    "LOW_VALUE",
    "RETIRED",
}


def _family(cand: dict[str, Any]) -> str:
    return str(
        cand.get("platform_family")
        or cand.get("adapter_family")
        or cand.get("source_family")
        or "UNKNOWN"
    ).upper()


def _est_opportunities(metrics: dict[str, Any], family: str, family_size: int) -> int:
    raw = int(metrics.get("raw") or metrics.get("records_fetched") or 0)
    if raw > 0:
        return raw
    # Peer productivity heuristic when this source is blocked
    base = {
        "SAM_GOV": 40,
        "SAM": 40,
        "DLA": 25,
        "DIBBS": 20,
        "JAGGAER": 15,
        "JAGGAER_SCIQUEST": 15,
        "SCIQUEST": 15,
        "BIDNET": 12,
        "BONFIRE": 10,
        "PUBLIC_PURCHASE": 8,
        "IONWAVE": 8,
        "STATEWIDE": 20,
        "UNIVERSITY": 6,
    }.get(family, 3)
    # Family leverage: unlocking one fix may unlock peers
    leverage = max(1, min(family_size, 20))
    return int(base * (1 + 0.15 * (leverage - 1)))


def map_health_state(classification: dict[str, Any], metrics: dict[str, Any]) -> str:
    primary = str(classification.get("primary") or "")
    if classification.get("productive") or (metrics.get("ok") and int(metrics.get("raw") or 0) > 0):
        return "PRODUCTIVE"
    if primary == VALID_HEALTHY_ZERO:
        return "HEALTHY_ZERO"
    if primary == PARSER_FAILURE:
        return "PARSER_FAILURE"
    if primary in {AUTH_REQUIRED, HTTP_401}:
        return "AUTH_REQUIRED"
    if primary == REGISTRATION_REQUIRED:
        return "REGISTRATION_REQUIRED"
    if primary in {BOT_CHALLENGE, CAPTCHA, HTTP_403}:
        return "BOT_PROTECTED"
    if primary in {STALE_URL, "HTTP_404", "PORTAL_MIGRATED"}:
        return "STALE_ROUTE"
    if primary == "SOURCE_RETIRED":
        return "RETIRED"
    if primary in {"NOT_YET_ATTEMPTED"}:
        return "TECHNICAL_FAILURE"
    return "TECHNICAL_FAILURE"


def classify_recovery_bucket(
    *,
    health: str,
    discovery_value: float,
    business_value: float,
    technical_value: float,
) -> str:
    if health == "PRODUCTIVE":
        return "productive"
    if health == "HEALTHY_ZERO":
        return "healthy_zero"
    if health == "RETIRED" or (discovery_value < 8 and business_value < 10):
        return LOW_VALUE
    if health in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED"}:
        return OPERATOR_CREDENTIAL_REQUIRED
    if health == "BOT_PROTECTED" and technical_value < 40:
        return PERMANENTLY_BLOCKED
    if health in {"PARSER_FAILURE", "STALE_ROUTE", "TECHNICAL_FAILURE"} and (
        discovery_value >= 25 or technical_value >= 50
    ):
        return WORTH_FIXING_NOW
    if discovery_value >= 15 or business_value >= 40:
        return WORTH_FIXING_LATER
    return LOW_VALUE


def score_source(
    cand: dict[str, Any],
    *,
    metrics: dict[str, Any] | None = None,
    family_size: int = 1,
    peer_raw_avg: float = 0.0,
) -> dict[str, Any]:
    metrics = metrics if isinstance(metrics, dict) else {}
    classification = classify_root_cause(metrics) if metrics else {
        "primary": "NOT_YET_ATTEMPTED",
        "access_outcome": "UNKNOWN",
        "productive": False,
    }
    family = _family(cand)
    raw = int(metrics.get("raw") or metrics.get("records_fetched") or 0)
    unique = int(metrics.get("unique") or 0)
    survivors = int(metrics.get("product_candidates") or metrics.get("survivors") or unique or 0)
    research_qual = int(metrics.get("research_qualified") or 0)

    # DISCOVERY VALUE (0-100)
    discovery = 0.0
    discovery += min(40.0, raw * 1.5)
    discovery += min(25.0, unique * 2.0)
    discovery += min(20.0, survivors * 2.5)
    discovery += min(10.0, research_qual * 3.0)
    if peer_raw_avg > 0 and raw == 0:
        discovery += min(20.0, peer_raw_avg * 0.8)  # blocked but peers produce
    kind = str(cand.get("kind") or "").upper()
    if kind in {"FEDERAL", "FED"}:
        discovery += 12
    elif kind in {"STATE", "STATEWIDE"}:
        discovery += 8
    elif kind in {"COOP", "COOPERATIVE"}:
        discovery += 6
    discovery = min(100.0, discovery)

    # TECHNICAL VALUE — portal family reuse / unlock leverage
    fam_pri = FAMILY_PRIORITY.get(family, 20)
    sources_unlocked_per_fix = max(1, family_size)
    technical = min(
        100.0,
        fam_pri * 0.55
        + min(40.0, sources_unlocked_per_fix * 2.5)
        + (15 if classification.get("primary") in {PARSER_FAILURE, STALE_URL, "HTML_CHANGED"} else 0)
        + (10 if health_publicly_fixable(classification) else 0),
    )

    # BUSINESS VALUE — tangible goods / reseller fit
    business = 35.0
    name = str(cand.get("name") or "").lower()
    if any(x in name for x in ("dla", "defense logistics", "gsa", "federal")):
        business += 30
    if any(x in name for x in ("university", "school", "college")):
        business += 8
    if any(x in name for x in ("utility", "airport", "transit", "port")):
        business += 5
    if family in FAMILY_PRIORITY and FAMILY_PRIORITY[family] >= 70:
        business += 15
    if kind in {"FEDERAL", "STATE", "COOP"}:
        business += 10
    business = min(100.0, business)

    health = map_health_state(classification, metrics)
    # Combined SOURCE_RECOVERY_SCORE — weight discovery + leverage + business
    score = round(0.40 * discovery + 0.35 * technical + 0.25 * business, 2)
    if health == "PRODUCTIVE":
        score = 0.0  # already productive — not a recovery target
    bucket = classify_recovery_bucket(
        health=health,
        discovery_value=discovery,
        business_value=business,
        technical_value=technical,
    )
    est = _est_opportunities(metrics, family, family_size)
    effort = _effort(health, classification)

    why_blocked = (
        classification.get("primary")
        or metrics.get("source_stop_reason")
        or metrics.get("error")
        or health
    )
    return {
        "source": cand.get("source_id"),
        "source_name": cand.get("name"),
        "portal_family": family,
        "current_state": health,
        "why_blocked": why_blocked,
        "potential_upside": _upside_blurb(family, kind, est),
        "estimated_opportunities_unlocked": est,
        "technical_effort": effort,
        "priority": score,
        "SOURCE_RECOVERY_SCORE": score,
        "DISCOVERY_VALUE": round(discovery, 2),
        "TECHNICAL_VALUE": round(technical, 2),
        "BUSINESS_VALUE": round(business, 2),
        "SOURCES_UNLOCKED_PER_FIX": sources_unlocked_per_fix,
        "recovery_bucket": bucket,
        "root_cause": classification.get("primary"),
        "jurisdiction": cand.get("kind") or cand.get("state_code"),
        "canonical_url": cand.get("list_url"),
        "raw_records": raw,
        "unique_records": unique,
    }


def health_publicly_fixable(classification: dict[str, Any]) -> bool:
    return str(classification.get("primary") or "") in {
        PARSER_FAILURE,
        STALE_URL,
        "HTML_CHANGED",
        "WRONG_ENDPOINT",
        "HTTP_404",
        "PORTAL_MIGRATED",
        "EMPTY_RESPONSE",
        "TIMEOUT",
        "TLS_NETWORK_FAILURE",
    }


def _effort(health: str, classification: dict[str, Any]) -> str:
    if health in {"PARSER_FAILURE", "STALE_ROUTE"}:
        return "LOW-MEDIUM"
    if health == "TECHNICAL_FAILURE":
        return "MEDIUM"
    if health in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED"}:
        return "OPERATOR"
    if health == "BOT_PROTECTED":
        return "HIGH_EXTERNAL"
    return "MEDIUM"


def _upside_blurb(family: str, kind: str, est: int) -> str:
    if family in {"JAGGAER", "JAGGAER_SCIQUEST", "SCIQUEST"}:
        return f"JAGGAER family reuse — ~{est} opps; unlocks peer entities on same adapter"
    if family in {"BIDNET"}:
        return f"BidNet multi-state leverage — ~{est} opps"
    if family in {"SAM", "SAM_GOV", "DLA", "DIBBS"}:
        return f"Federal/DLA product depth — ~{est} opps"
    if str(kind).upper() in {"STATE", "STATEWIDE"}:
        return f"Statewide portal — ~{est} opps across agencies"
    return f"Estimated ~{est} product-resale opportunities if recovered"


def build_source_recovery_queue(
    *,
    per_source_metrics: dict[str, Any] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """TOP_SOURCE_RECOVERY_QUEUE answering: where does the next 8 hours of fixes pay off?"""
    bundle = select_all_eligible_sources(include_blocked_accounted=True)
    pool = _pool_map()
    per = dict(per_source_metrics or {})

    # Enrich from registry health when last-run per_source is unavailable
    try:
        from procurement_source_registry import ProcurementSourceRegistry

        reg = ProcurementSourceRegistry()
        for s in reg.all_sources():
            sid = s.get("source_id")
            if not sid or sid in per:
                continue
            if s.get("productive") or s.get("operator_health_state") == "PRODUCTIVE":
                per[sid] = {
                    "ok": True,
                    "raw": int(s.get("records_seen") or s.get("last_records_seen") or 1),
                    "unique": int(s.get("records_seen") or 1),
                    "source_stop_reason": "COMPLETED",
                }
            elif s.get("root_cause") or s.get("failure_class"):
                per[sid] = {
                    "ok": False,
                    "raw": 0,
                    "source_stop_reason": s.get("root_cause") or s.get("failure_class"),
                    "error": s.get("failure_class"),
                }
    except Exception:
        pass

    # Family sizes for leverage
    family_sizes: dict[str, int] = {}
    peer_raw: dict[str, list[int]] = {}
    all_cands = list(bundle.get("eligible") or []) + list(bundle.get("accounted_non_attempt") or [])
    # Also include pool entries not in selection
    seen = {c.get("source_id") for c in all_cands}
    for sid, cand in pool.items():
        if sid not in seen:
            all_cands.append(cand)

    for cand in all_cands:
        fam = _family(cand)
        family_sizes[fam] = family_sizes.get(fam, 0) + 1
        m = per.get(cand.get("source_id")) if isinstance(per.get(cand.get("source_id")), dict) else {}
        raw = int(m.get("raw") or m.get("records_fetched") or 0)
        if raw > 0:
            peer_raw.setdefault(fam, []).append(raw)

    scored: list[dict[str, Any]] = []
    productive = 0
    blocked = 0
    for cand in all_cands:
        sid = cand.get("source_id")
        m = per.get(sid) if isinstance(per.get(sid), dict) else {}
        fam = _family(cand)
        peers = peer_raw.get(fam) or []
        peer_avg = sum(peers) / len(peers) if peers else 0.0
        row = score_source(
            cand,
            metrics=m,
            family_size=family_sizes.get(fam, 1),
            peer_raw_avg=peer_avg,
        )
        if row["current_state"] == "PRODUCTIVE":
            productive += 1
        else:
            blocked += 1
            scored.append(row)

    scored.sort(
        key=lambda r: (
            0 if r["recovery_bucket"] == WORTH_FIXING_NOW else 1,
            0 if r["recovery_bucket"] == OPERATOR_CREDENTIAL_REQUIRED else 1,
            -float(r["SOURCE_RECOVERY_SCORE"]),
            -int(r["SOURCES_UNLOCKED_PER_FIX"]),
        )
    )

    top = scored[:limit]
    by_family: dict[str, list[dict[str, Any]]] = {}
    for r in scored:
        by_family.setdefault(r["portal_family"], []).append(r)

    family_leverage = []
    for fam, rows in by_family.items():
        if fam == "UNKNOWN":
            continue
        family_leverage.append(
            {
                "portal_family": fam,
                "blocked_sources": len(rows),
                "SOURCES_UNLOCKED_PER_FIX": len(rows),
                "avg_recovery_score": round(
                    sum(float(x["SOURCE_RECOVERY_SCORE"]) for x in rows) / max(1, len(rows)), 2
                ),
                "family_priority": FAMILY_PRIORITY.get(fam, 20),
                "top_sources": [x["source"] for x in rows[:5]],
            }
        )
    family_leverage.sort(
        key=lambda x: (-x["family_priority"], -x["SOURCES_UNLOCKED_PER_FIX"], -x["avg_recovery_score"])
    )

    operator_actions = [
        {
            "SOURCE": r["source_name"] or r["source"],
            "STATUS": r["current_state"],
            "WHY_IT_MATTERS": r["potential_upside"],
            "UNLOCKS": f"{r['estimated_opportunities_unlocked']} est. opportunities; family {r['portal_family']}",
            "ACTION": (
                "Create vendor access / provide credentials"
                if r["recovery_bucket"] == OPERATOR_CREDENTIAL_REQUIRED
                else "External portal action required — do not auto-register"
            ),
            "priority": r["SOURCE_RECOVERY_SCORE"],
        }
        for r in scored
        if r["recovery_bucket"] == OPERATOR_CREDENTIAL_REQUIRED
    ][:15]

    return {
        "kind": "TOP_SOURCE_RECOVERY_QUEUE",
        "question": "If we spend the next 8 hours fixing sources, where does the highest return come from?",
        "productive_sources": productive,
        "blocked_sources": blocked,
        "queue": top,
        "TOP_10": top[:10],
        "family_leverage": family_leverage[:12],
        "operator_action_queue": operator_actions,
        "buckets": {
            WORTH_FIXING_NOW: sum(1 for r in scored if r["recovery_bucket"] == WORTH_FIXING_NOW),
            WORTH_FIXING_LATER: sum(1 for r in scored if r["recovery_bucket"] == WORTH_FIXING_LATER),
            OPERATOR_CREDENTIAL_REQUIRED: sum(
                1 for r in scored if r["recovery_bucket"] == OPERATOR_CREDENTIAL_REQUIRED
            ),
            PERMANENTLY_BLOCKED: sum(1 for r in scored if r["recovery_bucket"] == PERMANENTLY_BLOCKED),
            LOW_VALUE: sum(1 for r in scored if r["recovery_bucket"] == LOW_VALUE),
        },
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def coverage_dashboard_payload(
    *,
    per_source_metrics: dict[str, Any] | None = None,
    discovery_status: dict[str, Any] | None = None,
    pipeline_store: Any = None,
) -> dict[str, Any]:
    queue = build_source_recovery_queue(per_source_metrics=per_source_metrics, limit=10)
    bundle = select_all_eligible_sources(include_blocked_accounted=True)
    eligible = int(bundle.get("eligible_count") or len(bundle.get("eligible") or []))
    focus = discovery_status or {}
    last = focus.get("last_successful_completion") or focus.get("last_attempt") or {}
    handoff = focus.get("PIPELINE_HANDOFF") or {}
    disc = focus.get("DISCOVERY") or {}

    pipeline_discovered = handoff.get("discovered") or last.get("product_screen_survivors")
    pipeline_stored = handoff.get("transferred")
    pipeline_failed = handoff.get("failed") or 0
    pipeline_retrying = 1 if handoff.get("retrying") else 0

    needs_operator = queue["buckets"].get(OPERATOR_CREDENTIAL_REQUIRED, 0)
    blocked = queue.get("blocked_sources") or 0
    productive = queue.get("productive_sources") or 0

    return {
        "kind": "M3CoverageDashboard",
        "TOTAL_SOURCES": {
            "Eligible": eligible,
            "Attempted": disc.get("sources_attempted") or last.get("sources_attempted"),
            "Productive": productive if per_source_metrics else last.get("sources_successful"),
            "Fallback_productive": None,
            "Blocked": blocked,
            "Needs_operator": needs_operator,
        },
        "DISCOVERY": {
            "Raw": disc.get("records_fetched") or last.get("records_retrieved"),
            "Unique": disc.get("unique_records") or last.get("unique_records"),
            "Product_survivors": disc.get("product_survivors") or last.get("product_screen_survivors"),
            "Research_candidates": focus.get("RESEARCH", {}).get("queued")
            or last.get("deep_research_queued"),
        },
        "PIPELINE": {
            "Discovered": pipeline_discovered,
            "Stored": pipeline_stored,
            "Failed": pipeline_failed,
            "Retrying": pipeline_retrying,
        },
        "SOURCE_RECOVERY": {"Top_10": queue.get("TOP_10") or []},
        "operator_action_queue": queue.get("operator_action_queue") or [],
        "family_leverage": queue.get("family_leverage") or [],
    }
