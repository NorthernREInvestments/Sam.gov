"""Phase L.14 — non-BidNet commercial source expansion: priority, yield, caps."""

from __future__ import annotations

from typing import Any

BUILD = "20260928-m3-phase-l14-nonbidnet-source-expansion"

# Default work order (§5) — yield telemetry may reorder later
SOURCE_PRIORITY_ORDER = (
    "OpenGov",
    "IonWave",
    "PlanetBids",
    "Bonfire",
    "DemandStar",
    "Jaggaer/SciQuest",
    "Public Purchase",
    "DLA/DIBBS",
    "cooperatives",
    "state_portals",
)

# Hunt kind caps — cut BidNet NETWORK; lift local/state/coop/platform seeds
L14_KIND_CAPS = {
    "STATE": 32,
    "LOCAL": 40,
    "NETWORK": 4,  # BidNet discovery only if already working — not history
    "COOPERATIVE": 16,
    "FEDERAL": 8,  # DLA/DIBBS + limited SAM
}

L14_DEFAULT_MAX_SOURCES = 96

PARKED_ACCESS_DEPENDENCY = "PARKED_ACCESS_DEPENDENCY"

# Commercial category search tokens
CATEGORY_SEARCH_TERMS = {
    "IT": ("computers", "servers", "monitors", "networking", "printers", "AV equipment"),
    "Fleet": ("pickup", "SUV", "van", "police vehicle", "trailer"),
    "Equipment": ("skid steer", "utility vehicle", "mower", "tractor", "forklift", "generator"),
    "MRO": ("pump", "motor", "bearing", "valve", "electrical", "HVAC"),
    "Tools": ("power tools", "hand tools", "shop equipment"),
    "Lab": ("meter", "oscilloscope", "analyzer", "scientific equipment"),
    "Facility": ("furniture", "shelving", "appliance", "storage", "cleaning equipment"),
}

COMMERCIAL_BRANDS_L14 = (
    "Dell", "HP", "Lenovo", "Cisco", "Canon", "Epson",
    "Ford", "Chevrolet", "Ram", "Bobcat", "John Deere", "Kubota",
    "Milwaukee", "DeWalt", "Fluke", "Tektronix", "Keysight",
)

NON_BIDNET_ADAPTERS = (
    "live_opengov",
    "live_ionwave",
    "live_planetbids",
    "live_bonfire",
    "live_demandstar",
    "live_jaggaer",
    "live_public_purchase",
    "live_dibbs",
    "live_cooperative",
    "live_state_owned_html",
)


def source_priority_score(platform: str) -> int:
    try:
        return len(SOURCE_PRIORITY_ORDER) - SOURCE_PRIORITY_ORDER.index(platform)
    except ValueError:
        # BidNet lowest for this phase (parked history; discovery deprioritized)
        if "bidnet" in platform.lower():
            return -10
        return 0


def prioritize_candidates_nonbidnet(
    candidates: list[dict[str, Any]],
    *,
    kind_caps: dict[str, int] | None = None,
    max_sources: int | None = None,
    exclude_bidnet_network: bool = True,
) -> list[dict[str, Any]]:
    """Prefer OpenGov/IonWave/PlanetBids/… over BidNet network sources."""
    caps = kind_caps or L14_KIND_CAPS

    def _score(c: dict[str, Any]) -> tuple:
        plat = str(c.get("platform_family") or "")
        adapter = str(c.get("adapter_family") or "").lower()
        sid = str(c.get("source_id") or "").lower()
        name = str(c.get("name") or "").lower()
        # Hard deprioritize BidNet
        if exclude_bidnet_network and ("bidnet" in adapter or "bidnet" in sid or "bidnet" in plat.lower()):
            return (100, sid)
        pri = 0
        for i, p in enumerate(SOURCE_PRIORITY_ORDER):
            key = p.lower().replace("/", "").replace(" ", "")
            blob = f"{plat} {adapter} {sid} {name}".lower().replace("/", "").replace(" ", "")
            if key[:6] in blob or p.split("/")[0].lower() in blob:
                pri = 50 - i
                break
        if any(a in adapter for a in ("opengov", "ionwave", "planetbids", "bonfire")):
            pri = max(pri, 45)
        if "public_purchase" in adapter or "demandstar" in adapter or "jaggaer" in adapter:
            pri = max(pri, 40)
        if "dibbs" in adapter or "dla" in sid:
            pri = max(pri, 35)
        if "coop" in adapter or "sourcewell" in sid or "omnia" in sid:
            pri = max(pri, 30)
        validation = 2 if c.get("validation_candidate") else 0
        return (-(pri + validation), sid)

    filtered = []
    for c in candidates:
        sid = str(c.get("source_id") or "").lower()
        adapter = str(c.get("adapter_family") or "").lower()
        if exclude_bidnet_network and ("bidnet" in sid or "bidnet" in adapter):
            # Keep at most NETWORK cap later — skip from primary pool
            continue
        filtered.append(c)

    # Allow a tiny BidNet discovery residue under NETWORK cap only if needed to fill
    bidnet_residue = [
        c
        for c in candidates
        if "bidnet" in str(c.get("source_id") or "").lower()
        or "bidnet" in str(c.get("adapter_family") or "").lower()
    ]

    by_kind: dict[str, list] = {k: [] for k in ("STATE", "LOCAL", "NETWORK", "COOPERATIVE", "FEDERAL")}
    for c in filtered:
        k = str(c.get("kind") or "LOCAL").upper()
        if k == "NETWORK":
            by_kind["NETWORK"].append(c)
        elif k in by_kind:
            by_kind[k].append(c)
        else:
            by_kind["LOCAL"].append(c)

    for k in by_kind:
        by_kind[k].sort(key=_score)

    # Fill NETWORK with non-BidNet first; only then tiny BidNet residue
    by_kind["NETWORK"].extend(sorted(bidnet_residue, key=_score)[: caps.get("NETWORK", 4)])

    # Interleave kinds so LOCAL cannot starve COOPERATIVE/FEDERAL when max_sources bites
    kind_order = ("COOPERATIVE", "FEDERAL", "LOCAL", "STATE", "NETWORK")
    pools: dict[str, list] = {}
    for kind in kind_order:
        pools[kind] = by_kind.get(kind, [])[: caps.get(kind, 16)]

    diversified: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _take(c: dict[str, Any]) -> bool:
        sid = str(c.get("source_id") or id(c))
        if sid in seen:
            return False
        seen.add(sid)
        diversified.append(c)
        return True

    # Round 1: guarantee at least one source per priority platform family when present
    for plat in SOURCE_PRIORITY_ORDER:
        key = plat.lower().replace("/", "").replace(" ", "")[:6]
        for kind in kind_order:
            for c in pools.get(kind, []):
                blob = f"{c.get('platform_family')} {c.get('adapter_family')} {c.get('source_id')}".lower()
                if key in blob.replace("/", "").replace(" ", "") or plat.split("/")[0].lower() in blob:
                    if _take(c):
                        break
            else:
                continue
            break

    # Round 2: round-robin remaining by kind
    idxs = {k: 0 for k in kind_order}
    progressed = True
    while progressed:
        progressed = False
        for kind in kind_order:
            pool = pools.get(kind, [])
            i = idxs[kind]
            while i < len(pool):
                c = pool[i]
                i += 1
                idxs[kind] = i
                if _take(c):
                    progressed = True
                    break

    if max_sources:
        diversified = diversified[:max_sources]
    return diversified


def source_economic_yield(
    *,
    source_id: str,
    raw: int,
    commercial: int,
    stage3: int,
    gov_abc: int,
    quote_targets: int,
    http_requests: int = 1,
) -> dict[str, Any]:
    """SourceEconomicYield — rank sources by useful commercial evidence."""
    raw = max(int(raw or 0), 0)
    stage3 = max(int(stage3 or 0), 0)
    return {
        "kind": "SourceEconomicYield",
        "source_id": source_id,
        "raw": raw,
        "commercial": commercial,
        "stage3": stage3,
        "gov_abc": gov_abc,
        "quote_targets": quote_targets,
        "commercial_stage3_per_100_raw": round(100.0 * commercial / max(raw, 1), 2),
        "gov_abc_per_100_stage3": round(100.0 * gov_abc / max(stage3, 1), 2),
        "quote_targets_per_100_stage3": round(100.0 * quote_targets / max(stage3, 1), 2),
        "cost_per_useful_row": round(max(http_requests, 1) / max(gov_abc + quote_targets, 1), 2),
        "http_requests": http_requests,
    }


def parked_access_dependency(
    *,
    platform: str,
    opportunities_affected: int,
    potential_value: float = 0.0,
    unlock_method: str,
) -> dict[str, Any]:
    return {
        "kind": PARKED_ACCESS_DEPENDENCY,
        "platform": platform,
        "opportunities_affected": opportunities_affected,
        "potential_value": potential_value,
        "unlock_method": unlock_method,
        "create_account": False,
        "engineering_effort": "parked",
    }
