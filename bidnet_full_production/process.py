"""Process one BidNet opportunity: auth detail → buyer → package → canonical."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from bidnet_discovery.detail import (
    AUTH_REQUIRED,
    DETAIL_OK,
    NO_DETAIL_AVAILABLE,
    PARSER_FAILED,
    SESSION_LOST,
    SUPPLIER_REGISTRATION_REDIRECT,
    recover_detail,
)
from bidnet_full_production.auth_page import classify_auth_page
from bidnet_full_production.buyer_resolve import remember_buyer_mapping, resolve_buyer
from bidnet_full_production.canonical import build_canonical_record
from bidnet_full_production.models import (
    DETAIL_FAILED_RETRYABLE,
    DETAIL_FAILED_TERMINAL,
    DETAIL_LOCKED,
    DETAIL_PARTIAL,
    DETAIL_REGISTRATION_REDIRECT,
    PACKAGE_ACQUIRED_BIDNET,
    PACKAGE_ACQUIRED_OFFICIAL_SOURCE,
    PACKAGE_EXTERNAL_PORTAL_REQUIRED,
    PACKAGE_LOCKED_MEMBERSHIP,
    PACKAGE_NOT_POSTED,
    PACKAGE_REGISTRATION_REQUIRED,
    PACKAGE_RETRYABLE,
    PACKAGE_TERMINAL_OTHER,
)
from bidnet_recovery.parse_abstract import parse_bidnet_abstract
from package_recovery_sam_budget.quality import is_valid_package_evidence
from universe_pass.classify import classify_universe_opportunity


def _map_detail_status(auth_detail: dict[str, Any], parsed: dict[str, Any] | None) -> str:
    raw = str(auth_detail.get("detail_status") or "")
    parsed = parsed or {}
    if raw == DETAIL_OK:
        if parsed.get("locked_fields") and not (
            parsed.get("agency") and parsed.get("solicitation_number")
        ):
            return DETAIL_PARTIAL
        return DETAIL_OK
    if raw in {AUTH_REQUIRED} or parsed.get("auth_wall"):
        return DETAIL_LOCKED
    if raw == SUPPLIER_REGISTRATION_REDIRECT:
        return DETAIL_REGISTRATION_REDIRECT
    if raw in {SESSION_LOST, PARSER_FAILED, NO_DETAIL_AVAILABLE}:
        return DETAIL_FAILED_RETRYABLE
    if raw in {"404", "STALE_RESULT"}:
        return DETAIL_FAILED_TERMINAL
    if auth_detail.get("detail_opened"):
        return DETAIL_PARTIAL
    return DETAIL_FAILED_RETRYABLE


def _classify_package(
    *,
    row: dict[str, Any],
    parsed: dict[str, Any],
    auth_detail: dict[str, Any],
    official_hit: dict[str, Any] | None,
) -> tuple[str, str | None]:
    docs = list(row.get("attachments_metadata") or [])
    downloaded = sum(1 for d in docs if d.get("retrieval_status") == "DOWNLOADED" or d.get("local_path"))
    quality = is_valid_package_evidence(docs)

    if official_hit and official_hit.get("verified"):
        return PACKAGE_ACQUIRED_OFFICIAL_SOURCE, official_hit.get("route_family")

    if downloaded > 0 and quality.get("valid"):
        return PACKAGE_ACQUIRED_BIDNET, "bidnet_auth"

    if str(auth_detail.get("detail_status") or "") == SUPPLIER_REGISTRATION_REDIRECT:
        return PACKAGE_REGISTRATION_REQUIRED, None

    ext = parsed.get("agency_source_url") or row.get("original_posting_url")
    if ext and "bidnet" not in str(ext).lower():
        return PACKAGE_EXTERNAL_PORTAL_REQUIRED, None

    if parsed.get("auth_wall") or parsed.get("locked_fields"):
        return PACKAGE_LOCKED_MEMBERSHIP, None

    if not auth_detail.get("detail_opened"):
        return PACKAGE_RETRYABLE, None

    if str(auth_detail.get("detail_status") or "") in {"404", "STALE_RESULT"}:
        return PACKAGE_NOT_POSTED, None

    if quality.get("valid"):
        return PACKAGE_ACQUIRED_OFFICIAL_SOURCE, "url_inventory"

    return PACKAGE_RETRYABLE, None


def process_bidnet_opportunity(
    meta: dict[str, Any],
    l23_row: dict[str, Any],
    *,
    client: Any | None,
    store: dict[str, Any],
    skip_live_detail: bool = False,
) -> dict[str, Any]:
    row = deepcopy(l23_row)
    row.setdefault("title", meta.get("title"))
    row.setdefault("buyer", meta.get("buyer"))
    url = str(meta.get("authoritative_url") or row.get("authoritative_url") or row.get("detail_url") or "")
    if url:
        row["detail_url"] = url
        row["authoritative_url"] = url

    parsed: dict[str, Any] = {}
    auth_detail: dict[str, Any] = {"detail_status": NO_DETAIL_AVAILABLE, "detail_opened": False}

    if client and not skip_live_detail and url:
        row = recover_detail(client, row, retry=True)
        auth_detail = dict(row.get("auth_detail") or {})
        html = ""
        try:
            html = client.fetch_html(str(row.get("detail_url") or url), timeout_ms=45_000)
        except Exception:
            pass
        final_url = str(getattr(getattr(client, "_page", None), "url", None) or url)
        auth_detail["auth_page_state"] = classify_auth_page(
            final_url=final_url, html=html or "", session_expected=True
        )
        parsed = parse_bidnet_abstract(html, detail_url=final_url) if html else {}
        if row.get("agency"):
            parsed["agency"] = row.get("agency")
        if row.get("solicitation_number"):
            parsed["solicitation_number"] = row.get("solicitation_number")
    else:
        # Public abstract fallback (no Playwright) — still unlocks location/deadline/auth signals
        br = row.get("bidnet_recovery") or {}
        chase = br.get("free_package_chase") or {}
        if chase.get("documents"):
            row["attachments_metadata"] = chase["documents"]
        url = str(row.get("authoritative_url") or meta.get("authoritative_url") or "")
        if url:
            try:
                import httpx

                r = httpx.get(
                    url,
                    timeout=25.0,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if r.status_code < 400:
                    parsed = parse_bidnet_abstract(r.text or "", detail_url=str(r.url))
                    auth_detail["detail_opened"] = True
                    auth_detail["auth_page_state"] = classify_auth_page(
                        final_url=str(r.url), html=r.text or "", session_expected=False
                    )
            except Exception:
                pass

    if parsed and auth_detail.get("detail_opened"):
        if parsed.get("locked_fields") or parsed.get("auth_wall"):
            auth_detail.setdefault("mapped_detail_status", DETAIL_LOCKED if not parsed.get("agency") else DETAIL_PARTIAL)
        elif parsed.get("parse_ok"):
            auth_detail.setdefault("mapped_detail_status", DETAIL_PARTIAL if parsed.get("locked_fields") else DETAIL_OK)
    auth_detail["mapped_detail_status"] = auth_detail.get("mapped_detail_status") or _map_detail_status(auth_detail, parsed)
    buyer_info = resolve_buyer(row, parsed=parsed)

    official_hit = None
    if not skip_live_detail:
        try:
            from package_recovery_sam_budget.recover import recover_bidnet

            official_hit = recover_bidnet(meta, l23_row=row, store=store, force=False)
        except Exception:
            official_hit = None
    else:
        try:
            from package_recovery_sam_budget.recover import recover_bidnet

            official_hit = recover_bidnet(meta, l23_row=row, store=store, force=False)
            if not (official_hit or {}).get("verified"):
                official_hit = None
        except Exception:
            official_hit = None

    if official_hit and official_hit.get("verified"):
        row["official_package_source"] = official_hit.get("route_family") or official_hit.get("matched_source")
        if official_hit.get("documents"):
            row["attachments_metadata"] = official_hit.get("documents") or row.get("attachments_metadata")

    cls = classify_universe_opportunity(
        {
            "title": row.get("title") or meta.get("title"),
            "description": parsed.get("description") or row.get("description"),
            "buyer": buyer_info.get("normalized_buyer_name"),
        }
    )
    product_class = cls.get("class") or "UNKNOWN"

    pkg_state, portal = _classify_package(
        row=row, parsed=parsed, auth_detail=auth_detail, official_hit=official_hit
    )
    if portal:
        remember_buyer_mapping(buyer_info, portal=portal, success=pkg_state.startswith("PACKAGE_ACQUIRED"))

    url = str(row.get("authoritative_url") or meta.get("authoritative_url") or "")
    event_id = None
    m = re.search(r"/(\d{7,})(?:/abstract|\?|$)", url)
    if m:
        event_id = m.group(1)
    sol_resolved = bool(
        parsed.get("solicitation_number")
        or row.get("solicitation_number")
        or event_id
        or re.search(r"\b(RFP|RFQ|IFB|ITB|Bid)\s*[#:]?\s*[\w-]{4,}", str(row.get("title") or ""), re.I)
        or re.search(r"/open-bids/[^/]+/(\d{6,})", url, re.I)
    )

    canonical = build_canonical_record(
        meta=meta,
        l23_row=row,
        parsed=parsed,
        detail_stats=auth_detail,
        buyer_info=buyer_info,
        package_state=pkg_state,
        product_class=product_class,
    )

    # BidNet itself is the official source for acquired packages; also count
    # alternate portals (OpenGov/IonWave/buyer sites) and classifier portal routes.
    official_portal = bool(
        buyer_info.get("preferred_portal")
        or buyer_info.get("official_procurement_url")
        or (official_hit or {}).get("cross_portal_match")
        or (official_hit or {}).get("verified")
        or pkg_state in {PACKAGE_ACQUIRED_BIDNET, PACKAGE_ACQUIRED_OFFICIAL_SOURCE}
        or bool(portal)
    )

    return {
        "opportunity_id": meta.get("opportunity_id"),
        "canonical": canonical,
        "detail_status": auth_detail.get("mapped_detail_status"),
        "package_state": pkg_state,
        "buyer_confidence": buyer_info.get("buyer_identity_confidence"),
        "solicitation_resolved": sol_resolved,
        "official_portal_identified": official_portal,
        "package_verified": pkg_state in {PACKAGE_ACQUIRED_BIDNET, PACKAGE_ACQUIRED_OFFICIAL_SOURCE},
        "official_hit": official_hit,
        "portal_route": portal,
    }
