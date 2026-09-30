"""JurisdictionProcurementRegistry — Lower-48 state/county/municipality coverage (L.17).

Registry is exhaustive (Census-backed). Automation/enrichment is prioritized and resumable.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.lower48 import (
    AUTH_BLOCKED,
    AUTOMATED_STATIC,
    AUTOMATED_TIER1,
    AUTOMATED_TIER2,
    BUILD,
    FREE_REGISTRATION_REQUIRED,
    LOWER_48,
    LOWER_48_SET,
    MANUAL_PUBLIC,
    PORTAL_DISCOVERED_NOT_INTEGRATED,
    UNKNOWN_RESEARCH_PENDING,
    classify_source_status,
    normalize_jurisdiction_id,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
COUNTIES_PATH = DATA / "lower48_counties.json"
MUNIS_PATH = DATA / "lower48_municipalities.json"
REGISTRY_PATH = DATA / "jurisdiction_procurement_registry.json"
CHECKPOINT_PATH = DATA / "l17_coverage_checkpoint.json"


def _utc() -> str:
    return now_utc().isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: dict[str, Any], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_checkpoint() -> dict[str, Any]:
    return _load_json(CHECKPOINT_PATH) or {
        "completed_jurisdiction_ids": [],
        "enriched": {},
        "updated_at": None,
    }


def save_checkpoint(cp: dict[str, Any]) -> None:
    cp["updated_at"] = _utc()
    cp["build"] = BUILD
    _save_json(CHECKPOINT_PATH, cp)


def empty_jurisdiction(
    *,
    state: str,
    buyer_type: str,
    name: str,
    geoid: str | None = None,
    county_equivalent: str | None = None,
    city: str | None = None,
    population: int | None = None,
    automation_priority: str = "C",
) -> dict[str, Any]:
    jid = normalize_jurisdiction_id(state=state, buyer_type=buyer_type, name=name, geoid=geoid)
    return {
        "jurisdiction_id": jid,
        "state": state,
        "county_equivalent": county_equivalent,
        "city": city,
        "name": name,
        "geoid": geoid,
        "buyer_type": buyer_type,
        "population": population,
        "official_domain": None,
        "procurement_page": None,
        "bid_portal": None,
        "procurement_platform": None,
        "structured_source_available": False,
        "api_feed_export": False,
        "public_solicitation_visibility": None,
        "public_award_history_visibility": None,
        "registration_required": None,
        "registration_free": None,
        "registration_required_before_bid": None,
        "registration_url": None,
        "original_submission_location": None,
        "automation_status": UNKNOWN_RESEARCH_PENDING,
        "source_status": UNKNOWN_RESEARCH_PENDING,
        "automation_priority": automation_priority,
        "last_discovery_attempt": None,
        "last_successful_discovery": None,
        "source_health": None,
        "current_open_opportunity_count": 0,
        "adapter_family": None,
        "structured_source_ids": [],
    }


def build_base_registry(*, include_municipalities: bool = True) -> dict[str, Any]:
    """Exhaustive base inventory: 48 states + all counties + incorporated municipalities."""
    jurisdictions: dict[str, dict[str, Any]] = {}

    # --- States from STATE_MATRIX (Lower-48 only) ---
    from discovery.state_matrix import STATE_MATRIX

    for s in STATE_MATRIX:
        st = s.get("state")
        if st not in LOWER_48_SET:
            continue
        auth = bool(s.get("auth_required"))
        public = bool(s.get("publicly_searchable"))
        status = classify_source_status(
            adapter_status=s.get("adapter_status_seed"),
            auth_required=auth,
            publicly_searchable=public,
            platform=s.get("platform_family"),
            portal_url=s.get("list_url"),
            automated=bool(s.get("adapter_family") and public and not auth),
        )
        # Public + adapter but not yet structured tier → PORTAL or STATIC
        if status == UNKNOWN_RESEARCH_PENDING and s.get("list_url"):
            status = PORTAL_DISCOVERED_NOT_INTEGRATED if not public else AUTOMATED_STATIC
        row = empty_jurisdiction(
            state=st,
            buyer_type="STATE",
            name=s.get("name") or st,
            automation_priority="A",
        )
        row.update(
            {
                "procurement_page": s.get("list_url"),
                "bid_portal": s.get("list_url"),
                "procurement_platform": s.get("platform_family"),
                "adapter_family": s.get("adapter_family"),
                "public_solicitation_visibility": public,
                "registration_required": auth,
                "source_status": status,
                "automation_status": status,
                "portal_name": s.get("portal_name"),
                "restrictions": s.get("restrictions"),
            }
        )
        jurisdictions[row["jurisdiction_id"]] = row

    # --- Counties ---
    counties_blob = _load_json(COUNTIES_PATH)
    for c in counties_blob.get("counties") or []:
        st = c["state"]
        if st not in LOWER_48_SET:
            continue
        row = empty_jurisdiction(
            state=st,
            buyer_type="COUNTY",
            name=c["name"],
            geoid=c.get("geoid"),
            county_equivalent=c["name"],
            automation_priority="B",
        )
        jurisdictions[row["jurisdiction_id"]] = row

    # --- Municipalities ---
    muni_count = 0
    if include_municipalities:
        munis_blob = _load_json(MUNIS_PATH)
        for m in munis_blob.get("municipalities") or []:
            st = m["state"]
            if st not in LOWER_48_SET:
                continue
            # Priority A for large-name capitals / known commercial centers handled in enrich
            row = empty_jurisdiction(
                state=st,
                buyer_type="CITY",
                name=m["name"],
                geoid=m.get("geoid"),
                city=m["name"],
                automation_priority="C",
            )
            jurisdictions[row["jurisdiction_id"]] = row
            muni_count += 1

    return {
        "kind": "JurisdictionProcurementRegistry",
        "build": BUILD,
        "generated_at": _utc(),
        "lower_48_states": list(LOWER_48),
        "counts": {
            "total": len(jurisdictions),
            "states": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "STATE"),
            "counties": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "COUNTY"),
            "municipalities": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "CITY"),
        },
        "jurisdictions": jurisdictions,
    }


def _match_county(jurisdictions: dict[str, dict[str, Any]], state: str, name_hint: str) -> dict[str, Any] | None:
    hint = name_hint.lower().replace(" county", "").strip()
    for j in jurisdictions.values():
        if j["state"] != state or j["buyer_type"] != "COUNTY":
            continue
        n = j["name"].lower().replace(" county", "").replace(" parish", "").strip()
        if hint in n or n in hint:
            return j
    return None


def _match_city(jurisdictions: dict[str, dict[str, Any]], state: str, name_hint: str) -> dict[str, Any] | None:
    hint = name_hint.lower().replace(" city", "").replace("town of ", "").strip()
    # Prefer exact-ish matches
    best = None
    for j in jurisdictions.values():
        if j["state"] != state or j["buyer_type"] != "CITY":
            continue
        n = j["name"].lower()
        if n == hint or n.startswith(hint + " ") or hint == n.replace(" city", ""):
            return j
        if hint in n and best is None:
            best = j
    return best


def enrich_registry_from_known_sources(registry: dict[str, Any]) -> dict[str, Any]:
    """Apply known agency seeds, structured sources, BidNet networks — resumable via checkpoint."""
    jurisdictions: dict[str, dict[str, Any]] = registry["jurisdictions"]
    cp = load_checkpoint()
    enriched_ids: set[str] = set(cp.get("completed_jurisdiction_ids") or [])
    supplemental: list[dict[str, Any]] = []

    # Priority A city names (capitals + major commercial centers)
    priority_a_cities = {
        ("CA", "los angeles"), ("CA", "san diego"), ("CA", "san francisco"), ("CA", "san jose"),
        ("CA", "sacramento"), ("TX", "houston"), ("TX", "dallas"), ("TX", "austin"),
        ("TX", "san antonio"), ("TX", "fort worth"), ("NY", "new york"), ("IL", "chicago"),
        ("AZ", "phoenix"), ("PA", "philadelphia"), ("PA", "pittsburgh"), ("FL", "miami"),
        ("FL", "jacksonville"), ("FL", "tampa"), ("OH", "columbus"), ("OH", "cleveland"),
        ("NC", "charlotte"), ("NC", "raleigh"), ("GA", "atlanta"), ("MI", "detroit"),
        ("WA", "seattle"), ("MA", "boston"), ("CO", "denver"), ("TN", "nashville"),
        ("TN", "memphis"), ("OR", "portland"), ("NV", "las vegas"), ("MO", "kansas city"),
        ("MO", "st. louis"), ("IN", "indianapolis"), ("WI", "milwaukee"), ("MN", "minneapolis"),
        ("MD", "baltimore"), ("LA", "new orleans"), ("VA", "virginia beach"), ("VA", "richmond"),
        ("NE", "omaha"), ("NE", "lincoln"), ("OK", "oklahoma city"), ("OK", "tulsa"),
        ("UT", "salt lake city"), ("NM", "albuquerque"), ("CT", "hartford"), ("DE", "wilmington"),
        ("AL", "birmingham"), ("AL", "montgomery"), ("SC", "columbia"), ("SC", "charleston"),
        ("KY", "louisville"), ("KY", "lexington"), ("AR", "little rock"), ("MS", "jackson"),
        ("IA", "des moines"), ("KS", "wichita"), ("WV", "charleston"), ("ID", "boise"),
        ("MT", "billings"), ("WY", "cheyenne"), ("SD", "sioux falls"), ("ND", "fargo"),
        ("NH", "manchester"), ("ME", "portland"), ("VT", "burlington"), ("RI", "providence"),
        ("NJ", "newark"), ("NJ", "jersey city"),
    }

    for j in jurisdictions.values():
        if j["buyer_type"] != "CITY":
            continue
        key = (j["state"], j["name"].lower().replace(" city", "").strip())
        key2 = (j["state"], j["name"].lower())
        if key in priority_a_cities or key2 in priority_a_cities:
            j["automation_priority"] = "A"

    # Agency seeds
    try:
        from discovery.agency_seeds import AGENCY_SEEDS, COOPERATIVE_LIVE_SOURCES

        for a in AGENCY_SEEDS:
            st = a.get("state_code")
            if st not in LOWER_48_SET and st != "US":
                continue
            bt = str(a.get("buyer_type") or "CITY").upper()
            name = a.get("name") or a.get("agency_key")
            url = a.get("procurement_url")
            plat = a.get("platform_family")
            auth = bool(a.get("auth_required"))
            status = classify_source_status(
                auth_required=auth,
                publicly_searchable=not auth,
                platform=plat,
                portal_url=url,
                automated=bool(a.get("adapter_family") and url and not auth),
            )
            if "bidnet" in str(plat or "").lower():
                status = FREE_REGISTRATION_REQUIRED
            elif status == UNKNOWN_RESEARCH_PENDING and url:
                status = PORTAL_DISCOVERED_NOT_INTEGRATED

            target = None
            if bt == "COUNTY":
                target = _match_county(jurisdictions, st, name or "")
            elif bt in {"CITY", "AIRPORT", "TRANSIT", "PUBLIC_UTILITY", "SCHOOL_DISTRICT", "PUBLIC_UNIVERSITY"}:
                # Match city if possible; else supplemental
                city_hint = a.get("city") or name
                target = _match_city(jurisdictions, st, str(city_hint or ""))
                if bt != "CITY":
                    # Supplemental public buyer
                    sid = normalize_jurisdiction_id(state=st, buyer_type=bt, name=str(name))
                    if sid not in jurisdictions:
                        row = empty_jurisdiction(
                            state=st,
                            buyer_type=bt,
                            name=str(name),
                            city=a.get("city"),
                            automation_priority="A",
                        )
                        row.update(
                            {
                                "procurement_page": url,
                                "bid_portal": url,
                                "procurement_platform": plat,
                                "adapter_family": a.get("adapter_family"),
                                "source_status": status,
                                "automation_status": status,
                                "supplemental_buyer": True,
                                "agency_key": a.get("agency_key"),
                            }
                        )
                        jurisdictions[sid] = row
                        supplemental.append(row)
                        enriched_ids.add(sid)
                    continue

            if target:
                target["procurement_page"] = url or target.get("procurement_page")
                target["bid_portal"] = url or target.get("bid_portal")
                target["procurement_platform"] = plat or target.get("procurement_platform")
                target["adapter_family"] = a.get("adapter_family") or target.get("adapter_family")
                target["source_status"] = status
                target["automation_status"] = status
                target["registration_required"] = auth
                target["automation_priority"] = "A"
                target["last_discovery_attempt"] = _utc()
                if url:
                    target["last_successful_discovery"] = _utc()
                enriched_ids.add(target["jurisdiction_id"])

        for c in COOPERATIVE_LIVE_SOURCES:
            sid = normalize_jurisdiction_id(
                state="US", buyer_type="COOPERATIVE", name=c.get("name") or c.get("source_id")
            )
            if sid in jurisdictions:
                continue
            row = empty_jurisdiction(
                state="US",
                buyer_type="COOPERATIVE",
                name=str(c.get("name") or c.get("source_id")),
                automation_priority="A",
            )
            row.update(
                {
                    "procurement_page": c.get("list_url"),
                    "bid_portal": c.get("list_url"),
                    "procurement_platform": "Cooperative",
                    "adapter_family": c.get("adapter_family"),
                    "source_status": AUTOMATED_STATIC,
                    "automation_status": AUTOMATED_STATIC,
                    "supplemental_buyer": True,
                    "multi_state": True,
                }
            )
            jurisdictions[sid] = row
            supplemental.append(row)
    except Exception:
        pass

    # Structured open-data sources → AUTOMATED_TIER2
    try:
        from discovery.structured_source_registry import STRUCTURED_HISTORY_SOURCES, STRUCTURED_LIVE_SOURCES

        for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES:
            st = s.get("state_code")
            if not st or st not in LOWER_48_SET:
                continue
            bt = str(s.get("buyer_type") or "CITY").upper()
            name = s.get("name") or s.get("source_id")
            status = AUTOMATED_TIER2 if s.get("adapter_kind") == "socrata" else AUTOMATED_TIER1
            target = None
            if bt == "COUNTY":
                target = _match_county(jurisdictions, st, str(name))
            elif bt == "STATE":
                for j in jurisdictions.values():
                    if j["state"] == st and j["buyer_type"] == "STATE":
                        target = j
                        break
            else:
                target = _match_city(jurisdictions, st, str(name))
            if target:
                target["structured_source_available"] = True
                target["api_feed_export"] = True
                target["source_status"] = status
                target["automation_status"] = status
                target["procurement_platform"] = target.get("procurement_platform") or "Socrata"
                target["adapter_family"] = "live_structured"
                target.setdefault("structured_source_ids", []).append(s.get("source_id"))
                target["last_successful_discovery"] = _utc()
                target["automation_priority"] = "A"
                enriched_ids.add(target["jurisdiction_id"])
            else:
                # Attach as supplemental structured buyer
                sid = normalize_jurisdiction_id(state=st, buyer_type=bt, name=str(name))
                if sid not in jurisdictions:
                    row = empty_jurisdiction(
                        state=st, buyer_type=bt, name=str(name), automation_priority="A"
                    )
                    row.update(
                        {
                            "structured_source_available": True,
                            "api_feed_export": True,
                            "source_status": status,
                            "automation_status": status,
                            "procurement_page": s.get("list_url"),
                            "bid_portal": s.get("list_url"),
                            "procurement_platform": "Socrata",
                            "adapter_family": "live_structured",
                            "structured_source_ids": [s.get("source_id")],
                            "supplemental_buyer": True,
                        }
                    )
                    jurisdictions[sid] = row
                    supplemental.append(row)
                    enriched_ids.add(sid)
    except Exception:
        pass

    # BidNet statewide networks → many local jurisdictions share platform
    try:
        from discovery.bidnet_network import all_bidnet_networks_enriched

        for n in all_bidnet_networks_enriched():
            st = n.get("state_code")
            if st not in LOWER_48_SET:
                continue
            # Mark state-level BidNet as free-reg / portal discovered
            for j in jurisdictions.values():
                if j["state"] == st and j["buyer_type"] == "STATE":
                    if j.get("source_status") in {UNKNOWN_RESEARCH_PENDING, None}:
                        j["procurement_platform"] = "BidNet"
                        j["bid_portal"] = n.get("list_url")
                        j["source_status"] = FREE_REGISTRATION_REQUIRED
                        j["automation_status"] = FREE_REGISTRATION_REQUIRED
                        j["registration_required"] = True
                        j["registration_free"] = True
                        j["registration_required_before_bid"] = True
                        j["adapter_family"] = "live_bidnet"
                        enriched_ids.add(j["jurisdiction_id"])
    except Exception:
        pass

    cp["completed_jurisdiction_ids"] = sorted(enriched_ids)
    cp["enriched_count"] = len(enriched_ids)
    save_checkpoint(cp)

    registry["jurisdictions"] = jurisdictions
    registry["counts"] = {
        "total": len(jurisdictions),
        "states": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "STATE"),
        "counties": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "COUNTY"),
        "municipalities": sum(1 for j in jurisdictions.values() if j["buyer_type"] == "CITY"),
        "supplemental": sum(1 for j in jurisdictions.values() if j.get("supplemental_buyer")),
        "enriched": len(enriched_ids),
    }
    registry["supplemental_buyers_sample"] = supplemental[:40]
    registry["checkpoint"] = {"enriched_ids": len(enriched_ids), "path": str(CHECKPOINT_PATH)}
    registry["generated_at"] = _utc()
    return registry


def status_breakdown(registry: dict[str, Any]) -> dict[str, int]:
    c: Counter = Counter()
    for j in (registry.get("jurisdictions") or {}).values():
        c[str(j.get("source_status") or UNKNOWN_RESEARCH_PENDING)] += 1
    return dict(c)


def platform_jurisdiction_map(registry: dict[str, Any]) -> dict[str, Any]:
    by_plat: dict[str, list[str]] = defaultdict(list)
    for j in (registry.get("jurisdictions") or {}).values():
        plat = j.get("procurement_platform")
        if not plat:
            continue
        by_plat[str(plat)].append(j["jurisdiction_id"])
    ranked = sorted(by_plat.items(), key=lambda x: -len(x[1]))
    return {
        "kind": "PlatformJurisdictionMap",
        "build": BUILD,
        "generated_at": _utc(),
        "platforms": [
            {
                "platform": p,
                "jurisdiction_count": len(ids),
                "sample_jurisdiction_ids": ids[:15],
                "adapter_status": "VARIES",
                "known_auth_issues": p in {"BidNet", "DemandStar", "Public Purchase", "OpenGov"},
                "engineering_priority_hint": round(len(ids) / max(1, 50 if p in {"OpenGov", "Bonfire"} else 10), 2),
            }
            for p, ids in ranked
        ],
    }


def coverage_percentages(registry: dict[str, Any]) -> dict[str, Any]:
    juris = list((registry.get("jurisdictions") or {}).values())
    states = [j for j in juris if j["buyer_type"] == "STATE" and j["state"] in LOWER_48_SET]
    counties = [j for j in juris if j["buyer_type"] == "COUNTY"]
    munis = [j for j in juris if j["buyer_type"] == "CITY" and not j.get("supplemental_buyer")]

    def mapped(rows: list[dict[str, Any]]) -> int:
        return sum(
            1
            for j in rows
            if j.get("source_status") not in {UNKNOWN_RESEARCH_PENDING, None, ""}
        )

    def automated(rows: list[dict[str, Any]]) -> int:
        return sum(
            1
            for j in rows
            if j.get("source_status") in {AUTOMATED_TIER1, AUTOMATED_TIER2, AUTOMATED_STATIC}
        )

    def public_or_manual(rows: list[dict[str, Any]]) -> int:
        return sum(
            1
            for j in rows
            if j.get("source_status")
            in {MANUAL_PUBLIC, PORTAL_DISCOVERED_NOT_INTEGRATED, FREE_REGISTRATION_REQUIRED}
        )

    return {
        "state_source_mapped_pct": round(100.0 * mapped(states) / max(1, len(states)), 2),
        "county_source_mapped_pct": round(100.0 * mapped(counties) / max(1, len(counties)), 2),
        "municipality_source_mapped_pct": round(100.0 * mapped(munis) / max(1, len(munis)), 2),
        "automated_coverage_pct_of_mapped": round(
            100.0 * automated(juris) / max(1, mapped(juris)), 2
        ),
        "public_manual_coverage_pct_of_mapped": round(
            100.0 * public_or_manual(juris) / max(1, mapped(juris)), 2
        ),
        "denominators": {
            "states": len(states),
            "counties": len(counties),
            "municipalities": len(munis),
            "total": len(juris),
            "mapped_total": mapped(juris),
        },
    }


def build_and_persist_registry(*, include_municipalities: bool = True) -> dict[str, Any]:
    reg = build_base_registry(include_municipalities=include_municipalities)
    reg = enrich_registry_from_known_sources(reg)
    # Persist compact summary + full registry (may be large)
    _save_json(REGISTRY_PATH, reg, compact=True)
    return reg


def registry_summary_rows(registry: dict[str, Any], *, buyer_type: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for j in (registry.get("jurisdictions") or {}).values():
        if buyer_type and j.get("buyer_type") != buyer_type:
            continue
        rows.append(
            {
                "jurisdiction_id": j.get("jurisdiction_id"),
                "state": j.get("state"),
                "name": j.get("name"),
                "buyer_type": j.get("buyer_type"),
                "source_status": j.get("source_status"),
                "platform": j.get("procurement_platform"),
                "procurement_page": j.get("procurement_page"),
                "automation_priority": j.get("automation_priority"),
                "structured": j.get("structured_source_available"),
            }
        )
    return rows
