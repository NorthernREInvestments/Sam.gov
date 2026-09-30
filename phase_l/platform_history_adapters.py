"""Phase L.14 — separated discovery/history adapters for non-BidNet platforms."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from phase_l.auth_access import classify_access_mode
from phase_l.auth_history_recovery import run_auth_walled_history_recovery
from phase_l.bidnet_parked import is_bidnet_auth_parked
from phase_l.exact_history_recovery import grade_recovered_award
from phase_l.history_graphs import normalize_award_tabulation
from phase_l.nonbidnet_expansion import PARKED_ACCESS_DEPENDENCY, SOURCE_PRIORITY_ORDER, parked_access_dependency
from phase_l.platform_history import PLATFORM_HISTORY_ADAPTERS, detect_platform, run_buyer_pivot
from phase_l.public_artifact_recovery import run_public_artifact_recovery
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D

BUILD = "20260928-m3-phase-l14-nonbidnet-source-expansion"


def _utc() -> str:
    return now_utc().isoformat()


# Capability matrix defaults (refined by live telemetry in l14_rescue)
DEFAULT_CAPABILITIES: dict[str, dict[str, Any]] = {
    "OpenGov": {
        "discovery": "partial_agency_alternate",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "partial",
        "history": "buyer_pivot",
        "auth": "cdn_cloudflare_park_agency_alternate",
        "commercial_yield": "high_potential",
        "notes": "procurement.opengov.com often Cloudflare; prefer agency bid-listings / solicitations pages",
    },
    "IonWave": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "partial",
        "history": "buyer_pivot",
        "auth": "varies",
        "commercial_yield": "medium",
    },
    "PlanetBids": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "partial",
        "history": "buyer_pivot",
        "auth": "public_discovery_auth_docs",
        "commercial_yield": "high_potential",
    },
    "Bonfire": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "buyer_pivot",
        "bid_tabs": "buyer_pivot",
        "history": "buyer_pivot",
        "auth": "public_discovery_auth_history",
        "commercial_yield": "high_potential",
    },
    "DemandStar": {
        "discovery": "stub",
        "attachments": "unknown",
        "awards": "unknown",
        "bid_tabs": "unknown",
        "history": "buyer_pivot",
        "auth": "often_registration",
        "commercial_yield": "medium",
    },
    "Jaggaer/SciQuest": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "rare",
        "history": "buyer_pivot",
        "auth": "often_auth",
        "commercial_yield": "medium",
    },
    "Public Purchase": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "partial",
        "history": "partial",
        "auth": "often_public",
        "commercial_yield": "high_potential",
    },
    "DLA/DIBBS": {
        "discovery": "active",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "rare",
        "history": "nsn_history",
        "auth": "public_or_cac",
        "commercial_yield": "specialty_plus_milspec_open",
    },
    "cooperatives": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "schedules",
        "bid_tabs": "rare",
        "history": "contract_pricing",
        "auth": "public_docs",
        "commercial_yield": "medium",
    },
    "state_portals": {
        "discovery": "partial",
        "attachments": "partial",
        "awards": "partial",
        "bid_tabs": "partial",
        "history": "partial",
        "auth": "varies",
        "commercial_yield": "high_potential",
    },
    "BidNet": {
        "discovery": "degraded_anti_bot",
        "attachments": "parked",
        "awards": "parked",
        "bid_tabs": "parked",
        "history": "BIDNET_AUTH_HISTORY_PARKED",
        "auth": "free_registration",
        "commercial_yield": "parked_this_phase",
    },
}


class PlatformHistoryAdapter:
    """Base: history only — discovery stays in live_fetchers."""

    platform = "generic"

    def research_history(
        self,
        row: dict[str, Any],
        *,
        commercial: dict[str, Any] | None = None,
        authorize_live: bool = False,
        max_fetches: int = 2,
    ) -> dict[str, Any]:
        platform = detect_platform(row)
        if platform == "BidNet" and is_bidnet_auth_parked():
            return {
                "kind": "PlatformHistoryResult",
                "platform": "BidNet",
                "parked": True,
                "outcome": "BIDNET_AUTH_HISTORY_PARKED",
                "awards": [],
                "grade_after": GOV_VALUE_D,
                "note": "BidNet authenticated history parked — no engineering this phase",
            }

        attempts: list[dict[str, Any]] = []
        # 1) Public artifact recovery (non-BidNet or BidNet metadata only)
        art = run_public_artifact_recovery(
            row,
            commercial=commercial,
            authorize_live=authorize_live,
            max_queries=3 if authorize_live else 2,
            max_fetches=max_fetches if authorize_live else 0,
            platform_blocked=True,
        )
        attempts.append({"step": "public_artifact", "outcome": art.get("outcome"), "arts": art.get("artifacts_found")})

        best_grade = art.get("grade_after") or GOV_VALUE_D
        best_gov = art.get("gov")
        awards = list(art.get("awards") or [])
        vendor_intel = art.get("vendor_intel")
        competition = art.get("competition")
        outcome = art.get("outcome")

        # 2) Buyer pivot if still D
        if best_grade == GOV_VALUE_D:
            pivot = run_buyer_pivot(
                row, commercial=commercial, authorize_live=authorize_live, max_fetches=max_fetches
            )
            attempts.append({"step": "buyer_pivot", "awards": len(pivot.get("awards") or []), "auth": pivot.get("auth_class")})
            for a in pivot.get("awards") or []:
                aw = normalize_award_tabulation({**a, "source": "buyer_pivot", "item": row.get("title")})
                graded = grade_recovered_award(aw, row=row, commercial=commercial or {})
                if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    awards.append(aw)
                    best_grade, best_gov = graded["grade"], graded["gov"]
                    outcome = "BUYER_HISTORY_RECOVERED"

        # 3) Auth-walled buyer public paths (L.12) — never BidNet login
        if best_grade == GOV_VALUE_D and platform != "BidNet":
            auth = run_auth_walled_history_recovery(
                row,
                commercial=commercial,
                authorize_live=authorize_live,
                max_live_fetches=max_fetches,
                platform_blocked=True,
            )
            attempts.append({"step": "auth_walled_buyer_paths", "outcome": auth.get("outcome")})
            if auth.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                best_grade, best_gov = auth["grade_after"], auth.get("gov")
                awards.extend(auth.get("awards") or [])
                vendor_intel = auth.get("vendor_intel") or vendor_intel
                competition = auth.get("competition") or competition
                outcome = auth.get("outcome")

        parked = None
        if best_grade == GOV_VALUE_D and art.get("registration_opportunity"):
            parked = parked_access_dependency(
                platform=platform,
                opportunities_affected=1,
                unlock_method=str((art.get("registration_opportunity") or {}).get("account_type") or "registration"),
            )

        return {
            "kind": "PlatformHistoryResult",
            "build": BUILD,
            "platform": platform,
            "outcome": outcome,
            "grade_after": best_grade,
            "gov": best_gov,
            "awards": awards[:5],
            "awards_found": len(awards),
            "vendor_intel": vendor_intel,
            "competition": competition,
            "attempts": attempts,
            "parked_dependency": parked,
            "timestamp": _utc(),
        }


class OpenGovHistoryAdapter(PlatformHistoryAdapter):
    platform = "OpenGov"


class IonWaveHistoryAdapter(PlatformHistoryAdapter):
    platform = "IonWave"


class PlanetBidsHistoryAdapter(PlatformHistoryAdapter):
    platform = "PlanetBids"


class BonfireHistoryAdapter(PlatformHistoryAdapter):
    platform = "Bonfire"


class DemandStarHistoryAdapter(PlatformHistoryAdapter):
    platform = "DemandStar"


class JaggaerHistoryAdapter(PlatformHistoryAdapter):
    platform = "Jaggaer/SciQuest"


class PublicPurchaseHistoryAdapter(PlatformHistoryAdapter):
    platform = "Public Purchase"


class DlaDibbsHistoryAdapter(PlatformHistoryAdapter):
    platform = "DLA/DIBBS"


class CooperativeHistoryAdapter(PlatformHistoryAdapter):
    platform = "cooperatives"


HISTORY_ADAPTERS: dict[str, PlatformHistoryAdapter] = {
    "OpenGov": OpenGovHistoryAdapter(),
    "IonWave": IonWaveHistoryAdapter(),
    "PlanetBids": PlanetBidsHistoryAdapter(),
    "Bonfire": BonfireHistoryAdapter(),
    "DemandStar": DemandStarHistoryAdapter(),
    "Jaggaer/SciQuest": JaggaerHistoryAdapter(),
    "Public Purchase": PublicPurchaseHistoryAdapter(),
    "DLA/DIBBS": DlaDibbsHistoryAdapter(),
    "cooperatives": CooperativeHistoryAdapter(),
    "state_portals": PlatformHistoryAdapter(),
}


def get_history_adapter(platform: str) -> PlatformHistoryAdapter:
    if platform == "BidNet":
        return PlatformHistoryAdapter()  # parked path inside
    return HISTORY_ADAPTERS.get(platform) or PlatformHistoryAdapter()


def run_platform_history(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    authorize_live: bool = False,
    max_fetches: int = 2,
) -> dict[str, Any]:
    platform = detect_platform(row)
    if "bidnet" in str(row.get("original_solicitation_url") or "").lower():
        platform = "BidNet"
    return get_history_adapter(platform).research_history(
        row, commercial=commercial, authorize_live=authorize_live, max_fetches=max_fetches
    )


def source_capability_matrix(telemetry: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """| Platform | Discovery | Attachments | Awards | Bid Tabs | History | Auth | Commercial Yield |"""
    telemetry = telemetry or {}
    rows = []
    platforms = list(SOURCE_PRIORITY_ORDER) + ["BidNet", "SAM"]
    seen = set()
    for p in platforms:
        if p in seen:
            continue
        seen.add(p)
        base = dict(DEFAULT_CAPABILITIES.get(p) or {})
        meta = PLATFORM_HISTORY_ADAPTERS.get(p) or {}
        tele = telemetry.get(p) or {}
        rows.append(
            {
                "platform": p,
                "discovery": tele.get("discovery") or base.get("discovery") or meta.get("status"),
                "attachments": tele.get("attachments") or base.get("attachments"),
                "awards": tele.get("awards") or base.get("awards"),
                "bid_tabs": tele.get("bid_tabs") or base.get("bid_tabs"),
                "history": tele.get("history") or base.get("history"),
                "auth": tele.get("auth") or base.get("auth") or (
                    "auth_often" if meta.get("auth_often_required") else "varies"
                ),
                "commercial_yield": tele.get("commercial_yield") or base.get("commercial_yield"),
                "live_raw": tele.get("raw", 0),
                "live_stage3": tele.get("stage3", 0),
                "gov_abc": tele.get("gov_abc", 0),
                "quote_targets": tele.get("quote_targets", 0),
            }
        )
    return rows


def classify_platform_auth(blob: str | None = None, *, platform: str | None = None) -> dict[str, Any]:
    info = classify_access_mode(blob, platform=platform)
    mode = info["access_mode"]
    taxonomy = {
        "PUBLIC_ANTI_BOT_BLOCKED": "anti-bot",
        "FREE_REGISTRATION_REQUIRED": "free registration",
        "VENDOR_ACCOUNT_REQUIRED": "vendor account",
        "BUYER_SPECIFIC_ACCOUNT_REQUIRED": "buyer-specific account",
        "PRIVATE_RESTRICTED": "private restricted",
        "NO_PUBLIC_HISTORY_FEATURE": "fully public discovery / no history feature",
        "CAPTCHA_PRESENT": "anti-bot",
        "UNKNOWN_ACCESS_MODE": "unknown",
    }
    return {
        "access_mode": mode,
        "taxonomy": taxonomy.get(mode, mode),
        "platform_history_blocked": info.get("platform_history_blocked"),
    }
