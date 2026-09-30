"""Phase L.11 — platform history adapters + buyer pivot (distinct from discovery)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from phase_l.history_graphs import normalize_award_tabulation, persist_buyer_retrieval

BUILD = "20260928-m3-phase-l11-exact-award-history-recovery"

PLATFORM_HISTORY_ADAPTERS: dict[str, dict[str, Any]] = {
    "BidNet": {
        "history_capabilities": ["awards", "bid_tabs", "historical_solicitations", "attachments"],
        "auth_often_required": True,
        "status": "degraded",
        "notes": "Live listings previously strong; current public open-bids often HTTP 202 empty / anti-bot — pivot to buyer for history",
        "weak_reason": "anti_bot",
        "registration": {
            "free": True,
            "vendor_account_required": True,
            "buyer_specific": False,
            "docs_after_free_reg": "sometimes",
            "awards_after_reg": "often",
            "setup_complexity": "medium",
            "registration_url": "https://www.bidnetdirect.com/",
        },
    },
    "OpenGov": {
        "history_capabilities": ["awards", "bid_tabs", "tabulations", "contract_documents"],
        "auth_often_required": True,
        "status": "partial",
        "weak_reason": "enumeration_incomplete",
        "registration": {
            "free": True,
            "vendor_account_required": True,
            "buyer_specific": True,
            "setup_complexity": "medium",
        },
    },
    "Bonfire": {
        "history_capabilities": ["awards", "bid_tabs", "historical_solicitations"],
        "auth_often_required": True,
        "status": "stub",
        "weak_reason": "auth_issue",
    },
    "IonWave": {
        "history_capabilities": ["awards", "bid_tabs", "historical_solicitations"],
        "auth_often_required": True,
        "status": "stub",
        "weak_reason": "discovery_logic_incomplete",
    },
    "PlanetBids": {
        "history_capabilities": ["awards", "bid_tabs", "tabulations"],
        "auth_often_required": True,
        "status": "partial",
        "weak_reason": "buyer_registry_incomplete",
    },
    "Jaggaer/SciQuest": {
        "history_capabilities": ["awards", "contract_documents", "historical_solicitations"],
        "auth_often_required": True,
        "status": "stub",
        "weak_reason": "auth_issue",
    },
    "Public Purchase": {
        "history_capabilities": ["awards", "bid_tabs", "historical_solicitations"],
        "auth_often_required": False,
        "status": "partial",
        "weak_reason": "enumeration_incomplete",
    },
    "DemandStar": {
        "history_capabilities": ["awards", "historical_solicitations"],
        "auth_often_required": True,
        "status": "stub",
        "weak_reason": "auth_issue",
    },
    "SAM": {
        "history_capabilities": ["historical_solicitations", "awards_via_usaspending"],
        "auth_often_required": False,
        "status": "active",
    },
    "DLA/DIBBS": {
        "history_capabilities": ["awards", "historical_solicitations", "nsn_history"],
        "auth_often_required": False,
        "status": "active",
        "weak_reason": "not_in_accessible_commercial_path",
    },
    "state_portals": {
        "history_capabilities": ["awards", "bid_tabs", "contract_documents", "term_contracts"],
        "auth_often_required": "varies",
        "status": "partial",
    },
    "cooperatives": {
        "history_capabilities": ["contract_documents", "price_schedules"],
        "auth_often_required": False,
        "status": "partial",
        "weak_reason": "not_retained_in_accessible_set",
    },
}

_BUYER_PATH_HINTS = (
    "procurement",
    "purchasing",
    "finance",
    "clerk",
    "council",
    "board",
    "agenda",
    "open-data",
    "opendata",
    "bids",
    "rfps",
)


def detect_platform(row: dict[str, Any]) -> str:
    raw = row.get("raw_ref") if isinstance(row.get("raw_ref"), dict) else {}
    sid = str(
        raw.get("source_id")
        or row.get("source_id")
        or row.get("source_portal")
        or row.get("portal")
        or ""
    ).lower()
    if sid.startswith("network_bidnet") or "bidnet" in sid:
        return "BidNet"
    if sid.startswith("fed_sam") or ("sam" in sid and "dibbs" not in sid):
        return "SAM"
    if "dibbs" in sid or "dla" in sid:
        return "DLA/DIBBS"
    if "opengov" in sid:
        return "OpenGov"
    if "bonfire" in sid:
        return "Bonfire"
    if "ionwave" in sid:
        return "IonWave"
    if "planetbids" in sid:
        return "PlanetBids"
    if "jaggaer" in sid or "sciquest" in sid:
        return "Jaggaer/SciQuest"
    if "publicpurchase" in sid or "public_purchase" in sid:
        return "Public Purchase"
    if "demandstar" in sid:
        return "DemandStar"
    if sid.startswith("agency_") or sid.startswith("state_"):
        return "state_portals"
    if any(x in sid for x in ("sourcewell", "omnia", "naspo", "coop", "hgac", "buyboard", "1gpa")):
        return "cooperatives"
    if sid in {"live_cooperative", "live_opengov", "live_bonfire", "live_ionwave", "live_planetbids",
               "live_demandstar", "live_jaggaer", "live_public_purchase", "live_dibbs"}:
        return {
            "live_cooperative": "cooperatives",
            "live_opengov": "OpenGov",
            "live_bonfire": "Bonfire",
            "live_ionwave": "IonWave",
            "live_planetbids": "PlanetBids",
            "live_demandstar": "DemandStar",
            "live_jaggaer": "Jaggaer/SciQuest",
            "live_public_purchase": "Public Purchase",
            "live_dibbs": "DLA/DIBBS",
        }[sid]

    url = " ".join(
        str(x or "")
        for x in (
            row.get("url"),
            row.get("source_url"),
            row.get("original_posting_url"),
            row.get("source"),
            row.get("portal"),
            row.get("source_portal"),
            row.get("source_family"),
            sid,
        )
    ).lower()
    mapping = [
        ("bidnet", "BidNet"),
        ("opengov", "OpenGov"),
        ("boston.gov/bid", "OpenGov"),
        ("bonfire", "Bonfire"),
        ("ionwave", "IonWave"),
        ("planetbids", "PlanetBids"),
        ("jaggaer", "Jaggaer/SciQuest"),
        ("sciquest", "Jaggaer/SciQuest"),
        ("publicpurchase", "Public Purchase"),
        ("demandstar", "DemandStar"),
        ("sourcewell", "cooperatives"),
        ("hgacbuy", "cooperatives"),
        ("omniapartners", "cooperatives"),
        ("naspovaluepoint", "cooperatives"),
        ("sam.gov", "SAM"),
        ("fed_sam", "SAM"),
        ("dibbs", "DLA/DIBBS"),
    ]
    for needle, name in mapping:
        if needle in url:
            return name
    src = str(row.get("source_family") or row.get("source") or "").strip()
    return src or "unknown"


def guess_buyer_domain(buyer: str) -> list[str]:
    """Deterministic buyer public-site seeds (no scraping of search engines required)."""
    name = re.sub(r"[^a-z0-9\s]", "", (buyer or "").lower())
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) < 4:
        return []
    # city of X / county of X patterns
    m = re.search(r"(?:city|town|village|county|borough)\s+of\s+([a-z0-9\s]+)", name)
    if m:
        place = m.group(1).strip().replace(" ", "")
        return [f"https://www.{place}.gov/", f"https://{place}.gov/"][:2]
    tokens = [t for t in name.split() if t not in {"the", "of", "and", "dept", "department"}][:3]
    if not tokens:
        return []
    slug = "".join(tokens)[:24]
    return [f"https://www.{slug}.gov/", f"https://{slug}.gov/"]


def buyer_public_record_urls(buyer: str, *, solicitation: str | None = None, model: str | None = None) -> list[str]:
    domains = guess_buyer_domain(buyer)
    urls: list[str] = []
    q = quote_plus(" ".join(x for x in (solicitation, model, "bid tabulation award") if x))
    for d in domains:
        urls.append(d)
        for path in ("bids", "purchasing", "procurement", "finance", "agendas"):
            urls.append(d.rstrip("/") + f"/{path}")
        # site search style
        urls.append(d.rstrip("/") + f"/search?q={q}")
    return urls[:12]


def fetch_platform_history(row: dict[str, Any], *, authorize_live: bool = False) -> dict[str, Any]:
    platform = detect_platform(row)
    adapter = PLATFORM_HISTORY_ADAPTERS.get(platform) or {
        "history_capabilities": [],
        "status": "missing",
        "auth_often_required": True,
    }
    buyer = str(row.get("agency") or row.get("buyer") or "")
    if buyer and platform != "unknown":
        persist_buyer_retrieval(buyer, platform=platform)

    awards: list[dict[str, Any]] = []
    if (
        row.get("award_amount")
        or row.get("historical_unit_price")
        or row.get("historical_award_unit_price")
        or row.get("prior_award")
    ):
        awards.append(
            normalize_award_tabulation(
                {
                    "buyer": buyer,
                    "solicitation_id": row.get("solicitation_id"),
                    "vendor": row.get("prior_awardee") or row.get("awardee") or row.get("historical_awardee"),
                    "unit_price": row.get("historical_award_unit_price")
                    or row.get("historical_unit_price")
                    or row.get("unit_price"),
                    "total": row.get("award_amount") or row.get("historical_award_price"),
                    "award_date": row.get("award_date"),
                    "source": f"platform:{platform}",
                    "platform": platform,
                    "item": row.get("title"),
                    "quantity": row.get("quantity"),
                }
            )
        )

    # BidNet: mark that award path usually requires pivot
    bidnet_pivot_required = platform == "BidNet" and adapter.get("auth_often_required")

    return {
        "kind": "PlatformHistoryResult",
        "build": BUILD,
        "platform": platform,
        "adapter_status": adapter.get("status"),
        "capabilities": adapter.get("history_capabilities") or [],
        "auth_often_required": adapter.get("auth_often_required"),
        "registration_intel": adapter.get("registration"),
        "live_fetch_attempted": bool(authorize_live),
        "awards_normalized": awards,
        "bidnet_pivot_required": bidnet_pivot_required,
        "gap": adapter.get("status") in {"stub", "missing", "partial"} and not awards,
        "weak_reason": adapter.get("weak_reason"),
        "notes": adapter.get("notes"),
    }


def run_buyer_pivot(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    authorize_live: bool = False,
    max_fetches: int = 3,
) -> dict[str, Any]:
    """When aggregator lacks public awards — pivot to buyer's own public records."""
    from phase_l.auth_access import PLATFORM_HISTORY_BLOCKED, classify_access_mode

    commercial = commercial or {}
    buyer = str(row.get("agency") or row.get("buyer") or "")
    platform = detect_platform(row)
    attempts: list[dict[str, Any]] = []
    awards: list[dict[str, Any]] = []
    auth_class = None
    blocked = False
    documents_missing = False
    buyer_domain_tried = False
    platform_state = None

    urls = buyer_public_record_urls(
        buyer,
        solicitation=str(row.get("solicitation_id") or row.get("notice_id") or ""),
        model=str(commercial.get("model") or ""),
    )
    attempts.append(
        {
            "step": "buyer_pivot_url_seeds",
            "buyer": buyer[:80],
            "platform": platform,
            "urls": urls[:6],
            "paths": list(_BUYER_PATH_HINTS),
        }
    )

    if platform == "BidNet":
        attempts.append(
            {
                "step": "bidnet_history_recovery",
                "actions": [
                    "prior_solicitations",
                    "award_result_tabs",
                    "attached_bid_tabs",
                    "award_documents",
                    "buyer_procurement_outside_bidnet",
                    "board_council_records",
                    "public_purchase_records",
                ],
                "public_award_likely": False,
                "pivot_to_buyer": True,
                "platform_history_blocked": True,
                "note": "BidNet anti-bot must not terminate exact-history recovery",
            }
        )
        access = classify_access_mode(
            "bidnet anti-bot", status="PLATFORM_BLOCKED", http_status=202, platform="BidNet"
        )
        auth_class = access["access_mode"]
        platform_state = PLATFORM_HISTORY_BLOCKED
        attempts.append(
            {
                "step": "bidnet_auth_boundary",
                "auth_class": auth_class,
                "platform_state": PLATFORM_HISTORY_BLOCKED,
                "history_not_available": False,
            }
        )

    if authorize_live and urls and max_fetches > 0:
        from phase_l.resilient_fetch import (
            FETCH_403,
            FETCH_BOT_BLOCKED,
            FETCH_OK,
            DomainCircuitBreaker,
            resilient_fetch,
        )
        from phase_l.pricing_sources import parse_council_award_text

        breaker = DomainCircuitBreaker()
        for url in urls[:max_fetches]:
            buyer_domain_tried = True
            if not breaker.allow(url):
                continue
            fr = resilient_fetch(url, breaker=breaker, retries=0, read_timeout=8.0)
            attempts.append({"step": "buyer_pivot_fetch", "url": url, "status": fr.status})
            if fr.status in {FETCH_403, FETCH_BOT_BLOCKED}:
                access = classify_access_mode(
                    fr.text[:500] if fr.text else "",
                    status=fr.status,
                    http_status=getattr(fr, "status_code", None),
                    platform=platform,
                )
                auth_class = access["access_mode"]
                platform_state = PLATFORM_HISTORY_BLOCKED if access.get("platform_history_blocked") else None
                blocked = True
                continue
            if fr.status != FETCH_OK or not fr.text:
                documents_missing = True
                continue
            parsed_records = parse_council_award_text(
                fr.text[:80000],
                search_id={
                    "model": commercial.get("model"),
                    "primary_mpn": commercial.get("mpn"),
                    "manufacturer": commercial.get("manufacturer"),
                },
                source_url=url,
            )
            for p in parsed_records or []:
                pd = p.to_dict() if hasattr(p, "to_dict") else (p if isinstance(p, dict) else {})
                if not pd:
                    # PriceEvidenceRecord attributes
                    pd = {
                        "vendor": getattr(p, "vendor", None),
                        "unit_price": getattr(p, "unit_price", None) or getattr(p, "price", None),
                        "total": getattr(p, "total", None) or getattr(p, "amount", None),
                        "quantity": getattr(p, "quantity", None),
                        "model": getattr(p, "model", None),
                        "date": getattr(p, "date", None),
                        "description": getattr(p, "description", None),
                    }
                awards.append(
                    normalize_award_tabulation(
                        {
                            "buyer": buyer,
                            "vendor": pd.get("vendor"),
                            "unit_price": pd.get("unit_price") or pd.get("price"),
                            "total": pd.get("total") or pd.get("amount"),
                            "quantity": pd.get("quantity"),
                            "model": commercial.get("model") or pd.get("model"),
                            "manufacturer": commercial.get("manufacturer"),
                            "award_date": pd.get("date"),
                            "source": "buyer_pivot_board",
                            "item": pd.get("description") or row.get("title"),
                            "solicitation_id": row.get("solicitation_id"),
                        }
                    )
                )
    else:
        attempts.append({"step": "buyer_pivot_live_skipped", "reason": "authorize_live_false_or_no_urls"})

    persist_buyer_retrieval(
        buyer,
        platform=platform,
        bid_tabs_url=urls[0] if urls else None,
        board_packets_url=next((u for u in urls if "agenda" in u or "board" in u), None),
    )

    return {
        "kind": "BuyerPivotResult",
        "build": BUILD,
        "attempts": attempts,
        "awards": awards,
        "auth_class": auth_class,
        "auth_required": bool(auth_class),
        "platform_state": platform_state,
        "blocked": blocked,
        "documents_missing": documents_missing and not awards,
        "buyer_domain_tried": buyer_domain_tried,
        "urls_seeded": urls,
    }


def weak_platform_audit() -> list[dict[str, Any]]:
    rows = []
    for name, meta in PLATFORM_HISTORY_ADAPTERS.items():
        if meta.get("status") in {"stub", "partial", "missing"} or meta.get("weak_reason"):
            rows.append(
                {
                    "platform": name,
                    "status": meta.get("status"),
                    "classification": meta.get("weak_reason") or "partial_support",
                    "auth_often_required": meta.get("auth_often_required"),
                    "capabilities": meta.get("history_capabilities"),
                    "registration": meta.get("registration"),
                }
            )
    return rows


def platform_history_inventory() -> dict[str, Any]:
    return {
        "kind": "PlatformHistoryInventory",
        "build": BUILD,
        "adapters": PLATFORM_HISTORY_ADAPTERS,
        "discovery_vs_history_separated": True,
        "weak_platform_audit": weak_platform_audit(),
    }
