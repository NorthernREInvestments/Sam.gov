"""Phase L.12 — BuyerHistoryPath persistence + buyer path discovery."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

from application_clock import now_utc

BUILD = "20260928-m3-phase-l12-auth-walled-history-recovery"
ROOT = Path(__file__).resolve().parents[1]
PATHS_FILE = ROOT / "data" / "phase_l12_buyer_history_paths.json"
PLATFORM_MEMORY_FILE = ROOT / "data" / "phase_l12_platform_history_memory.json"

PATH_KEYWORDS = (
    "award",
    "awarded",
    "bid tab",
    "bid results",
    "tabulation",
    "procurement results",
    "purchasing results",
    "contract award",
    "recommendation of award",
    "agenda",
    "council",
    "board",
    "purchase order",
    "check register",
    "vendor payments",
    "expenditures",
    "contracts",
)

AGENDA_SYSTEM_MARKERS = (
    "legistar",
    "granicus",
    "civicclerk",
    "civicplus",
    "boarddocs",
    "primegov",
    "novusagenda",
    "onbase",
    "iqm2",
    "municode",
)


def _utc() -> str:
    return now_utc().isoformat()


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"buyers": {}, "build": BUILD}
    return {"buyers": {}, "build": BUILD}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc()
    data["build"] = BUILD
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def buyer_key(buyer: str | None) -> str:
    return re.sub(r"\s+", " ", str(buyer or "UNKNOWN").strip().upper())[:120]


def empty_buyer_history_path(buyer: str) -> dict[str, Any]:
    return {
        "kind": "BuyerHistoryPath",
        "buyer": buyer,
        "procurement_portal": None,
        "award_results_url": None,
        "bid_tab_url_path": None,
        "board_agenda_system": None,
        "finance_open_data_system": None,
        "po_check_register_location": None,
        "auth_requirements": None,
        "document_naming_patterns": [],
        "last_successful_recovery": None,
        "supported_evidence_types": [],
        "search_strategy": [],
        "discovered_urls": [],
        "agenda_system_type": None,
        "updated_at": _utc(),
    }


def get_buyer_history_path(buyer: str) -> dict[str, Any]:
    data = _load(PATHS_FILE)
    key = buyer_key(buyer)
    return data.get("buyers", {}).get(key) or empty_buyer_history_path(buyer)


def upsert_buyer_history_path(buyer: str, **fields: Any) -> dict[str, Any]:
    data = _load(PATHS_FILE)
    buyers = data.setdefault("buyers", {})
    key = buyer_key(buyer)
    rec = buyers.setdefault(key, empty_buyer_history_path(buyer))
    rec["buyer"] = buyer
    for k, v in fields.items():
        if v is None:
            continue
        if k in {"discovered_urls", "document_naming_patterns", "supported_evidence_types", "search_strategy"}:
            existing = list(rec.get(k) or [])
            if isinstance(v, list):
                for item in v:
                    if item and item not in existing:
                        existing.append(item)
            elif v and v not in existing:
                existing.append(v)
            rec[k] = existing[-40:]
        else:
            rec[k] = v
    rec["updated_at"] = _utc()
    buyers[key] = rec
    _save(PATHS_FILE, data)
    return rec


def guess_buyer_site_seeds(buyer: str) -> list[str]:
    name = re.sub(r"[^a-z0-9\s]", "", (buyer or "").lower())
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) < 4:
        return []
    m = re.search(r"(?:city|town|village|county|borough)\s+of\s+([a-z0-9\s]+)", name)
    if m:
        place = m.group(1).strip().replace(" ", "")
        return [f"https://www.{place}.gov/", f"https://{place}.gov/"]
    tokens = [t for t in name.split() if t not in {"the", "of", "and", "dept", "department", "usd", "isd"}][:3]
    if not tokens:
        return []
    slug = "".join(tokens)[:24]
    return [f"https://www.{slug}.gov/", f"https://{slug}.gov/"]


def discover_buyer_path_urls(
    buyer: str,
    *,
    solicitation: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Deterministic URL seeds for buyer public history (no login)."""
    existing = get_buyer_history_path(buyer)
    seeds = list(existing.get("discovered_urls") or [])
    bases = guess_buyer_site_seeds(buyer)
    paths = (
        "bids",
        "purchasing",
        "procurement",
        "finance",
        "agendas",
        "council",
        "board",
        "contracts",
        "opendata",
        "open-data",
        "check-register",
        "vendor",
    )
    for b in bases:
        if b not in seeds:
            seeds.append(b)
        for p in paths:
            u = urljoin(b, p)
            if u not in seeds:
                seeds.append(u)
        q = quote_plus(" ".join(x for x in (solicitation, model, "bid tabulation award") if x))
        seeds.append(urljoin(b, f"search?q={q}"))

    # Persist discovery attempt
    upsert_buyer_history_path(
        buyer,
        discovered_urls=seeds[:30],
        search_strategy=["buyer_site_seeds", "path_keywords", "solicitation_query"],
        procurement_portal=bases[0] if bases else existing.get("procurement_portal"),
    )
    return {
        "buyer": buyer,
        "urls": seeds[:24],
        "keywords": list(PATH_KEYWORDS),
        "memory": get_buyer_history_path(buyer),
    }


def detect_agenda_system(url_or_html: str) -> str | None:
    t = (url_or_html or "").lower()
    for m in AGENDA_SYSTEM_MARKERS:
        if m in t:
            return m
    return None


def record_successful_recovery(
    buyer: str,
    *,
    evidence_type: str,
    source_url: str | None = None,
    auth_requirements: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "last_successful_recovery": _utc(),
        "supported_evidence_types": [evidence_type],
    }
    if source_url:
        fields["discovered_urls"] = [source_url]
        agenda = detect_agenda_system(source_url)
        if agenda:
            fields["agenda_system_type"] = agenda
            fields["board_agenda_system"] = source_url
        if "bid" in source_url.lower() and "tab" in source_url.lower():
            fields["bid_tab_url_path"] = source_url
        if any(x in source_url.lower() for x in ("award", "result", "contract")):
            fields["award_results_url"] = source_url
        if any(x in source_url.lower() for x in ("opendata", "open-data", "finance", "check")):
            fields["finance_open_data_system"] = source_url
            if "check" in source_url.lower() or "register" in source_url.lower():
                fields["po_check_register_location"] = source_url
    if auth_requirements:
        fields["auth_requirements"] = auth_requirements
    return upsert_buyer_history_path(buyer, **fields)


def update_platform_memory(
    platform: str,
    *,
    access_mode: str | None = None,
    anti_bot: bool | None = None,
    public_award_access: bool | None = None,
    buyer_pivot_success: bool | None = None,
    registration_leverage: int | None = None,
) -> dict[str, Any]:
    data = _load(PLATFORM_MEMORY_FILE)
    plats = data.setdefault("platforms", {})
    rec = plats.setdefault(
        platform,
        {
            "platform": platform,
            "public_award_access": None,
            "auth_requirements": None,
            "anti_bot_behavior": None,
            "alternate_buyer_pivot_attempts": 0,
            "alternate_buyer_pivot_successes": 0,
            "registration_leverage": 0,
        },
    )
    if access_mode:
        rec["auth_requirements"] = access_mode
    if anti_bot is not None:
        rec["anti_bot_behavior"] = anti_bot
    if public_award_access is not None:
        rec["public_award_access"] = public_award_access
    if buyer_pivot_success is not None:
        rec["alternate_buyer_pivot_attempts"] = int(rec.get("alternate_buyer_pivot_attempts") or 0) + 1
        if buyer_pivot_success:
            rec["alternate_buyer_pivot_successes"] = int(rec.get("alternate_buyer_pivot_successes") or 0) + 1
    if registration_leverage is not None:
        rec["registration_leverage"] = max(int(rec.get("registration_leverage") or 0), registration_leverage)
    rec["updated_at"] = _utc()
    plats[platform] = rec
    _save(PLATFORM_MEMORY_FILE, data)
    return rec


def all_buyer_history_paths() -> dict[str, Any]:
    return _load(PATHS_FILE)
