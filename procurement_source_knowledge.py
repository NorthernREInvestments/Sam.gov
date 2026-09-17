"""ProcurementSourceKnowledgeBase — HOW TO FIND information, not bulk source mirrors.

Persists navigation recipes and portal quirks. Does NOT store entire SAM/USAspending/
state databases or solicitation archives.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from application_clock import now_utc
from persistence_policy import KIND_BENCHMARK_ANSWER_KEY, KIND_SOURCE_RECIPE, persistence_decision

# Staleness
STALE_CURRENT = "CURRENT"
STALE_AGING = "AGING"
STALE_STALE = "STALE"
STALE_UNKNOWN = "UNKNOWN"

# Default TTL days by knowledge class
_TTL_DAYS = {
    "portal_recipe": 180,
    "portal_quirk": 365,
    "financier_criteria": 30,
    "supplier_authorization": 90,
    "buyer_portal": 180,
}


def _utc() -> str:
    return now_utc().isoformat()


def assess_staleness(observed_at: str | None, *, knowledge_class: str = "portal_recipe") -> str:
    if not observed_at:
        return STALE_UNKNOWN
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return STALE_UNKNOWN
    age = now_utc() - dt.astimezone(timezone.utc)
    ttl = _TTL_DAYS.get(knowledge_class, 90)
    if age <= timedelta(days=ttl * 0.6):
        return STALE_CURRENT
    if age <= timedelta(days=ttl):
        return STALE_AGING
    return STALE_STALE


def preserve_signed_query_string(url: str) -> str:
    """Iowa SciQuest/JAGGAER lesson: never strip ?X-Amz-* from signed S3 URLs."""
    if not url:
        return url
    # Identity — callers must not strip; this helper documents the rule
    return url


def signed_url_would_break_if_stripped(url: str) -> bool:
    return "X-Amz-" in url or "X-Amz-Signature" in url


def source_profile(
    *,
    source_id: str,
    source_name: str,
    jurisdiction: str | None = None,
    government_level: str | None = None,
    portal_family: str | None = None,
    base_url: str | None = None,
    search_url_pattern: str | None = None,
    public_search_supported: bool = True,
    solicitation_lookup_method: str | None = None,
    event_lookup_method: str | None = None,
    attachment_discovery_method: str | None = None,
    document_download_method: str | None = None,
    authentication_behavior: str = "PUBLIC",
    signed_url_behavior: str | None = None,
    query_string_requirements: str | None = None,
    pagination_behavior: str | None = None,
    robots_access_constraints: str | None = None,
    known_public_alternate_paths: list[str] | None = None,
    award_lookup_method: str | None = None,
    historical_lookup_method: str | None = None,
    amendment_lookup_method: str | None = None,
    qa_lookup_method: str | None = None,
    source_reliability: str = "MEDIUM",
    authoritativeness: str = "AUTHORITATIVE_WHEN_AGENCY_HOSTED",
    known_parser_adapter: str | None = None,
    notes: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed = observed_at or _utc()
    return {
        "kind": "ProcurementSourceProfile",
        "source_id": source_id,
        "source_name": source_name,
        "jurisdiction": jurisdiction,
        "government_level": government_level,
        "portal_family": portal_family,
        "base_url": base_url,
        "search_url_pattern": search_url_pattern,
        "public_search_supported": public_search_supported,
        "solicitation_lookup_method": solicitation_lookup_method,
        "event_lookup_method": event_lookup_method,
        "attachment_discovery_method": attachment_discovery_method,
        "document_download_method": document_download_method,
        "authentication_behavior": authentication_behavior,
        "signed_url_behavior": signed_url_behavior,
        "query_string_requirements": query_string_requirements,
        "pagination_behavior": pagination_behavior,
        "robots_access_constraints": robots_access_constraints,
        "known_public_alternate_paths": list(known_public_alternate_paths or []),
        "award_lookup_method": award_lookup_method,
        "historical_lookup_method": historical_lookup_method,
        "amendment_lookup_method": amendment_lookup_method,
        "qa_lookup_method": qa_lookup_method,
        "source_reliability": source_reliability,
        "authoritativeness": authoritativeness,
        "last_verified_at": observed,
        "observed_at": observed,
        "staleness": assess_staleness(observed, knowledge_class="portal_recipe"),
        "verification_evidence": [],
        "known_parser_adapter": known_parser_adapter,
        "notes": notes,
        "persistence": persistence_decision(KIND_SOURCE_RECIPE),
        # Hard rule: case-specific benchmark URLs are NOT recipes
        "benchmark_case_specific": False,
    }


def seed_builtin_source_profiles() -> list[dict[str, Any]]:
    """Reusable HOW-TO knowledge already learned — not bulk data."""
    return [
        source_profile(
            source_id="iowa_sciquest_jaggaer",
            source_name="Iowa IMPACS / SciQuest Public Events",
            jurisdiction="IA",
            government_level="state",
            portal_family="JAGGAER_SCIQUEST",
            base_url="https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
            search_url_pattern="https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
            public_search_supported=True,
            solicitation_lookup_method="search_public_event_list_by_solicitation_or_title",
            event_lookup_method="public_event_row_and_view_as_pdf",
            attachment_discovery_method=(
                "public_event_html → View as PDF / Sourcingevent/{id}-event.pdf; "
                "supplier portal may hold additional attachments"
            ),
            document_download_method="http_get_signed_s3_url_preserving_query_string",
            authentication_behavior="PUBLIC_EVENT_PDF; AUTH_REQUIRED_FOR_SOME_ATTACHMENTS_AND_BID_RESPONSE",
            signed_url_behavior="PRESERVE_X_AMZ_QUERY_STRING",
            query_string_requirements=(
                "CRITICAL: stripping ?X-Amz-* from S3 event PDF URLs produces false 403 AccessDenied. "
                "Always preserve the full signed URL from listing HTML."
            ),
            pagination_behavior="public_event_list_html_rows",
            known_public_alternate_paths=[
                "View as PDF link in PublicEvent listing",
            ],
            award_lookup_method="agency_award_posting_or_public_bid_results_when_published",
            historical_lookup_method="public_archives_if_available; do_not_mirror_bulk",
            amendment_lookup_method="event_row_amendment_links_or_updated_event_pdf",
            qa_lookup_method="event_attachments_or_supplier_portal",
            source_reliability="HIGH",
            authoritativeness="AUTHORITATIVE_AGENCY_PORTAL",
            known_parser_adapter="discovery.sciquest / discovery.live_fetchers.jaggaer",
            notes=(
                "Learned from live Iowa blade work: public event → View as PDF → signed S3 URLs. "
                "Parser must prefer href= capture so AWS signatures survive."
            ),
        ),
        source_profile(
            source_id="sam_gov",
            source_name="SAM.gov",
            jurisdiction="US",
            government_level="federal",
            portal_family="SAM_GOV",
            base_url="https://sam.gov",
            search_url_pattern="https://sam.gov/search/?index=opp",
            public_search_supported=True,
            solicitation_lookup_method="notice_id_or_solicitation_number_search",
            attachment_discovery_method="notice_attachments_when_public",
            document_download_method="direct_public_url_or_api_file_with_key",
            authentication_behavior="MIXED_PUBLIC_AND_AUTH",
            award_lookup_method="award_notice_when_posted",
            historical_lookup_method="historical_notices_via_public_pages; SAM_API_scarce",
            source_reliability="HIGH",
            authoritativeness="AUTHORITATIVE_FEDERAL",
            known_parser_adapter="sam_client (scarce API — last resort)",
            notes="SAM API is scarce late-stage verification — not routine discovery.",
        ),
        source_profile(
            source_id="usaspending",
            source_name="USAspending.gov",
            jurisdiction="US",
            government_level="federal",
            portal_family="USASPENDING",
            base_url="https://www.usaspending.gov",
            search_url_pattern="https://api.usaspending.gov/api/v2/search/spending_by_award/",
            public_search_supported=True,
            solicitation_lookup_method="keyword_or_award_id_search_not_full_solicitation_package",
            award_lookup_method="spending_by_award_api",
            historical_lookup_method="award_api_on_demand — do not mirror database locally",
            authentication_behavior="PUBLIC",
            source_reliability="HIGH",
            authoritativeness="AUTHORITATIVE_AWARD_AMOUNTS",
            known_parser_adapter="usaspending_client",
            notes="Award-centric. Does not replace solicitation package recovery.",
        ),
        source_profile(
            source_id="montana_sciquest",
            source_name="Montana eMACS / SciQuest Public",
            jurisdiction="MT",
            government_level="state",
            portal_family="JAGGAER_SCIQUEST",
            base_url="https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana",
            public_search_supported=True,
            signed_url_behavior="PRESERVE_X_AMZ_QUERY_STRING",
            query_string_requirements="Same SciQuest family — preserve signed query strings",
            known_parser_adapter="discovery.sciquest",
            authentication_behavior="PUBLIC_EVENT_PDF_LIKELY",
        ),
        source_profile(
            source_id="bidnet_public",
            source_name="BidNet Direct (public pages)",
            jurisdiction="MULTI",
            government_level="state_local",
            portal_family="BIDNET",
            base_url="https://www.bidnetdirect.com",
            public_search_supported=True,
            authentication_behavior="MIXED_PUBLIC_AND_AUTH",
            notes="Secondary discovery; verify against authoritative agency portal.",
            source_reliability="MEDIUM",
            authoritativeness="SECONDARY_UNLESS_AGENCY_HOSTED",
        ),
    ]


class ProcurementSourceKnowledgeBase:
    """In-memory + optional JSON store of source recipes (not bulk data)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._profiles: dict[str, dict[str, Any]] = {}
        self._search_learnings: list[dict[str, Any]] = []
        for p in seed_builtin_source_profiles():
            self._profiles[p["source_id"]] = p

    def get(self, source_id: str) -> dict[str, Any] | None:
        p = self._profiles.get(source_id)
        if not p:
            return None
        out = deepcopy(p)
        out["staleness"] = assess_staleness(out.get("observed_at"), knowledge_class="portal_recipe")
        return out

    def list_profiles(self) -> list[dict[str, Any]]:
        return [self.get(sid) for sid in sorted(self._profiles)]  # type: ignore[misc]

    def upsert(self, profile: dict[str, Any], *, allow_benchmark_specific: bool = False) -> dict[str, Any]:
        if profile.get("benchmark_case_specific") and not allow_benchmark_specific:
            raise ValueError("refusing to store benchmark-case-specific URL as generic source recipe")
        if profile.get("kind") == KIND_BENCHMARK_ANSWER_KEY:
            raise ValueError("benchmark answer key cannot enter source knowledge")
        # Reject hidden winning-outcome seeds
        for banned in ("winning_vendor", "winning_price", "award_amount_seed", "hidden_benchmark_url"):
            if banned in (profile.get("notes") or "") and "benchmark" in (profile.get("notes") or "").lower():
                raise ValueError(f"refusing benchmark answer-key field pattern: {banned}")
        sid = profile["source_id"]
        profile = deepcopy(profile)
        profile["last_verified_at"] = profile.get("last_verified_at") or _utc()
        profile["staleness"] = assess_staleness(profile.get("observed_at") or profile["last_verified_at"])
        self._profiles[sid] = profile
        return profile

    def record_search_learning(
        self,
        *,
        source_id: str,
        learning_type: str,
        pattern: str,
        success: bool,
        case_specific_url: bool = False,
    ) -> dict[str, Any] | None:
        """Learn reusable non-case-specific navigation — not benchmark cheat paths."""
        if case_specific_url:
            # May live on deal record only — not generic recipe
            return None
        rec = {
            "source_id": source_id,
            "learning_type": learning_type,
            "pattern": pattern,
            "success": success,
            "observed_at": _utc(),
            "reusable": True,
        }
        self._search_learnings.append(rec)
        # Attach successful portal quirks to profile notes lightly
        prof = self._profiles.get(source_id)
        if prof and success and learning_type in {"signed_url_preserve", "attachment_endpoint", "url_pattern"}:
            evidence = list(prof.get("verification_evidence") or [])
            evidence.append(rec)
            prof["verification_evidence"] = evidence[-20:]  # compact
            prof["last_verified_at"] = _utc()
        return rec

    def needs_reverification(self, source_id: str) -> bool:
        p = self.get(source_id)
        if not p:
            return True
        return p.get("staleness") in {STALE_STALE, STALE_UNKNOWN}

    def match_by_jurisdiction(self, jurisdiction: str | None, agency: str | None = None) -> list[dict[str, Any]]:
        out = []
        j = (jurisdiction or "").upper()
        agency_l = (agency or "").lower()
        for p in self.list_profiles():
            if not p:
                continue
            if j and p.get("jurisdiction") in {j, "US", "MULTI"}:
                out.append(p)
            elif agency_l and ("iowa" in agency_l or "dot" in agency_l) and p.get("source_id") == "iowa_sciquest_jaggaer":
                out.append(p)
        return out

    def save(self, path: Path | None = None) -> Path:
        dest = path or self.path
        if dest is None:
            raise ValueError("no path")
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "ProcurementSourceKnowledgeBase",
            "note": "HOW TO FIND — not a bulk procurement database",
            "profiles": self._profiles,
            "search_learnings": self._search_learnings[-100:],
            "saved_at": _utc(),
        }
        dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return dest

    def load(self, path: Path | None = None) -> None:
        src = path or self.path
        if src is None or not src.exists():
            return
        data = json.loads(src.read_text(encoding="utf-8"))
        for sid, p in (data.get("profiles") or {}).items():
            if p.get("benchmark_case_specific"):
                continue  # never load cheat recipes
            self._profiles[sid] = p
        self._search_learnings = list(data.get("search_learnings") or [])
