"""Source coverage reporting — VERIFIED counted separately from UNVERIFIED."""

from __future__ import annotations

from typing import Any

from discovery.agency_seeds import all_agencies_enriched, all_coops_enriched, FEDERAL_NON_SAM_LIVE
from discovery.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BLOCKED,
    ADAPTER_BROKEN,
    ADAPTER_DEGRADED,
    ADAPTER_FIXTURE_ONLY,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_UNVERIFIED_LIVE,
)
from discovery.live_fetchers import list_live_capable_fetchers
from discovery.state_matrix import all_states_enriched, state_coverage_summary


def build_coverage_report(session: Any | None = None, *, load_persisted: bool = True) -> dict[str, Any]:
    if load_persisted:
        if session is not None:
            from discovery.state_matrix import load_status_overrides_from_db

            load_status_overrides_from_db(session)
        else:
            try:
                from database import SessionLocal
                from discovery.state_matrix import load_status_overrides_from_db

                _s = SessionLocal()
                try:
                    load_status_overrides_from_db(_s)
                finally:
                    _s.close()
            except Exception:
                pass

    states = state_coverage_summary()
    agencies = all_agencies_enriched()
    coops = all_coops_enriched()

    def by_status(rows: list[dict], status: str) -> list:
        return [r for r in rows if r.get("adapter_status") == status]

    all_sources: list[dict[str, Any]] = []
    for s in all_states_enriched():
        all_sources.append(s)
    for a in agencies:
        all_sources.append(a)
    for c in coops:
        all_sources.append(c)
    for f in FEDERAL_NON_SAM_LIVE:
        st = ADAPTER_LIVE_VERIFIED if False else (
            ADAPTER_UNVERIFIED_LIVE if f.get("list_url") else "PARTIAL"
        )
        from discovery.state_matrix import get_status_overrides

        ov = get_status_overrides()
        if f["source_id"] in ov:
            st = ov[f["source_id"]]
        all_sources.append({**f, "adapter_status": st, "kind": "FEDERAL"})

    counts = {
        "REGISTERED": len(all_sources) + 6,  # +fixture adapters represented
        "FETCHER_AVAILABLE": len(list_live_capable_fetchers()),
        "UNVERIFIED_LIVE": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_UNVERIFIED_LIVE),
        "LIVE_VERIFIED": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_LIVE_VERIFIED),
        "DEGRADED": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_DEGRADED),
        "AUTH_REQUIRED": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_AUTH_REQUIRED),
        "BLOCKED": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_BLOCKED),
        "BROKEN": sum(1 for r in all_sources if r.get("adapter_status") == ADAPTER_BROKEN),
    }

    live_states = by_status(all_states_enriched(), ADAPTER_LIVE_VERIFIED)
    live_agencies = by_status(agencies, ADAPTER_LIVE_VERIFIED)
    live_coops = by_status(coops, ADAPTER_LIVE_VERIFIED)
    live_fed = [f for f in all_sources if f.get("kind") == "FEDERAL" and f.get("adapter_status") == ADAPTER_LIVE_VERIFIED]

    return {
        "states": states,
        "counts": counts,
        "local_agencies_registered": len(agencies),
        "local_agencies_UNVERIFIED_LIVE": len(by_status(agencies, ADAPTER_UNVERIFIED_LIVE)),
        "local_agencies_LIVE_VERIFIED": len(live_agencies),
        "cooperatives_UNVERIFIED_LIVE": len(by_status(coops, ADAPTER_UNVERIFIED_LIVE)),
        "cooperatives_LIVE_VERIFIED": len(live_coops),
        "federal_non_sam_LIVE_VERIFIED": len(live_fed),
        "TOTAL_LIVE_VERIFIED_SOURCES": counts["LIVE_VERIFIED"],
        "TOTAL_UNVERIFIED_LIVE_SOURCES": counts["UNVERIFIED_LIVE"],
        "TOTAL_LIVE_CAPABLE_SOURCES": counts["LIVE_VERIFIED"],  # strict alias
        "TOTAL_LIVE_CAPABLE_STATES": len(live_states),
        "TOTAL_LIVE_VERIFIED_STATES": len(live_states),
        "TOTAL_LIVE_VERIFIED_LOCAL_AGENCIES": len(live_agencies),
        "TOTAL_LIVE_VERIFIED_COOPERATIVES": len(live_coops),
        "TOTAL_LIVE_VERIFIED_FEDERAL_NON_SAM": len(live_fed),
        "FIXTURE_ONLY_ADAPTERS": 6,
        "live_fetcher_implementations": len(list_live_capable_fetchers()),
        "live_verified_detail": {
            "states": [r["state"] for r in live_states],
            "locals": [r.get("source_id") or r.get("agency_key") for r in live_agencies],
            "coops": [r["source_id"] for r in live_coops],
            "federal": [r["source_id"] for r in live_fed],
        },
        "note": (
            "LIVE_VERIFIED requires successful real live validation with parsed records. "
            "UNVERIFIED_LIVE is fetcher+URL only — NOT operational. No invented % of US procurement."
        ),
        "LIVE_API_REQUESTS": 0,
        "SAM": 0,
        "OpenAI": 0,
    }
