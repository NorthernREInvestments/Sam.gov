"""BuyerHistoryProfile — durable buyer-specific history source intelligence (L.19)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.buyer_history_paths import (
    buyer_key,
    discover_buyer_path_urls,
    empty_buyer_history_path,
    get_buyer_history_path,
    record_successful_recovery,
    upsert_buyer_history_path,
)

BUILD = "20260928-m3-phase-l19-buyer-specific-history-recovery"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROFILES_PATH = DATA / "buyer_history_profiles.json"

# Match confidence (§8)
EXACT_SAME_BUY = "EXACT_SAME_BUY"
EXACT_PRODUCT = "EXACT_PRODUCT"
STRONG_EQUIVALENT = "STRONG_EQUIVALENT"
COMPARABLE_SPEC = "COMPARABLE_SPEC"
CATEGORY_ONLY = "CATEGORY_ONLY"
NO_MATCH = "NO_MATCH"

# Source failure states (§38)
HISTORY_SOURCE_FOUND = "HISTORY_SOURCE_FOUND"
HISTORY_SOURCE_NO_MATCH = "HISTORY_SOURCE_NO_MATCH"
HISTORY_SOURCE_EMPTY = "HISTORY_SOURCE_EMPTY"
HISTORY_SOURCE_AUTH_BLOCKED = "HISTORY_SOURCE_AUTH_BLOCKED"
HISTORY_SOURCE_ANTI_BOT = "HISTORY_SOURCE_ANTI_BOT"
HISTORY_SOURCE_PARSER_FAIL = "HISTORY_SOURCE_PARSER_FAIL"
NO_PUBLIC_HISTORY_SOURCE_FOUND = "NO_PUBLIC_HISTORY_SOURCE_FOUND"

PRIOR_GOVERNMENT_VENDOR = "PRIOR_GOVERNMENT_VENDOR"
HIGH_VALUE_RECURRING_BUYER = "HIGH_VALUE_RECURRING_BUYER"
RECURRING_BUY_SIGNAL = "RECURRING_BUY_SIGNAL"

# Buyer → preferred structured history source families
BUYER_HISTORY_AFFINITY: list[tuple[re.Pattern[str], list[str]]] = [
    (
        re.compile(
            r"nyc|new york city|environmental protection|housing authority|"
            r"citywide administrative|mayor.?s office|city university|m/?wbe",
            re.I,
        ),
        ["structured_socrata_nyc_discretionary_awards", "NY", "CITY"],
    ),
    (
        re.compile(r"los angeles|la county|harbor department|airports.? los angeles|ladwp", re.I),
        ["structured_socrata_la_county", "CA", "COUNTY"],
    ),
    (re.compile(r"chicago", re.I), ["structured_socrata_chicago_contracts", "IL", "CITY"]),
    (re.compile(r"austin", re.I), ["structured_socrata_austin_contracts", "TX", "CITY"]),
    (re.compile(r"king county", re.I), ["structured_socrata_king_county_contracts", "WA", "COUNTY"]),
    (re.compile(r"montgomery", re.I), ["structured_socrata_montgomery_contracts", "MD", "COUNTY"]),
    (re.compile(r"baton rouge|brla", re.I), ["structured_socrata_brla_po", "LA", "CITY"]),
    (re.compile(r"richmond", re.I), ["structured_socrata_richmond_contracts", "VA", "CITY"]),
]


def _utc() -> str:
    return now_utc().isoformat()


def _load_profiles() -> dict[str, Any]:
    if PROFILES_PATH.exists():
        try:
            return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"kind": "BuyerHistoryProfileRegistry", "build": BUILD, "profiles": {}}


def _save_profiles(data: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    data["build"] = BUILD
    data["updated_at"] = _utc()
    PROFILES_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def empty_buyer_history_profile(buyer: str) -> dict[str, Any]:
    path = empty_buyer_history_path(buyer)
    return {
        "kind": "BuyerHistoryProfile",
        "buyer_id": buyer_key(buyer),
        "buyer": buyer,
        "jurisdiction": None,
        "buyer_type": None,
        "procurement_portal": path.get("procurement_portal"),
        "official_history_sources": [],
        "award_archive": path.get("award_results_url"),
        "bid_tab_source": path.get("bid_tab_url_path"),
        "po_source": path.get("po_check_register_location"),
        "payment_source": path.get("finance_open_data_system"),
        "contract_register_source": None,
        "board_council_source": path.get("board_agenda_system"),
        "open_data_source": path.get("finance_open_data_system"),
        "recurring_buy_evidence": [],
        "history_source_health": {},
        "last_successful_recovery": path.get("last_successful_recovery"),
        "history_sources": [],  # separate from live_sources (§45)
        "live_sources": [],
        "queries_attempted": 0,
        "matches": 0,
        "exact_matches": 0,
        "gov_upgrades": 0,
        "competition_evidence": 0,
        "recurring_buy_signals": 0,
        "updated_at": _utc(),
    }


def get_buyer_history_profile(buyer: str) -> dict[str, Any]:
    data = _load_profiles()
    key = buyer_key(buyer)
    existing = (data.get("profiles") or {}).get(key)
    if existing:
        return existing
    # Bootstrap from L.12 path memory
    path = get_buyer_history_path(buyer)
    prof = empty_buyer_history_profile(buyer)
    for k in (
        "procurement_portal",
        "award_results_url",
        "bid_tab_url_path",
        "po_check_register_location",
        "board_agenda_system",
        "finance_open_data_system",
        "last_successful_recovery",
    ):
        if path.get(k):
            if k == "award_results_url":
                prof["award_archive"] = path[k]
            elif k == "bid_tab_url_path":
                prof["bid_tab_source"] = path[k]
            elif k == "po_check_register_location":
                prof["po_source"] = path[k]
            elif k == "board_agenda_system":
                prof["board_council_source"] = path[k]
            elif k == "finance_open_data_system":
                prof["open_data_source"] = path[k]
                prof["payment_source"] = path[k]
            else:
                prof[k] = path[k]
    for u in path.get("discovered_urls") or []:
        prof["official_history_sources"].append({"url": u, "type": "discovered"})
        prof["history_sources"].append({"url": u, "role": "history"})
    return prof


def save_buyer_history_profile(profile: dict[str, Any]) -> dict[str, Any]:
    data = _load_profiles()
    key = profile.get("buyer_id") or buyer_key(profile.get("buyer"))
    profile["buyer_id"] = key
    profile["updated_at"] = _utc()
    data.setdefault("profiles", {})[key] = profile
    _save_profiles(data)
    # Write-through to L.12 paths for reuse
    upsert_buyer_history_path(
        profile.get("buyer") or key,
        procurement_portal=profile.get("procurement_portal"),
        award_results_url=profile.get("award_archive"),
        bid_tab_url_path=profile.get("bid_tab_source"),
        po_check_register_location=profile.get("po_source"),
        board_agenda_system=profile.get("board_council_source"),
        finance_open_data_system=profile.get("open_data_source") or profile.get("payment_source"),
        discovered_urls=[s.get("url") for s in (profile.get("official_history_sources") or []) if s.get("url")][:30],
    )
    return profile


def affinity_for_buyer(buyer: str) -> dict[str, Any]:
    for pat, meta in BUYER_HISTORY_AFFINITY:
        if pat.search(buyer or ""):
            return {
                "preferred_source_id": meta[0],
                "state": meta[1],
                "buyer_type": meta[2],
            }
    return {}


def discover_and_profile_buyer(
    buyer: str,
    *,
    solicitation: str | None = None,
    model: str | None = None,
    jurisdiction: str | None = None,
    buyer_type: str | None = None,
) -> dict[str, Any]:
    """Discover official history sources and return updated BuyerHistoryProfile."""
    disc = discover_buyer_path_urls(buyer, solicitation=solicitation, model=model)
    prof = get_buyer_history_profile(buyer)
    aff = affinity_for_buyer(buyer)
    if aff:
        prof["jurisdiction"] = prof.get("jurisdiction") or aff.get("state")
        prof["buyer_type"] = prof.get("buyer_type") or aff.get("buyer_type")
        if aff.get("preferred_source_id"):
            sid = aff["preferred_source_id"]
            if not any(s.get("source_id") == sid for s in (prof.get("history_sources") or [])):
                prof.setdefault("history_sources", []).append(
                    {"source_id": sid, "role": "structured_open_data", "preferred": True}
                )
                prof.setdefault("official_history_sources", []).append(
                    {"source_id": sid, "type": "structured_open_data"}
                )
    if jurisdiction:
        prof["jurisdiction"] = jurisdiction
    if buyer_type:
        prof["buyer_type"] = buyer_type
    for u in disc.get("urls") or []:
        if not any(s.get("url") == u for s in (prof.get("official_history_sources") or [])):
            prof.setdefault("official_history_sources", []).append({"url": u, "type": "official_seed"})
            prof.setdefault("history_sources", []).append({"url": u, "role": "history"})
    bases = [u for u in (disc.get("urls") or []) if u.rstrip("/").count("/") <= 3]
    if bases and not prof.get("procurement_portal"):
        prof["procurement_portal"] = bases[0]
    prof["queries_attempted"] = int(prof.get("queries_attempted") or 0) + 1
    if prof.get("official_history_sources"):
        prof["history_source_health"]["discovery"] = HISTORY_SOURCE_FOUND
    else:
        prof["history_source_health"]["discovery"] = NO_PUBLIC_HISTORY_SOURCE_FOUND
    return save_buyer_history_profile(prof)


def classify_match_confidence(
    *,
    live_row: dict[str, Any],
    award: dict[str, Any],
    commercial: dict[str, Any] | None = None,
) -> str:
    """Match confidence — CATEGORY_ONLY must not upgrade Gov to A/B."""
    commercial = commercial or {}
    buyer_live = re.sub(r"\s+", " ", str(live_row.get("agency") or live_row.get("buyer") or "").upper())
    buyer_aw = re.sub(r"\s+", " ", str(award.get("buyer") or award.get("agency") or "").upper())
    same_buyer = bool(
        buyer_live
        and buyer_aw
        and (buyer_live[:20] in buyer_aw or buyer_aw[:20] in buyer_live or _buyer_family_match(buyer_live, buyer_aw))
    )
    sol_live = str(live_row.get("solicitation_number") or live_row.get("solicitation") or "").upper().strip()
    sol_aw = str(award.get("solicitation_id") or award.get("solicitation_number") or "").upper().strip()
    if same_buyer and sol_live and sol_aw and (sol_live in sol_aw or sol_aw in sol_live):
        return EXACT_SAME_BUY

    model = str(commercial.get("model") or "").upper()
    mpn = str(commercial.get("mpn") or commercial.get("sku") or "").upper()
    aw_model = str(award.get("model") or award.get("mpn") or "").upper()
    aw_item = str(award.get("item") or award.get("product") or award.get("description") or "").upper()
    title = str(live_row.get("title") or "").upper()

    if mpn and (mpn in aw_item or mpn == aw_model):
        return EXACT_PRODUCT if not same_buyer else EXACT_SAME_BUY
    if model and len(model) >= 3 and (model == aw_model or model in aw_item):
        return EXACT_PRODUCT if not same_buyer else EXACT_SAME_BUY

    # Strong brand+model tokens from title
    tokens = _product_tokens(title)
    aw_tokens = _product_tokens(aw_item)
    overlap = tokens & aw_tokens
    strong = {"FORKLIFT", "IPAD", "CHROMEBOX", "CATERPILLAR", "YAMAHA", "FORD", "CISCO", "RICOH", "HUDSON", "SCANSNAP"}
    if same_buyer and (overlap & strong):
        return STRONG_EQUIVALENT
    if overlap & strong and len(overlap) >= 2:
        return STRONG_EQUIVALENT
    if same_buyer and len(overlap) >= 2:
        return COMPARABLE_SPEC
    if len(overlap) >= 2:
        return COMPARABLE_SPEC
    if overlap:
        return CATEGORY_ONLY
    return NO_MATCH


def _buyer_family_match(a: str, b: str) -> bool:
    """NYC agencies share a city family; LA County agencies likewise."""
    nyc = ("NYC", "NEW YORK CITY", "ENVIRONMENTAL PROTECTION", "HOUSING AUTHORITY", "CITYWIDE ADMINISTRATIVE")
    la = ("LOS ANGELES", "LA COUNTY", "HARBOR DEPARTMENT", "AIRPORTS")
    for fam in (nyc, la):
        if any(x in a for x in fam) and any(x in b for x in fam):
            return True
    return False


def _product_tokens(text: str) -> set[str]:
    stop = {
        "THE", "AND", "FOR", "WITH", "FROM", "BID", "RFB", "IFB", "RFP", "CLOSING",
        "EXTENSION", "CORRECTION", "AMENDMENT", "VARIOUS", "SERVICES", "SERVICE",
    }
    toks = re.findall(r"[A-Z0-9][A-Z0-9\-]{2,}", (text or "").upper())
    return {t for t in toks if t not in stop and not t.isdigit()}


def note_prior_government_vendor(vendor: str | None, *, award: dict[str, Any]) -> dict[str, Any] | None:
    if not vendor:
        return None
    return {
        "kind": PRIOR_GOVERNMENT_VENDOR,
        "vendor": vendor,
        "not_acquisition_supplier": True,
        "award_source": award.get("source"),
        "award_date": award.get("award_date"),
        "note": "Competitor/incumbent intelligence only — separate from supplier evidence grades",
    }


def mark_recovery_success(
    buyer: str,
    *,
    evidence_type: str,
    source_url: str | None = None,
    match_confidence: str | None = None,
    gov_upgraded: bool = False,
) -> dict[str, Any]:
    record_successful_recovery(buyer, evidence_type=evidence_type, source_url=source_url)
    prof = get_buyer_history_profile(buyer)
    prof["last_successful_recovery"] = _utc()
    prof["matches"] = int(prof.get("matches") or 0) + 1
    if match_confidence in {EXACT_SAME_BUY, EXACT_PRODUCT}:
        prof["exact_matches"] = int(prof.get("exact_matches") or 0) + 1
    if gov_upgraded:
        prof["gov_upgrades"] = int(prof.get("gov_upgrades") or 0) + 1
    if evidence_type == "bid_tab":
        prof["bid_tab_source"] = source_url or prof.get("bid_tab_source")
    elif evidence_type == "purchase_order":
        prof["po_source"] = source_url or prof.get("po_source")
    elif evidence_type == "contract_register":
        prof["contract_register_source"] = source_url or prof.get("contract_register_source")
    elif evidence_type in {"board", "council"}:
        prof["board_council_source"] = source_url or prof.get("board_council_source")
    elif evidence_type == "award":
        prof["award_archive"] = source_url or prof.get("award_archive")
    return save_buyer_history_profile(prof)
