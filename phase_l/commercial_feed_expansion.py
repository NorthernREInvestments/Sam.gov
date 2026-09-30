"""Phase L.4 — commercial feed expansion: platforms, state registry, yield, buyers."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    STAGE3_NO_ROW_CAP,
    UNKNOWN_ACQUISITION_CHANNEL,
    classify_acquisition_lane,
)
from phase_l.commercial_discovery import COMMERCIAL_BRANDS, COMMERCIAL_CATEGORY_POOLS

ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_PATH = ROOT / "data" / "phase_l4_commercial_buyer_watchlist.json"
BRAND_MEMORY_PATH = ROOT / "data" / "phase_l4_commercial_brand_memory.json"
YIELD_PATH = ROOT / "data" / "phase_l4_commercial_yield.json"

BUILD = "20260927-m3-phase-l4-commercial-feed-expansion"

# L.3 Stage 3 baseline for comparison
L3_STAGE3_BASELINE = {
    "stage3": 81,
    "quote_required": 9,
    "unknown": 16,
    "milspec_specialty": 56,
    "commercial_share_pct": 11.1,
    "commercially_sourceable": 9,  # open+distributor+quote+milspec-open
}

# Hunt diversification for commercial_feed profile — widen Stage 0, not Stage 3 caps
L4_KIND_CAPS = {
    "STATE": 28,
    "LOCAL": 28,
    "NETWORK": 45,  # BidNet Direct statewide networks — primary commercial lever
    "COOPERATIVE": 12,
    "FEDERAL": 4,
}
L4_DEFAULT_MAX_SOURCES = 110

# Platform inventory (architecture; live health reported separately)
PLATFORM_INVENTORY: list[dict[str, Any]] = [
    {"platform": "OpenGov", "adapter": "live_opengov", "priority": "HIGH", "buyer_classes": ["CITY", "COUNTY", "SCHOOL", "UNIVERSITY"]},
    {"platform": "Bonfire", "adapter": "live_bonfire", "priority": "HIGH", "buyer_classes": ["CITY", "COUNTY", "SCHOOL", "UNIVERSITY", "TRANSIT"]},
    {"platform": "PublicPurchase", "adapter": "live_public_purchase", "priority": "HIGH", "buyer_classes": ["CITY", "COUNTY", "SCHOOL", "UTILITY"]},
    {"platform": "BidNet", "adapter": "live_bidnet", "priority": "HIGH", "buyer_classes": ["MULTI_AGENCY_NETWORK"]},
    {"platform": "DemandStar", "adapter": "live_demandstar", "priority": "MEDIUM", "buyer_classes": ["CITY", "COUNTY", "AIRPORT", "UTILITY"]},
    {"platform": "PlanetBids", "adapter": "live_planetbids", "priority": "HIGH", "buyer_classes": ["CITY", "COUNTY", "UTILITY", "TRANSIT", "SCHOOL"]},
    {"platform": "IonWave", "adapter": "live_ionwave", "priority": "MEDIUM", "buyer_classes": ["CITY", "COUNTY", "SCHOOL", "UNIVERSITY", "UTILITY"]},
    {"platform": "Jaggaer", "adapter": "live_jaggaer", "priority": "MEDIUM", "buyer_classes": ["STATE", "UNIVERSITY"]},
    {"platform": "SciQuest", "adapter": "live_jaggaer", "priority": "MEDIUM", "buyer_classes": ["UNIVERSITY", "STATE"]},
    {"platform": "Periscope", "adapter": None, "priority": "LOW", "buyer_classes": [], "note": "AUTH_REQUIRED when public unavailable"},
    {"platform": "Euna", "adapter": None, "priority": "LOW", "buyer_classes": [], "note": "Expand when public surface found"},
]

COMMERCIAL_PLATFORMS = {
    "OpenGov", "Bonfire", "PlanetBids", "BidNet", "PublicPurchase",
    "DemandStar", "IonWave", "Jaggaer",
}

BUYER_TYPES = (
    "STATE_AGENCY",
    "CITY",
    "COUNTY",
    "SCHOOL_DISTRICT",
    "UNIVERSITY",
    "UTILITY",
    "TRANSIT",
    "AIRPORT",
    "SPECIAL_DISTRICT",
    "COOPERATIVE",
    "FEDERAL",
    "MULTI_AGENCY_NETWORK",
    "OTHER",
)

# Stage 1 commercial triage statuses
KEEP_COMMERCIAL = "KEEP_COMMERCIAL"
KEEP_DISTRIBUTOR = "KEEP_DISTRIBUTOR"
KEEP_QUOTE_REQUIRED = "KEEP_QUOTE_REQUIRED"
KEEP_UNKNOWN = "KEEP_UNKNOWN"
SPECIALTY_PIPELINE = "SPECIALTY_PIPELINE"
HARD_REJECT = "HARD_REJECT"

SOURCE_HEALTH = (
    "HEALTHY",
    "DEGRADED",
    "AUTH_REQUIRED",
    "PARSE_BROKEN",
    "BOT_BLOCKED",
    "NO_CURRENT_RESULTS",
    "DISABLED",
)

REGISTER_BEFORE_BID = "REGISTER_BEFORE_BID"
REGISTER_NOW_RECURRING_BUYER = "REGISTER_NOW_RECURRING_BUYER"
RECURRING_COMMERCIAL_BUYER = "RECURRING_COMMERCIAL_BUYER"
COMMERCIAL_BUYER_WATCHLIST = "COMMERCIAL_BUYER_WATCHLIST"


def _utc() -> str:
    return now_utc().isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def platform_inventory() -> list[dict[str, Any]]:
    return [dict(p) for p in PLATFORM_INVENTORY]


def state_source_registry() -> dict[str, Any]:
    """Structured 50-state (+DC via BidNet) registry view for L.4."""
    from discovery.state_matrix import all_states_enriched
    from discovery.bidnet_network import all_bidnet_networks_enriched

    states = []
    for s in all_states_enriched():
        states.append(
            {
                "state": s.get("state"),
                "name": s.get("name"),
                "primary_portal": s.get("portal_name"),
                "list_url": s.get("list_url"),
                "platform_family": s.get("platform_family"),
                "adapter_family": s.get("adapter_family"),
                "adapter_status": s.get("adapter_status"),
                "publicly_searchable": s.get("publicly_searchable"),
                "auth_required": s.get("auth_required"),
                "priority_tier": _state_priority_tier(s),
            }
        )
    networks = [
        {"state_code": n.get("state_code"), "name": n.get("name"), "list_url": n.get("list_url"), "platform": "BidNet"}
        for n in all_bidnet_networks_enriched()
    ]
    return {
        "kind": "PhaseL4StateSourceRegistry",
        "states": states,
        "state_count": len(states),
        "bidnet_networks": networks,
        "bidnet_network_count": len(networks),
        "generated_at": _utc(),
    }


def _state_priority_tier(s: dict[str, Any]) -> int:
    """1=best public commercial access … 3=gated/auth."""
    if s.get("auth_required") or str(s.get("adapter_status") or "").upper() in {
        "AUTH_REQUIRED",
        "BLOCKED",
        "BROKEN",
    }:
        return 3
    if s.get("publicly_searchable") and s.get("validation_candidate"):
        return 1
    if s.get("publicly_searchable"):
        return 2
    return 3


def classify_buyer_type(row: dict[str, Any]) -> str:
    explicit = str(row.get("buyer_type") or row.get("jurisdiction") or "").upper()
    mapping = {
        "CITY": "CITY",
        "COUNTY": "COUNTY",
        "SCHOOL_DISTRICT": "SCHOOL_DISTRICT",
        "PUBLIC_UNIVERSITY": "UNIVERSITY",
        "PUBLIC_COLLEGE": "UNIVERSITY",
        "UNIVERSITY": "UNIVERSITY",
        "PUBLIC_UTILITY": "UTILITY",
        "UTILITY": "UTILITY",
        "TRANSIT": "TRANSIT",
        "AIRPORT": "AIRPORT",
        "SPECIAL_DISTRICT": "SPECIAL_DISTRICT",
        "COOPERATIVE": "COOPERATIVE",
        "STATE": "STATE_AGENCY",
        "STATE_AGENCY": "STATE_AGENCY",
        "MULTI_AGENCY_NETWORK": "MULTI_AGENCY_NETWORK",
        "FEDERAL": "FEDERAL",
    }
    if explicit in mapping:
        return mapping[explicit]
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("agency", "buyer", "department", "title", "source_id", "source_level")
    )
    if re.search(r"\b(school|ISD|USD|district)\b", blob, re.I):
        return "SCHOOL_DISTRICT"
    if re.search(r"\b(university|college|campus)\b", blob, re.I):
        return "UNIVERSITY"
    if re.search(r"\b(utility|water\s+district|electric\s+coop|DWP|power\s+authority)\b", blob, re.I):
        return "UTILITY"
    if re.search(r"\b(transit|MTA|CTA|metro\s+rail|bus\s+authority)\b", blob, re.I):
        return "TRANSIT"
    if re.search(r"\b(airport|aviation)\b", blob, re.I):
        return "AIRPORT"
    if re.search(r"\b(county|sheriff)\b", blob, re.I):
        return "COUNTY"
    if re.search(r"\b(city|municipal|town of|village of)\b", blob, re.I):
        return "CITY"
    if re.search(r"\b(NASPO|Sourcewell|OMNIA|cooperative|BuyBoard|HGAC)\b", blob, re.I):
        return "COOPERATIVE"
    if re.search(r"\b(DLA|DoD|SAM\.gov|federal|USAF|Army|Navy)\b", blob, re.I):
        return "FEDERAL"
    if str(row.get("source_level") or "").upper() == "STATE":
        return "STATE_AGENCY"
    if str(row.get("source_level") or "").upper() == "LOCAL":
        return "CITY"
    return "OTHER"


def commercial_category_search_queries() -> dict[str, list[str]]:
    """Category keyword queries for portals that support search."""
    return {k: list(v) for k, v in COMMERCIAL_CATEGORY_POOLS.items()}


def brand_model_search_terms(*, brand_memory: dict[str, Any] | None = None) -> list[str]:
    brands = list(COMMERCIAL_BRANDS)
    mem = brand_memory or load_json(BRAND_MEMORY_PATH)
    for b in (mem.get("brands") or []):
        if b and b not in brands:
            brands.append(str(b))
    return brands


def remember_commercial_brand(
    memory: dict[str, Any],
    *,
    manufacturer: str | None,
    product_family: str | None = None,
    keywords: list[str] | None = None,
    portal: str | None = None,
) -> dict[str, Any]:
    memory.setdefault("brands", [])
    memory.setdefault("families", {})
    mfr = (manufacturer or "").strip()
    if mfr and mfr not in memory["brands"]:
        memory["brands"].append(mfr)
    if product_family:
        key = product_family.upper()
        rec = memory["families"].setdefault(key, {"count": 0, "portals": [], "keywords": []})
        rec["count"] = int(rec.get("count") or 0) + 1
        if portal and portal not in rec["portals"]:
            rec["portals"].append(portal)
        for kw in keywords or []:
            if kw and kw not in rec["keywords"]:
                rec["keywords"].append(kw)
    memory["updated_at"] = _utc()
    return memory


def stage1_commercial_triage_status(row: dict[str, Any], *, lane: dict[str, Any] | None = None) -> str:
    """Map acquisition lane → Stage 1 commercial triage status (not eligibility rejection)."""
    lane = lane or classify_acquisition_lane(row)
    al = lane.get("acquisition_lane")
    if al == COMMERCIAL_OPEN_CHANNEL:
        return KEEP_COMMERCIAL
    if al == COMMERCIAL_DISTRIBUTOR_CHANNEL:
        return KEEP_DISTRIBUTOR
    if al == QUOTE_REQUIRED_COMMERCIAL:
        return KEEP_QUOTE_REQUIRED
    if al in {MILSPEC_SPECIALTY, SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
        return SPECIALTY_PIPELINE
    if al == MILSPEC_OPEN_CHANNEL:
        return KEEP_COMMERCIAL
    return KEEP_UNKNOWN


def commercial_yield_rate(
    *,
    commercial_stage3: int,
    tangible: int,
) -> float:
    if tangible <= 0:
        return 0.0
    return round(100.0 * commercial_stage3 / tangible, 1)


def economic_yield_rate(*, economic_positive: int, stage3: int) -> float:
    if stage3 <= 0:
        return 0.0
    return round(100.0 * economic_positive / stage3, 1)


def score_source_commercial_yield(
    *,
    source_id: str,
    raw: int = 0,
    tangible: int = 0,
    commercial: int = 0,
    stage3: int = 0,
    quote_required: int = 0,
    specialty: int = 0,
    http_requests: int = 0,
) -> dict[str, Any]:
    cyr = commercial_yield_rate(commercial_stage3=commercial, tangible=max(tangible, 1))
    cost = max(http_requests, 1)
    return {
        "source_id": source_id,
        "raw": raw,
        "tangible": tangible,
        "commercial": commercial,
        "stage3": stage3,
        "quote_required": quote_required,
        "specialty": specialty,
        "commercial_yield_rate": cyr,
        "cost_per_commercial_stage3": round(cost / max(commercial, 1), 2),
        "http_requests": http_requests,
        "health": "HEALTHY" if commercial > 0 else ("NO_CURRENT_RESULTS" if raw == 0 else "DEGRADED"),
    }


def prioritize_candidates_commercial(
    candidates: list[dict[str, Any]],
    *,
    kind_caps: dict[str, int] | None = None,
    max_sources: int | None = None,
    watchlist: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Platform-weighted diversification favoring commercial-heavy sources."""
    caps = kind_caps or L4_KIND_CAPS
    watch = watchlist or load_json(WATCHLIST_PATH)
    watched = {str(b.get("buyer_key") or "").lower() for b in (watch.get("buyers") or [])}

    def _score(c: dict[str, Any]) -> tuple:
        plat = str(c.get("platform_family") or "")
        adapter = str(c.get("adapter_family") or "")
        sid = str(c.get("source_id") or "").lower()
        name = str(c.get("name") or "").lower()
        commercial_plat = 0
        for p in COMMERCIAL_PLATFORMS:
            if p.lower() in plat.lower() or p.lower().replace(" ", "") in adapter.lower():
                commercial_plat = 10
                break
        if "bidnet" in adapter or "bidnet" in sid:
            commercial_plat = 12  # highest — multi-agency commercial density
        if "opengov" in adapter or "bonfire" in adapter or "planetbids" in adapter:
            commercial_plat = max(commercial_plat, 11)
        watch_boost = 5 if any(w and (w in sid or w in name) for w in watched) else 0
        validation = 1 if c.get("validation_candidate") else 0
        return (-(commercial_plat + watch_boost + validation), sid)

    by_kind: dict[str, list] = {k: [] for k in ("STATE", "LOCAL", "NETWORK", "COOPERATIVE", "FEDERAL")}
    for c in candidates:
        k = str(c.get("kind") or "LOCAL").upper()
        if k == "NETWORK":
            by_kind["NETWORK"].append(c)
        elif k in by_kind:
            by_kind[k].append(c)
        else:
            by_kind["LOCAL"].append(c)

    for k in by_kind:
        by_kind[k].sort(key=_score)

    diversified: list[dict[str, Any]] = []
    for kind, cap in (
        ("NETWORK", caps.get("NETWORK", 45)),
        ("LOCAL", caps.get("LOCAL", 28)),
        ("STATE", caps.get("STATE", 28)),
        ("COOPERATIVE", caps.get("COOPERATIVE", 12)),
        ("FEDERAL", caps.get("FEDERAL", 4)),
    ):
        diversified.extend(by_kind.get(kind, [])[:cap])
    if max_sources:
        diversified = diversified[:max_sources]
    return diversified


def update_buyer_watchlist(
    watchlist: dict[str, Any],
    *,
    buyer: str | None,
    portal: str | None = None,
    product_family: str | None = None,
    lane: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    watchlist.setdefault("buyers", [])
    key = (buyer or "").strip()
    if not key:
        return watchlist
    buyers = watchlist["buyers"]
    rec = next((b for b in buyers if str(b.get("buyer") or "").lower() == key.lower()), None)
    if rec is None:
        rec = {
            "buyer": key,
            "buyer_key": re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")[:80],
            "portal": portal,
            "state": state,
            "product_families": [],
            "qualifying_opportunities": 0,
            "lanes": [],
            "status": COMMERCIAL_BUYER_WATCHLIST,
        }
        buyers.append(rec)
    rec["qualifying_opportunities"] = int(rec.get("qualifying_opportunities") or 0) + 1
    if portal:
        rec["portal"] = portal
    if state:
        rec["state"] = state
    if product_family and product_family not in (rec.get("product_families") or []):
        rec.setdefault("product_families", []).append(product_family)
    if lane and lane not in (rec.get("lanes") or []):
        rec.setdefault("lanes", []).append(lane)
    if rec["qualifying_opportunities"] >= 2 and lane in {
        COMMERCIAL_OPEN_CHANNEL,
        COMMERCIAL_DISTRIBUTOR_CHANNEL,
        QUOTE_REQUIRED_COMMERCIAL,
    }:
        rec["status"] = RECURRING_COMMERCIAL_BUYER
        rec["registration_hint"] = REGISTER_NOW_RECURRING_BUYER
    watchlist["updated_at"] = _utc()
    return watchlist


def expand_buyers_from_platform(
    *,
    productive_buyer: dict[str, Any],
    known_buyers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """When a buyer on a platform is productive, surface peer buyers on same platform."""
    plat = str(productive_buyer.get("platform_family") or productive_buyer.get("portal") or "")
    if not plat:
        return []
    peers = []
    for b in known_buyers:
        if b.get("agency_key") == productive_buyer.get("agency_key"):
            continue
        bp = str(b.get("platform_family") or "")
        if bp and bp.lower() == plat.lower():
            peers.append(
                {
                    "agency_key": b.get("agency_key"),
                    "name": b.get("name"),
                    "platform_family": bp,
                    "expansion_from": productive_buyer.get("agency_key") or productive_buyer.get("buyer"),
                    "reason": "same_platform_peer",
                }
            )
    return peers


def source_family_bucket(row: dict[str, Any]) -> str:
    """Bucket for L.4 contribution report — avoid double-counting."""
    level = str(row.get("source_level") or row.get("level") or "").upper()
    sid = str(row.get("source_id") or "").lower()
    agency = str(row.get("agency") or "").lower()
    buyer = classify_buyer_type(row)
    if level == "FEDERAL" or "sam.gov" in sid or "dla" in sid or "dibbs" in sid:
        if "dla" in sid or "dibbs" in sid or "dla" in agency:
            return "DLA"
        return "FEDERAL"
    if buyer == "COOPERATIVE" or level == "COOPERATIVE":
        return "COOPERATIVE"
    if buyer == "UNIVERSITY" or buyer == "SCHOOL_DISTRICT":
        return "EDUCATION"
    if buyer == "UTILITY":
        return "UTILITY"
    if buyer in {"TRANSIT", "AIRPORT"}:
        return "TRANSIT_AUTHORITY"
    if level == "STATE" or buyer == "STATE_AGENCY":
        return "STATE"
    if level == "LOCAL" or buyer in {"CITY", "COUNTY", "MULTI_AGENCY_NETWORK"}:
        return "LOCAL"
    if "bidnet" in sid:
        return "LOCAL"
    return level or "OTHER"


def contribution_reports(rows: list[dict[str, Any]], *, stage3_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Source-family / platform / buyer-type / state contribution telemetry."""
    stage3_rows = stage3_rows or []
    s3_ids = {
        str(r.get("notice_id") or r.get("solicitation_id") or r.get("external_id") or id(r))
        for r in stage3_rows
    }

    def _is_commercial(r: dict[str, Any]) -> bool:
        lane = r.get("acquisition_lane") or ""
        return lane in {
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        }

    def _is_specialty(r: dict[str, Any]) -> bool:
        return (r.get("acquisition_lane") or "") in {
            MILSPEC_SPECIALTY,
            SOURCE_APPROVAL_REQUIRED,
            SOLE_SOURCE_RESTRICTED,
        }

    family_c: Counter = Counter()
    family_comm: Counter = Counter()
    platform_stats: dict[str, Counter] = {}
    buyer_stats: dict[str, Counter] = {}
    state_stats: dict[str, Counter] = {}

    for r in rows:
        fam = source_family_bucket(r)
        family_c[fam] += 1
        if _is_commercial(r):
            family_comm[fam] += 1
        plat = str(r.get("platform_family") or r.get("adapter_family") or r.get("discovery_platform") or "unknown")
        ps = platform_stats.setdefault(plat, Counter())
        ps["raw"] += 1
        if _is_commercial(r):
            ps["commercial"] += 1
        if _is_specialty(r):
            ps["specialty"] += 1
        key = str(r.get("notice_id") or r.get("solicitation_id") or r.get("external_id") or "")
        if key and key in s3_ids:
            ps["stage3"] += 1
            if (r.get("acquisition_lane") or "") == QUOTE_REQUIRED_COMMERCIAL:
                ps["quote_required"] += 1

        bt = classify_buyer_type(r)
        bs = buyer_stats.setdefault(bt, Counter())
        bs["raw"] += 1
        if _is_commercial(r):
            bs["commercial"] += 1

        st = str(r.get("state_code") or r.get("state") or "").upper() or "UNK"
        ss = state_stats.setdefault(st, Counter())
        ss["raw"] += 1
        if _is_commercial(r):
            ss["commercial"] += 1

    for r in stage3_rows:
        fam = source_family_bucket(r)
        # stage3 counts tracked via platform; also bump family stage3 via separate pass
        st = str(r.get("state_code") or r.get("state") or "").upper() or "UNK"
        state_stats.setdefault(st, Counter())["stage3"] += 1
        bt = classify_buyer_type(r)
        buyer_stats.setdefault(bt, Counter())["stage3"] += 1

    return {
        "source_family": {
            k: {"raw": family_c[k], "commercial": family_comm[k]} for k in sorted(family_c.keys())
        },
        "platforms": {k: dict(v) for k, v in sorted(platform_stats.items())},
        "buyer_types": {k: dict(v) for k, v in sorted(buyer_stats.items())},
        "states": {k: dict(v) for k, v in sorted(state_stats.items()) if v.get("raw", 0) > 0},
    }


def easy_registration_not_rejection(access_label: str | None) -> bool:
    """REGISTER_BEFORE_BID must not become INELIGIBLE."""
    lab = str(access_label or "").upper()
    return lab in {"YES", "CONDITIONAL", REGISTER_BEFORE_BID, "REGISTER_BEFORE_BID"}


assert STAGE3_NO_ROW_CAP is True
