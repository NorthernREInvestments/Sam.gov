"""Geographic / source coverage matrix helpers (Build 002G capability levels)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from m3.models import SourceRegistry

US_STATES_DC = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]

# Capability levels (must be independently verified with live evidence)
# 0 discovered, 1 verified official, 2 retrieves individual solicitations,
# 3 verifies active status + details, 4 complete packages


def infer_capability_level(row: SourceRegistry, yield_now: dict | None = None) -> int:
    cfg = row.config or {}
    y = yield_now or {}
    if cfg.get("capability_level") is not None:
        try:
            return max(0, min(4, int(cfg["capability_level"])))
        except (TypeError, ValueError):
            pass
    verified_indiv = int(y.get("verified_individual_solicitations") or y.get("genuine_solicitations") or 0)
    complete = int(y.get("complete_packages") or y.get("package_complete") or 0)
    accepted = int(y.get("accepted") or y.get("items") or y.get("yield") or 0)
    if complete > 0:
        return 4
    if verified_indiv > 0 and (y.get("detail_ok") or cfg.get("document_access") in {"public_html", "public_pdf"}):
        return 3
    if verified_indiv > 0 or (accepted > 0 and cfg.get("dataset_purpose") == "individual_solicitation_candidate"):
        return 2
    if row.experimentally_verified or cfg.get("operational"):
        return 1
    if cfg.get("discovered") or row.documented_ok:
        return 0
    return 0


def persist_source_intelligence(
    db: Session,
    source_key: str,
    *,
    yield_stats: dict[str, Any] | None = None,
    capability_level: int | None = None,
    last_success: bool = False,
) -> None:
    """Persist reusable source/platform configuration without inventing evidence."""
    row = db.query(SourceRegistry).filter_by(source_key=source_key).one_or_none()
    if not row:
        return
    cfg = dict(row.config or {})
    if yield_stats:
        cfg["last_yield"] = {
            k: yield_stats.get(k)
            for k in (
                "accepted",
                "verified_individual_solicitations",
                "complete_packages",
                "failed_packages",
                "pages_fetched",
                "rows_examined",
            )
            if yield_stats.get(k) is not None
        }
        cfg["last_unique_solicitation_yield"] = yield_stats.get("verified_individual_solicitations")
        cfg["last_package_yield"] = yield_stats.get("complete_packages")
    if capability_level is not None:
        cfg["capability_level"] = int(capability_level)
        if capability_level >= 1:
            row.experimentally_verified = True
            cfg["operational"] = True
    if last_success:
        from datetime import datetime, timezone

        row.last_success_at = datetime.now(timezone.utc)
    row.config = cfg
    db.add(row)


def build_coverage_matrix(db: Session, source_yield: list[dict] | None = None) -> dict[str, Any]:
    rows = db.query(SourceRegistry).filter(SourceRegistry.access_cost == "free").all()
    yield_map = {y.get("source_key"): y for y in (source_yield or []) if y.get("source_key")}
    by_state: dict[str, dict] = {
        st: {
            "known": 0,
            "operational": 0,
            "discovered_not_operational": 0,
            "level_0": 0,
            "level_1": 0,
            "level_2": 0,
            "level_3": 0,
            "level_4": 0,
            "sources": [],
        }
        for st in US_STATES_DC
    }
    operational_total = 0
    level_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for row in rows:
        cfg = row.config or {}
        st = (cfg.get("geo_state") or "").upper()
        y = yield_map.get(row.source_key) or {}
        level = infer_capability_level(row, y)
        level_counts[level] = level_counts.get(level, 0) + 1
        entry = {
            "source_key": row.source_key,
            "name": row.name,
            "adapter": row.adapter,
            "enabled": row.is_enabled,
            "verified": row.experimentally_verified,
            "operational": bool(cfg.get("operational") or row.experimentally_verified or level >= 1),
            "discovered": bool(cfg.get("discovered")),
            "capability_level": level,
            "dataset_purpose": cfg.get("dataset_purpose") or cfg.get("record_purpose"),
            "last_yield": cfg.get("last_yield"),
            "yield_now": y,
        }
        if st in by_state:
            by_state[st]["known"] += 1
            by_state[st]["sources"].append(entry)
            by_state[st][f"level_{level}"] = by_state[st].get(f"level_{level}", 0) + 1
            if entry["operational"] and row.is_enabled:
                by_state[st]["operational"] += 1
                operational_total += 1
            elif cfg.get("discovered") and not entry["operational"]:
                by_state[st]["discovered_not_operational"] += 1

    weak = [st for st, v in by_state.items() if v["operational"] == 0]
    thin = [st for st, v in by_state.items() if v["operational"] == 1]
    level2_plus_states = [st for st, v in by_state.items() if (v.get("level_2", 0) + v.get("level_3", 0) + v.get("level_4", 0)) > 0]
    level4_states = [st for st, v in by_state.items() if v.get("level_4", 0) > 0]
    return {
        "states": by_state,
        "states_with_any_source": sum(1 for v in by_state.values() if v["known"] > 0),
        "states_with_operational": sum(1 for v in by_state.values() if v["operational"] > 0),
        "states_with_level2_plus": len(level2_plus_states),
        "states_with_level4": len(level4_states),
        "level2_plus_states": level2_plus_states,
        "level4_states": level4_states,
        "operational_sources_geotagged": operational_total,
        "capability_level_counts": level_counts,
        "zero_operational_states": weak,
        "single_portal_only_states": thin,
        "note": "LEVEL0=discovered; LEVEL1=verified source; LEVEL2=individual solicitations; LEVEL3=active details; LEVEL4=complete packages",
    }
