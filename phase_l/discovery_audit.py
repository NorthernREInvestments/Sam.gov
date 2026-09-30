"""Phase L.10 — discovery coverage audit + DiscoveryGap catalog."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.platform_history import PLATFORM_HISTORY_ADAPTERS, detect_platform
from phase_l.source_inventory import SOURCE_INVENTORY

BUILD = "20260928-m3-phase-l10-exact-evidence-workflow"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"

GAP_BUYER_COVERAGE = "BUYER_COVERAGE_GAP"
GAP_STATE_COVERAGE = "STATE_COVERAGE_GAP"
GAP_PLATFORM_ENUMERATION = "PLATFORM_ENUMERATION_GAP"
GAP_CATEGORY = "CATEGORY_GAP"
GAP_ATTACHMENT = "ATTACHMENT_GAP"
GAP_AUTH_REQUIRED = "AUTH_REQUIRED_GAP"
GAP_AWARD_HISTORY = "AWARD_HISTORY_GAP"
GAP_SOURCE_HEALTH = "SOURCE_HEALTH_GAP"


def _utc() -> str:
    return now_utc().isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Expected national platforms — must appear in audit
EXPECTED_PLATFORMS = (
    "SAM",
    "DLA/DIBBS",
    "BidNet",
    "OpenGov",
    "Bonfire",
    "Public Purchase",
    "DemandStar",
    "PlanetBids",
    "IonWave",
    "Jaggaer/SciQuest",
    "state_portals",
    "cooperatives",
)


def audit_discovery_coverage(
    rows: list[dict[str, Any]] | None = None,
    *,
    hunt_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if rows is None:
        data = _load_json(OUT / "accessible_latest.json")
        rows = list(data.get("rows") or [])
    hunt = hunt_meta or _load_json(OUT / "hunt_latest.json")
    health = _load_json(ROOT / "data" / "phase_l29_source_health.json")

    by_source: dict[str, dict[str, Any]] = {}
    platform_counts = Counter()
    state_counts = Counter()
    dup_keys: set[str] = set()
    duplicates = 0
    live_open = 0
    stale = 0
    tangible = 0
    commercial = 0
    auth_resolved = 0
    attach_yes = 0
    attach_no = 0

    for row in rows:
        raw = row.get("raw_ref") if isinstance(row.get("raw_ref"), dict) else {}
        src = str(
            raw.get("source_id")
            or row.get("source_family")
            or row.get("source")
            or detect_platform(row)
            or "unknown"
        )
        platform = detect_platform(row)
        platform_counts[platform] += 1
        bucket = by_source.setdefault(
            src,
            {
                "source_platform": src,
                "buyers_reachable": set(),
                "opportunities_returned": 0,
                "live_open": 0,
                "stale": 0,
                "duplicate": 0,
                "tangible_products": 0,
                "commercial_products": 0,
                "authoritative_source_resolved": 0,
                "attachment_access": 0,
                "attachment_missing": 0,
                "award_history_access": 0,
                "source_health": (health.get(src) or health.get("sources", {}).get(src) or {}).get("status")
                if isinstance(health, dict)
                else "unknown",
            },
        )
        bucket["opportunities_returned"] += 1
        buyer = str(row.get("agency") or row.get("buyer") or "")
        if buyer:
            bucket["buyers_reachable"].add(buyer[:80])
        state = str(row.get("state") or row.get("buyer_state") or "")
        if not state and "bidnet_" in src:
            # network_bidnet_texas → TX-ish label
            parts = src.split("bidnet_")
            if len(parts) > 1:
                state = parts[-1].replace("_", " ")[:24]
                state_counts[state] += 1
        elif state:
            state_counts[state] += 1

        key = str(
            row.get("notice_id")
            or row.get("solicitation_id")
            or (raw.get("external_id") if raw else None)
            or row.get("url")
            or ""
        )
        if key and key in dup_keys:
            duplicates += 1
            bucket["duplicate"] += 1
        elif key:
            dup_keys.add(key)

        status = str(row.get("status") or row.get("live_status") or row.get("notice_type") or "").lower()
        if any(x in status for x in ("closed", "awarded", "cancelled", "inactive")):
            stale += 1
            bucket["stale"] += 1
        else:
            live_open += 1
            bucket["live_open"] += 1

        title = str(row.get("title") or "").lower()
        if row.get("is_product") or any(
            x in title
            for x in ("vehicle", "truck", "equipment", "server", "laptop", "parts", "nsn", "supply", "furniture")
        ):
            tangible += 1
            bucket["tangible_products"] += 1
        if row.get("commercial_likely") or "commercial" in str(row.get("lane") or "").lower():
            commercial += 1
            bucket["commercial_products"] += 1

        if row.get("original_posting_url") or row.get("url") or raw.get("external_id"):
            auth_resolved += 1
            bucket["authoritative_source_resolved"] += 1
        atts = row.get("attachments") or row.get("attachment_urls") or []
        if atts:
            attach_yes += 1
            bucket["attachment_access"] += 1
        else:
            attach_no += 1
            bucket["attachment_missing"] += 1

        if row.get("award_history") or row.get("historical_unit_price") or row.get("historical_award_unit_price"):
            bucket["award_history_access"] += 1

    # Serialize sets
    report_sources = []
    for src, b in sorted(by_source.items(), key=lambda x: -x[1]["opportunities_returned"]):
        report_sources.append(
            {
                **{k: v for k, v in b.items() if k != "buyers_reachable"},
                "buyers_reachable": len(b["buyers_reachable"]),
                "platform_detected": src,
            }
        )

    gaps = generate_discovery_gaps(
        platform_counts=platform_counts,
        state_counts=state_counts,
        report_sources=report_sources,
        attach_missing=attach_no,
        rows_n=len(rows),
    )

    productive = [s["source_platform"] for s in report_sources[:8] if s["opportunities_returned"] > 10]
    weak = [
        p
        for p in EXPECTED_PLATFORMS
        if platform_counts.get(p, 0) < 5 and p not in {"cooperatives", "state_portals"}
    ]

    return {
        "kind": "DiscoveryCoverageAudit",
        "build": BUILD,
        "generated_at": _utc(),
        "totals": {
            "raw_rows": len(rows),
            "live_open": live_open,
            "stale": stale,
            "duplicate": duplicates,
            "tangible_products": tangible,
            "commercial_products": commercial,
            "authoritative_source_resolved": auth_resolved,
            "attachment_access": attach_yes,
            "attachment_missing": attach_no,
        },
        "by_source": report_sources,
        "platform_counts": dict(platform_counts),
        "state_counts": dict(state_counts.most_common(30)),
        "productive_sources": productive,
        "weak_sources": weak,
        "inventory_size": len(SOURCE_INVENTORY),
        "history_adapters": {k: v.get("status") for k, v in PLATFORM_HISTORY_ADAPTERS.items()},
        "hunt_meta_keys": list((hunt or {}).keys())[:20],
        "gaps": gaps,
        "national_coverage_claimed": False,
    }


def generate_discovery_gaps(
    *,
    platform_counts: Counter,
    state_counts: Counter,
    report_sources: list[dict[str, Any]],
    attach_missing: int,
    rows_n: int,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for p in EXPECTED_PLATFORMS:
        n = platform_counts.get(p, 0)
        if n == 0:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_PLATFORM_ENUMERATION,
                    "platform": p,
                    "detail": f"No opportunities attributed to {p} in current corpus",
                    "severity": "high",
                }
            )
        elif n < 10 and p in {"OpenGov", "Bonfire", "IonWave", "PlanetBids", "DemandStar"}:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_PLATFORM_ENUMERATION,
                    "platform": p,
                    "detail": f"Only {n} rows for {p} — partial buyer enumeration likely",
                    "severity": "medium",
                }
            )

    # State coverage — thin states
    if state_counts:
        for st, n in state_counts.most_common():
            pass
        thin = [st for st, n in state_counts.items() if n < 3]
        # Also missing large states entirely is a gap if we have state data
        major = {"CA", "TX", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI"}
        present = set(state_counts.keys())
        for st in major - present:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_STATE_COVERAGE,
                    "state": st,
                    "detail": f"Major state {st} not represented in state field",
                    "severity": "medium",
                }
            )
        if len(thin) > 5:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_STATE_COVERAGE,
                    "detail": f"{len(thin)} states with <3 opportunities",
                    "states_sample": thin[:15],
                    "severity": "low",
                }
            )

    if attach_missing > rows_n * 0.5 and rows_n:
        gaps.append(
            {
                "kind": "DiscoveryGap",
                "category": GAP_ATTACHMENT,
                "detail": f"{attach_missing}/{rows_n} rows lack attachment access",
                "severity": "high",
            }
        )

    for src in report_sources:
        if src.get("award_history_access", 0) == 0 and src.get("opportunities_returned", 0) > 20:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_AWARD_HISTORY,
                    "source": src.get("source_platform"),
                    "detail": "Opportunities present but no award/history access on rows",
                    "severity": "high",
                }
            )
        health = str(src.get("source_health") or "")
        if health in {"SOURCE_DEGRADED", "SOURCE_DISABLED", "SOURCE_BOT_BLOCKED", "degraded", "disabled"}:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_SOURCE_HEALTH,
                    "source": src.get("source_platform"),
                    "detail": f"Source health={health}",
                    "severity": "medium",
                }
            )

    # Auth-required platforms
    for p, meta in PLATFORM_HISTORY_ADAPTERS.items():
        if meta.get("auth_often_required") is True and meta.get("status") in {"stub", "partial"}:
            gaps.append(
                {
                    "kind": "DiscoveryGap",
                    "category": GAP_AUTH_REQUIRED,
                    "platform": p,
                    "detail": "History/tabulations often require supplier auth",
                    "severity": "medium",
                }
            )

    # Buyer class / category underrepresentation (heuristic)
    gaps.append(
        {
            "kind": "DiscoveryGap",
            "category": GAP_BUYER_COVERAGE,
            "detail": "K-12 / special districts / utilities underrepresented vs cities/DOD in commercial lane",
            "severity": "medium",
        }
    )
    gaps.append(
        {
            "kind": "DiscoveryGap",
            "category": GAP_CATEGORY,
            "detail": "Commercial medical devices, lab equipment, and food-service equipment under-sampled vs vehicles/IT",
            "severity": "medium",
        }
    )

    return gaps
