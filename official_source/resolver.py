"""OfficialSourceResolver — map BidNet listings to free public procurement sources."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from official_source.agency_profile import (
    PLATFORM_OPENGOV,
    PLATFORM_UNKNOWN,
    detect_platform_from_url,
    get_profile,
    upsert_profile,
)

log = logging.getLogger("govtracker.official_source.resolver")

RESULT_TYPES = {
    "OPENGOV",
    "IONWAVE",
    "PLANETBIDS_AGENCY",
    "PUBLIC_PURCHASE_AGENCY",
    "STATE_PORTAL",
    "COUNTY_PORTAL",
    "CITY_PORTAL",
    "UNIVERSITY_PORTAL",
    "SCHOOL_DISTRICT_PORTAL",
    "UTILITY_PORTAL",
    "TRANSIT_PORTAL",
    "AGENCY_NATIVE",
    "UNKNOWN",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _norm_sol(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _title_tokens(title: str) -> set[str]:
    stop = {"the", "and", "for", "of", "to", "a", "an", "in", "on", "with", "or"}
    return {t for t in re.findall(r"[a-z0-9]{3,}", _norm(title)) if t not in stop}


def _title_overlap(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


class OfficialSourceResolver:
    """Resolve BidNet opportunity → official free procurement source."""

    def __init__(self, *, timeout: float = 35.0) -> None:
        self.timeout = timeout

    def resolve(self, rec: dict[str, Any]) -> dict[str, Any]:
        buyer = str(rec.get("buyer") or rec.get("agency") or "").strip()
        agency = buyer
        sol = str(
            rec.get("solicitation_number")
            or rec.get("solicitation_event_id")
            or ((rec.get("row_ref") or {}) if isinstance(rec.get("row_ref"), dict) else {}).get(
                "solicitation_number"
            )
            or ""
        ).strip()
        title = str(rec.get("title") or "").strip()
        if not sol and title:
            m = re.search(
                r"\b(?:BID|ITB|IFB|RFP|RFQ|RFI|SOL)\s*#?\s*([A-Z0-9][A-Z0-9\-_/]{2,})\b",
                title,
                re.I,
            )
            if m:
                sol = m.group(1).strip()

        city = str(rec.get("city") or "").strip()
        state = str(rec.get("state") or rec.get("location_state") or "").strip()
        close = str(rec.get("deadline") or rec.get("close_date") or "")[:10]
        provenance = list(rec.get("provenance_urls") or [])
        for k in ("source_url", "detail_url", "url", "agency_source_url"):
            if rec.get(k):
                provenance.append(str(rec[k]))
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        if br.get("detail_url"):
            provenance.append(str(br["detail_url"]))

        evidence: list[str] = []
        profile = get_profile(agency) if agency else None
        # Only trust cached OpenGov / major platforms; ignore polluted soft profiles
        if profile and str(profile.get("platform_family") or "") in {
            PLATFORM_OPENGOV,
            "IONWAVE",
            "PLANETBIDS_AGENCY",
            "PUBLIC_PURCHASE_AGENCY",
        }:
            evidence.append(f"cached_profile:{profile.get('platform_family')}")
            # For OpenGov cache, still attempt live project match when we have sol/title
            if str(profile.get("platform_family")) == PLATFORM_OPENGOV and (sol or title):
                live = self._resolve_opengov(
                    agency=agency, sol=sol, title=title, state=state, close=close
                )
                if live.get("project_id") or live.get("official_source_resolved"):
                    return live
            return self._result(
                resolved_platform=str(profile.get("platform_family")),
                resolved_agency=agency,
                resolved_base_url=profile.get("base_url"),
                resolved_opportunity_url=profile.get("public_search_url"),
                confidence=float(profile.get("confidence") or 0.7),
                evidence=evidence + list(profile.get("evidence") or [])[:6],
                match_tier="cached_agency_profile",
                government_code=profile.get("government_code")
                or (
                    (profile.get("base_url") or "").rstrip("/").split("/")[-1]
                    if "opengov.com/portal/" in str(profile.get("base_url") or "")
                    else None
                ),
            )

        # Prefer OpenGov directory match before weak URL heuristics
        og = self._resolve_opengov(agency=agency, sol=sol, title=title, state=state, close=close)
        if og.get("resolved_platform") == PLATFORM_OPENGOV:
            return og

        # URL / provenance platform detection (non-BidNet only)
        for url in provenance:
            plat = detect_platform_from_url(url)
            if plat == PLATFORM_UNKNOWN:
                continue
            evidence.append(f"url_platform:{plat}:{url[:120]}")
            # Cache only strong platform families, not soft native guesses
            if plat in {
                PLATFORM_OPENGOV,
                "IONWAVE",
                "PLANETBIDS_AGENCY",
                "PUBLIC_PURCHASE_AGENCY",
            }:
                upsert_profile(
                    agency_name=agency or url,
                    jurisdiction=state or None,
                    platform_family=plat,
                    base_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                    if urlparse(url).netloc
                    else url,
                    public_search_url=url,
                    working_route="provenance_url",
                    evidence=evidence,
                    confidence=0.75,
                )
            return self._result(
                resolved_platform=plat,
                resolved_agency=agency,
                resolved_base_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                resolved_opportunity_url=url,
                confidence=0.75,
                evidence=evidence,
                match_tier="provenance_url",
            )

        return self._result(
            resolved_platform=PLATFORM_UNKNOWN,
            resolved_agency=agency or None,
            resolved_base_url=None,
            resolved_opportunity_url=None,
            confidence=0.0,
            evidence=evidence or ["unresolved"],
            match_tier="unresolved",
        )

    def _resolve_opengov(
        self,
        *,
        agency: str,
        sol: str,
        title: str,
        state: str,
        close: str,
    ) -> dict[str, Any]:
        evidence: list[str] = []
        try:
            from opengov_discovery.government_directory import OpenGovGovernmentDirectory
            from opengov_discovery.public_data_client import OpenGovPublicDataClient
        except Exception as exc:
            return self._result(
                resolved_platform=PLATFORM_UNKNOWN,
                resolved_agency=agency,
                confidence=0.0,
                evidence=[f"opengov_import_error:{type(exc).__name__}"],
                match_tier="error",
            )

        code = None
        with OpenGovGovernmentDirectory() as directory:
            try:
                code = directory.resolve_code(
                    {"entity_name": agency, "state": state or None}
                )
            except Exception:
                code = None
            if not code and agency:
                # Soft: scan codes for name containment
                nn = _norm(agency)
                for c in list(directory.codes)[:800]:
                    ent = directory._by_code.get(c) or {}
                    en = _norm(str(ent.get("name") or ""))
                    if nn and en and (nn in en or en in nn) and len(nn) >= 5:
                        est = str(ent.get("state") or "").upper()
                        if state and est and state.upper() != est:
                            continue
                        code = c
                        evidence.append(f"name_soft_match:{c}")
                        break

        if not code:
            return self._result(
                resolved_platform=PLATFORM_UNKNOWN,
                resolved_agency=agency,
                confidence=0.0,
                evidence=evidence + ["no_opengov_code"],
                match_tier="unresolved",
            )

        base = f"https://procurement.opengov.com/portal/{code}"
        evidence.append(f"opengov_code:{code}")
        best: dict[str, Any] | None = None
        best_score = 0.0
        match_tier = "buyer_portal_only"

        with OpenGovPublicDataClient() as client:
            # Include recently closed when searching by solicitation #
            fr = client.fetch_project_public(
                code,
                page_size=50,
                max_pages=6 if sol else 3,
                open_only=not bool(sol),
            )
            if not fr.get("ok"):
                upsert_profile(
                    agency_name=agency or code,
                    jurisdiction=state or None,
                    platform_family=PLATFORM_OPENGOV,
                    base_url=base,
                    public_search_url=base,
                    working_route="POST government/{code}/project/public",
                    evidence=evidence,
                    confidence=0.55,
                )
                return self._result(
                    resolved_platform=PLATFORM_OPENGOV,
                    resolved_agency=agency,
                    resolved_base_url=base,
                    resolved_opportunity_url=base,
                    confidence=0.55,
                    evidence=evidence + [f"list_fetch:{fr.get('error') or 'fail'}"],
                    match_tier="opengov_agency_only",
                    government_code=code,
                )

            sol_n = _norm_sol(sol)
            for row in fr.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                row_sol = _norm_sol(
                    row.get("financialId")
                    or row.get("solicitationNumber")
                    or row.get("projectNumber")
                    or ""
                )
                row_title = str(row.get("title") or "")
                score = 0.0
                tier = None
                if sol_n and row_sol and sol_n == row_sol:
                    score = 0.99
                    tier = "exact_solicitation_number"
                elif sol_n and row_sol and len(sol_n) >= 6 and (sol_n in row_sol or row_sol in sol_n):
                    score = 0.92
                    tier = "exact_buyer_solicitation"
                else:
                    tov = _title_overlap(title, row_title)
                    # Buyer already mapped to this OpenGov portal — allow lower title threshold
                    if tov >= 0.28:
                        score = 0.55 + tov
                        tier = "buyer_title_date"
                        rdl = str(row.get("proposalDeadline") or "")[:10]
                        if close and rdl and close == rdl:
                            score = min(0.95, score + 0.15)
                            tier = "title_place_close_date"
                        # Title-only without sol still needs stronger overlap
                        if not sol_n and tov < 0.40:
                            continue
                        if score < 0.55:
                            continue
                    else:
                        continue
                if score > best_score:
                    best_score = score
                    best = row
                    match_tier = tier or match_tier

        opp_url = base
        if best and best.get("id"):
            opp_url = f"{base}/projects/{best['id']}"
            evidence.append(f"project_id:{best['id']}")
            evidence.append(f"match_score:{best_score:.2f}")

        conf = best_score if best else 0.6
        upsert_profile(
            agency_name=agency or code,
            jurisdiction=state or None,
            platform_family=PLATFORM_OPENGOV,
            base_url=base,
            public_search_url=opp_url,
            public_document_pattern="GET /api/v1/project/{id}",
            working_route="GET /api/v1/project/{id}",
            government_code=code,
            evidence=evidence,
            confidence=conf,
        )
        # stash government_code on profile via base_url parse
        return self._result(
            resolved_platform=PLATFORM_OPENGOV,
            resolved_agency=agency,
            resolved_base_url=base,
            resolved_opportunity_url=opp_url,
            confidence=conf,
            evidence=evidence,
            match_tier=match_tier,
            government_code=code,
            project_id=str(best["id"]) if best and best.get("id") else None,
            project_title=(best or {}).get("title"),
            project_financial_id=(best or {}).get("financialId"),
        )

    def _result(
        self,
        *,
        resolved_platform: str,
        resolved_agency: str | None = None,
        resolved_base_url: str | None = None,
        resolved_opportunity_url: str | None = None,
        confidence: float = 0.0,
        evidence: list[str] | None = None,
        match_tier: str | None = None,
        government_code: str | None = None,
        project_id: str | None = None,
        project_title: str | None = None,
        project_financial_id: str | None = None,
    ) -> dict[str, Any]:
        plat = resolved_platform if resolved_platform in RESULT_TYPES else PLATFORM_UNKNOWN
        return {
            "resolved_platform": plat,
            "resolved_agency": resolved_agency,
            "resolved_base_url": resolved_base_url,
            "resolved_opportunity_url": resolved_opportunity_url,
            "confidence": round(float(confidence or 0), 3),
            "evidence": evidence or [],
            "match_tier": match_tier,
            "government_code": government_code,
            "project_id": project_id,
            "project_title": project_title,
            "project_financial_id": project_financial_id,
            "official_source_resolved": plat != PLATFORM_UNKNOWN and float(confidence or 0) >= 0.5,
            "retrieved_at": now_utc().isoformat(),
        }
