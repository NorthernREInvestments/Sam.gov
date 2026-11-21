"""Geographic / source coverage matrix helpers."""

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


def build_coverage_matrix(db: Session, source_yield: list[dict] | None = None) -> dict[str, Any]:
    rows = db.query(SourceRegistry).filter(SourceRegistry.access_cost == "free").all()
    yield_map = {y.get("source_key"): y for y in (source_yield or []) if y.get("source_key")}
    by_state: dict[str, dict] = {st: {"known": 0, "operational": 0, "discovered_not_operational": 0, "sources": []} for st in US_STATES_DC}
    operational_total = 0
    for row in rows:
        cfg = row.config or {}
        st = (cfg.get("geo_state") or "").upper()
        if st not in by_state:
            continue
        entry = {
            "source_key": row.source_key,
            "name": row.name,
            "adapter": row.adapter,
            "enabled": row.is_enabled,
            "verified": row.experimentally_verified,
            "operational": bool(cfg.get("operational") or row.experimentally_verified),
            "discovered": bool(cfg.get("discovered")),
            "last_yield": cfg.get("last_yield"),
            "yield_now": yield_map.get(row.source_key),
        }
        by_state[st]["known"] += 1
        by_state[st]["sources"].append(entry)
        if entry["operational"] and row.is_enabled:
            by_state[st]["operational"] += 1
            operational_total += 1
        elif cfg.get("discovered") and not entry["operational"]:
            by_state[st]["discovered_not_operational"] += 1

    weak = [st for st, v in by_state.items() if v["operational"] == 0]
    thin = [st for st, v in by_state.items() if v["operational"] == 1]
    return {
        "states": by_state,
        "states_with_any_source": sum(1 for v in by_state.values() if v["known"] > 0),
        "states_with_operational": sum(1 for v in by_state.values() if v["operational"] > 0),
        "operational_sources_geotagged": operational_total,
        "zero_operational_states": weak,
        "single_portal_only_states": thin,
    }
