"""Free package chase for BidNet-discovered opportunities.

BidNet list discovery is free. Member-only BidNet documents are NOT.
When BidNet walls docs, chase the same opportunity on free public sources.

Statuses:
  FREE_PACKAGE_FOUND
  PACKAGE_UNAVAILABLE_FREE   — conclusive; economics_dead allowed
  PACKAGE_RECOVERY_RETRYABLE — transient / incomplete; never economics_dead
  PACKAGE_MATCH_AMBIGUOUS    — conflicting matches; never economics_dead

No membership. No paid unlock. No registration wall bypass.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from bidnet_recovery.states import (
    FREE_PACKAGE_FOUND,
    PACKAGE_MATCH_AMBIGUOUS,
    PACKAGE_RECOVERY_RETRYABLE,
    PACKAGE_UNAVAILABLE_FREE,
    PARTIAL_FREE_PACKAGE_FOUND,
    VALID_FREE_PACKAGE_FOUND,
)

log = logging.getLogger("govtracker.bidnet_recovery.free_package_chase")

__all__ = [
    "chase_free_package",
    "fingerprint_for_chase",
    "should_skip_unchanged",
    "FREE_PACKAGE_FOUND",
    "VALID_FREE_PACKAGE_FOUND",
    "PARTIAL_FREE_PACKAGE_FOUND",
    "PACKAGE_UNAVAILABLE_FREE",
    "PACKAGE_RECOVERY_RETRYABLE",
    "PACKAGE_MATCH_AMBIGUOUS",
]

# Bump when chase logic gains new free routes so prior UNAVAILABLE rows revive.
CHASE_LOGIC_VERSION = "v3-official-source-docs"

_BIDNET_HOST = re.compile(r"bidnetdirect\.com|bidnet\.com", re.I)

_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_sol(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _title_overlap(a: str, b: str) -> float:
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def _state_hint(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> str | None:
    parsed = parsed or {}
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    for raw in (
        rec.get("state"),
        parsed.get("location"),
        br.get("location"),
        rec.get("location"),
        rec.get("buyer"),
        ((rec.get("raw_metadata") or {}).get("state") if isinstance(rec.get("raw_metadata"), dict) else None),
    ):
        s = str(raw or "").strip()
        if not s:
            continue
        if len(s) == 2 and s.isalpha():
            return s.upper()
        low = s.lower()
        if low in _STATE_NAMES:
            return _STATE_NAMES[low]
    return None


def _looks_like_bidnet_event_id(s: str) -> bool:
    """BidNet internal event ids are long digit strings — not public solicitation numbers."""
    t = re.sub(r"[^0-9]", "", s or "")
    return bool(t) and t == re.sub(r"\s+", "", s or "") and len(t) >= 10


def _sol_number(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> str | None:
    parsed = parsed or {}
    for raw in (
        parsed.get("solicitation_number"),
        rec.get("solicitation_number"),
        (rec.get("row_ref") or {}).get("solicitation_number") if isinstance(rec.get("row_ref"), dict) else None,
        (rec.get("bidnet_recovery") or {}).get("solicitation_number")
        if isinstance(rec.get("bidnet_recovery"), dict)
        else None,
        # solicitation_event_id last — often BidNet internal numeric id, not public ITB#
        rec.get("solicitation_event_id"),
    ):
        s = str(raw or "").strip()
        if not s or len(s) < 3:
            continue
        if _looks_like_bidnet_event_id(s):
            continue
        return s
    return None


def _entity_candidates(title: str, buyer: str, overview: str | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = re.sub(r"\s+", " ", (name or "").strip(" .,;:"))
        if len(n) < 4:
            return
        key = n.lower()
        if key in seen or key in _STATE_NAMES:
            return
        if key in {"united states", "local", "unknown", "statewide"}:
            return
        seen.add(key)
        out.append(n)

    # Prefer overview-derived agency (BidNet public AI overview often names issuer)
    blob = f"{overview or ''}\n{title or ''}"
    for m in re.finditer(
        r"\b([A-Z][A-Za-z0-9 .'-]{2,70}?"
        r"(?:Public Utility District|Irrigation District|School District|"
        r"Community College District|Fire District|Hospital District|"
        r"Water District|Utility District|Transportation Authority|"
        r"Housing Authority|Port Authority|Unified School District))\b",
        blob,
    ):
        add(m.group(1))
    for m in re.finditer(
        r"\b((?:City|County|Town|Township|Borough|Village|Parish|School District|USD|Airport|"
        r"University|College|Authority|District)\s+of\s+[A-Z][A-Za-z .'-]{2,40})",
        blob,
    ):
        add(m.group(1))
    for m in re.finditer(
        r"\b([A-Z][A-Za-z .'-]{2,40}\s+(?:City|County|Town|Township|Borough|Village|"
        r"Airport|University|College|Authority|School District))\b",
        blob,
    ):
        add(m.group(1))
    # "The X is soliciting" pattern
    for m in re.finditer(
        r"\b(?:The\s+)?([A-Z][A-Za-z0-9 .'-]{3,70}?)\s+is\s+soliciting\b",
        overview or "",
    ):
        add(m.group(1))
    add(buyer)
    t = title or ""
    m = re.match(r"^([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b", t)
    if m:
        add(m.group(1))
    return out[:10]


def _overview_text(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> str:
    parsed = parsed or {}
    for raw in (
        parsed.get("overview"),
        parsed.get("description"),
        rec.get("description"),
        (rec.get("row_ref") or {}).get("description") if isinstance(rec.get("row_ref"), dict) else None,
        (rec.get("bidnet_recovery") or {}).get("overview")
        if isinstance(rec.get("bidnet_recovery"), dict)
        else None,
    ):
        s = str(raw or "").strip()
        if len(s) >= 20:
            return s
    return ""


def refresh_public_overview(rec: dict[str, Any]) -> dict[str, Any]:
    """Re-fetch BidNet public abstract for overview/agency clues (no membership)."""
    from bidnet_recovery.recover import detail_url_for
    from bidnet_recovery.parse_abstract import parse_bidnet_abstract

    url = detail_url_for(rec)
    if not url:
        return {}
    try:
        import httpx

        r = httpx.get(
            url,
            timeout=25.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code >= 400:
            return {"error": f"HTTP_{r.status_code}"}
        return parse_bidnet_abstract(r.text or "", detail_url=url)
    except Exception as exc:
        return {"error": type(exc).__name__}


def fingerprint_for_chase(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> str:
    """Stable fingerprint of identity + free-source clues for skip/revival."""
    parsed = parsed or {}
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    parts = [
        CHASE_LOGIC_VERSION,
        str(rec.get("title") or ""),
        str(_sol_number(rec, parsed) or ""),
        str(rec.get("buyer") or parsed.get("agency") or ""),
        str(_state_hint(rec, parsed) or ""),
        str(rec.get("deadline") or br.get("deadline_raw") or parsed.get("deadline") or ""),
        "|".join(candidate_free_urls(rec, parsed)[:8]),
        str(br.get("agency_source_url") or parsed.get("agency_source_url") or ""),
        str(_overview_text(rec, parsed)[:160]),
    ]
    return hashlib.sha1("\n".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:20]


def should_skip_unchanged(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> bool:
    """Skip if prior conclusive chase fingerprint unchanged (still revivable on change)."""
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
    prior_fp = str(chase.get("fingerprint") or "")
    if not prior_fp:
        return False
    status = str(chase.get("status") or "")
    fp = fingerprint_for_chase(rec, parsed)
    if status in {
        PACKAGE_UNAVAILABLE_FREE,
        FREE_PACKAGE_FOUND,
        VALID_FREE_PACKAGE_FOUND,
        PARTIAL_FREE_PACKAGE_FOUND,
    }:
        return prior_fp == fp
    # Thin fast-fail already recorded — don't re-chase until identity clues change
    if status == PACKAGE_RECOVERY_RETRYABLE:
        reasons = chase.get("retry_reasons") or []
        note = str(chase.get("note") or "")
        if prior_fp == fp and (
            "INCOMPLETE_IDENTITY_FAST_FAIL" in reasons
            or "Incomplete buyer/solicitation" in note
        ):
            return True
    return False


def candidate_free_urls(rec: dict[str, Any], parsed: dict[str, Any] | None = None) -> list[str]:
    parsed = parsed or {}
    out: list[str] = []
    seen: set[str] = set()

    def add(u: Any) -> None:
        url = str(u or "").strip()
        if not url.startswith("http"):
            return
        if _BIDNET_HOST.search(url):
            return
        key = url.split("#")[0].rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        out.append(url)

    add(parsed.get("agency_source_url"))
    add(rec.get("original_posting_url"))
    add(rec.get("agency_source_url"))
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    add(rr.get("agency_source_url"))
    add(rr.get("original_posting_url"))
    meta = rec.get("raw_metadata") if isinstance(rec.get("raw_metadata"), dict) else {}
    add(meta.get("agency_source_url"))
    add(meta.get("original_posting_url"))
    for p in rec.get("source_provenance") or []:
        if isinstance(p, dict):
            add(p.get("url"))
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    add(br.get("agency_source_url"))
    return out


def _docs_from_portal_resolve(url: str, rec: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Run free public document resolvers. Returns (docs, error_code)."""
    row = {
        "detail_url": url,
        "source_url": url,
        "url": url,
        "title": rec.get("title"),
        "agency": rec.get("buyer") or rec.get("agency"),
        "solicitation_number": rec.get("solicitation_number") or rec.get("solicitation_event_id"),
    }
    docs: list[dict[str, Any]] = []
    try:
        from portal_document_resolver import classify_portal_family, resolve_portal_documents
        from portal_resolvers.generic import resolve_generic_documents

        family = classify_portal_family(row)
        if str(family).upper() == "BIDNET":
            res = resolve_generic_documents(row)
        else:
            try:
                res = resolve_portal_documents(row)
            except Exception:
                res = resolve_generic_documents(row)
        for d in res.get("documents") or []:
            if not isinstance(d, dict):
                continue
            u = d.get("download_url") or d.get("url") or d.get("document_url")
            if not u or _BIDNET_HOST.search(str(u)):
                continue
            docs.append(
                {
                    "document_type": d.get("document_type") or d.get("type") or "attachment",
                    "document_name": d.get("title") or d.get("document_name") or str(u).rsplit("/", 1)[-1],
                    "document_url": u,
                    "retrieval_status": "DOWNLOADED" if d.get("bytes_recovered") else "URL_DISCOVERED",
                    "byte_size": d.get("byte_size") or d.get("bytes"),
                    "free_chase": True,
                    "free_source_url": url,
                    "portal_family": res.get("family") or family,
                    "provenance": {"route": "agency_or_provenance_url", "source_url": url},
                }
            )
        return docs, None
    except Exception as exc:
        err = type(exc).__name__
        log.info("free portal resolve failed %s: %s", urlparse(url).netloc, err)
        try:
            import httpx
            from portal_resolvers.attachment_extract import extract_attachment_candidates

            r = httpx.get(
                url,
                timeout=25.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code in {429, 500, 502, 503, 504}:
                return [], f"HTTP_{r.status_code}"
            if r.status_code >= 400:
                return [], f"HTTP_{r.status_code}"
            for c in extract_attachment_candidates(r.text or "", base_url=str(r.url))[:20]:
                u = c.get("url") or c.get("document_url")
                if not u or _BIDNET_HOST.search(str(u)):
                    continue
                docs.append(
                    {
                        "document_type": c.get("document_type") or "attachment",
                        "document_name": c.get("name")
                        or c.get("document_name")
                        or str(u).rsplit("/", 1)[-1],
                        "document_url": u,
                        "retrieval_status": "URL_DISCOVERED",
                        "free_chase": True,
                        "free_source_url": url,
                        "portal_family": "GENERIC_HREF",
                        "provenance": {"route": "agency_or_provenance_url", "source_url": url},
                    }
                )
            return docs, None
        except Exception as exc2:
            name = type(exc2).__name__
            if "Timeout" in name:
                return [], "TIMEOUT"
            return [], name


def _opengov_codes_for_rec(
    rec: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
) -> list[str]:
    try:
        from opengov_discovery.government_directory import OpenGovGovernmentDirectory, _norm_name
    except Exception:
        return []

    buyer = str(
        (parsed or {}).get("agency")
        or rec.get("buyer")
        or rec.get("agency")
        or ""
    ).strip()
    title = str(rec.get("title") or "").strip()
    overview = _overview_text(rec, parsed)
    state = _state_hint(rec, parsed)
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    loc = str(
        (parsed or {}).get("location") or br.get("location") or rec.get("location") or ""
    ).strip()

    directory = OpenGovGovernmentDirectory()
    directory.ensure_loaded()
    codes: list[str] = []

    def add_code(c: str | None) -> None:
        if c and c not in codes:
            codes.append(c)

    entities = _entity_candidates(title, buyer, overview) + ([loc] if loc else [])
    for name in entities:
        add_code(directory.resolve_code({"entity_name": name, "state": state}))
        if state:
            add_code(directory.resolve_code({"entity_name": f"City of {name}", "state": state}))
            add_code(directory.resolve_code({"entity_name": f"County of {name}", "state": state}))

    tokens = [w for w in _norm(title).split() if len(w) >= 5]
    buyer_n = _norm_name(buyer) if buyer else ""
    # Prefer non-state-name buyers for fuzzy scoring
    scored: list[tuple[int, str]] = []
    for code, g in directory._by_code.items():
        if state and str(g.get("state") or "").upper() != state:
            continue
        gn = _norm_name(str(g.get("name") or ""))
        if not gn:
            continue
        score = 0
        if buyer_n and buyer_n not in _STATE_NAMES and buyer_n in gn:
            score += 6
        for tok in tokens:
            if tok in gn:
                score += 2
        if score >= 4:
            scored.append((score, code))
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _, code in scored[:4]:
        add_code(code)

    try:
        directory.close()
    except Exception:
        pass
    return codes[:6]


def _docs_from_opengov_project(
    client: Any,
    *,
    code: str,
    row: dict[str, Any],
    rec: dict[str, Any],
    agency: str | None,
    confidence: float,
) -> list[dict[str, Any]]:
    from opengov_discovery.parse import parse_opengov_json_payload

    docs: list[dict[str, Any]] = []
    parsed_rows = parse_opengov_json_payload({"count": 1, "rows": [row]}, list_url="", agency=agency)
    detail = (parsed_rows[0].get("detail_url") if parsed_rows else None) or (
        f"https://procurement.opengov.com/portal/{code}/projects/{row.get('id')}"
    )
    for d in (parsed_rows[0].get("document_links") if parsed_rows else None) or []:
        if isinstance(d, dict) and d.get("document_url"):
            dd = dict(d)
            dd["free_chase"] = True
            dd["free_source"] = "opengov_project_public"
            dd["opengov_code"] = code
            dd["match_confidence"] = confidence
            dd["provenance"] = {
                "route": "opengov_project_public",
                "opengov_code": code,
                "project_id": row.get("id"),
                "source_url": detail,
                "confidence": confidence,
            }
            docs.append(dd)
    try:
        pid = row.get("id")
        if pid:
            from opengov_discovery.public_document_client import (
                DOCUMENTS_FOUND_PUBLIC,
                OpenGovPublicDocumentClient,
            )

            with OpenGovPublicDocumentClient() as doc_client:
                fetched = doc_client.fetch_project_documents(
                    project_id=pid, government_code=code
                )
            if fetched.get("status") == DOCUMENTS_FOUND_PUBLIC:
                for item in fetched.get("documents") or []:
                    if not isinstance(item, dict) or not item.get("document_url"):
                        continue
                    dd = dict(item)
                    dd["retrieval_status"] = "URL_DISCOVERED"
                    dd["free_chase"] = True
                    dd["free_source"] = "opengov_public_document_client"
                    dd["match_confidence"] = confidence
                    dd.setdefault(
                        "provenance",
                        {
                            "route": "GET /api/v1/project/{id}",
                            "opengov_code": code,
                            "project_id": pid,
                            "confidence": confidence,
                        },
                    )
                    docs.append(dd)
    except Exception:
        pass
    if detail and not _BIDNET_HOST.search(str(detail)):
        more, _err = _docs_from_portal_resolve(detail, rec)
        for d in more:
            d["match_confidence"] = confidence
            d.setdefault("provenance", {})["opengov_code"] = code
        docs.extend(more)
    return docs


def _row_sol(row: dict[str, Any]) -> str:
    for k in (
        "solicitationNumber",
        "solicitation_number",
        "projectNumber",
        "project_number",
        "number",
        " bidNumber",
        "bid_number",
        " bidId",
    ):
        v = row.get(k.strip())
        if v:
            return _norm_sol(v)
    return ""


def _opengov_free_match(
    rec: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match against OpenGov public project/public. Returns docs + meta."""
    buyer = str(rec.get("buyer") or rec.get("agency") or "").strip()
    title = str(rec.get("title") or "").strip()
    sol = _norm_sol(_sol_number(rec, parsed))
    if len(title) < 8 and not sol:
        return {"docs": [], "error": None, "codes": [], "confidence": 0.0, "ambiguous": False}

    try:
        from opengov_discovery.public_data_client import OpenGovPublicDataClient
    except Exception as exc:
        return {"docs": [], "error": type(exc).__name__, "codes": [], "confidence": 0.0, "ambiguous": False}

    codes = _opengov_codes_for_rec(rec, parsed=parsed)
    if not codes:
        return {"docs": [], "error": "NO_OPENGOV_CODES", "codes": [], "confidence": 0.0, "ambiguous": False}

    docs: list[dict[str, Any]] = []
    best_conf = 0.0
    matched_id = None
    matched_url = None
    strong_hits = 0
    try:
        with OpenGovPublicDataClient() as client:
            for code in codes:
                fr = client.fetch_project_public(code, page_size=50, max_pages=4, open_only=True)
                if not fr.get("ok") and fr.get("error"):
                    return {
                        "docs": [],
                        "error": str(fr.get("error") or "OPENGOV_FETCH_ERROR"),
                        "codes": codes,
                        "confidence": 0.0,
                        "ambiguous": False,
                    }
                rows = fr.get("rows") or []
                candidates: list[tuple[float, dict[str, Any]]] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_sol = _row_sol(row)
                    score = 0.0
                    if sol and row_sol and sol == row_sol:
                        score = 0.98
                    elif sol and row_sol and (sol in row_sol or row_sol in sol):
                        score = 0.90
                    else:
                        tscore = _title_overlap(title, str(row.get("title") or ""))
                        # Title-only needs additional evidence
                        if tscore >= 0.55 and buyer and _norm(buyer)[:6] in _norm(str(row.get("department") or row.get("agency") or code)):
                            score = tscore
                        elif tscore >= 0.70:
                            score = tscore * 0.85  # title-only soft
                        else:
                            score = 0.0
                    if score >= 0.55:
                        candidates.append((score, row))
                candidates.sort(key=lambda x: -x[0])
                if len(candidates) >= 2 and abs(candidates[0][0] - candidates[1][0]) < 0.05:
                    strong_hits += len(candidates)
                    continue
                if not candidates:
                    continue
                score, best = candidates[0]
                if score < 0.55:
                    continue
                got = _docs_from_opengov_project(
                    client,
                    code=code,
                    row=best,
                    rec=rec,
                    agency=buyer or None,
                    confidence=score,
                )
                if got:
                    docs = got
                    best_conf = score
                    matched_id = str(best.get("id") or "")
                    matched_url = (
                        f"https://procurement.opengov.com/portal/{code}/projects/{best.get('id')}"
                    )
                    break
    except Exception as exc:
        return {
            "docs": [],
            "error": type(exc).__name__,
            "codes": codes,
            "confidence": 0.0,
            "ambiguous": False,
        }

    if strong_hits >= 2 and not docs:
        return {
            "docs": [],
            "error": None,
            "codes": codes,
            "confidence": 0.0,
            "ambiguous": True,
            "matched_source": "opengov_project_public",
        }
    return {
        "docs": docs,
        "error": None,
        "codes": codes,
        "confidence": best_conf,
        "ambiguous": False,
        "matched_source": "opengov_project_public" if docs else None,
        "matched_opportunity_id": matched_id,
        "source_url": matched_url,
    }


def _entity_domain_guesses(entity: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", entity or "")
    # Drop only articles/prepositions from acronym construction
    weak = {"the", "of", "and", "a", "an"}
    # Place-name-ish tokens (keep for joined/dashed forms)
    place_stop = {
        "public",
        "utility",
        "district",
        "school",
        "county",
        "city",
        "town",
        "township",
        "borough",
        "village",
        "authority",
        "department",
        "unified",
        "community",
        "college",
        "hospital",
        "fire",
        "water",
        "irrigation",
        "housing",
        "port",
        "transportation",
    }
    content = [w for w in words if w.lower() not in weak]
    place = [w for w in content if w.lower() not in place_stop]
    if not place:
        place = content[:4]
    guesses: list[str] = []
    # Acronym including org-type words → tdpud for Truckee Donner Public Utility District
    if content:
        acronym = "".join(w[0] for w in content).lower()
        if 3 <= len(acronym) <= 8:
            guesses.append(acronym)
    if place:
        initials = "".join(w[0] for w in place).lower()
        if len(initials) >= 3:
            guesses.append(initials)
        joined = "".join(place[:4]).lower()
        if len(joined) >= 6:
            guesses.append(joined[:40])
        dashed = "-".join(w.lower() for w in place[:4])
        if len(dashed) >= 6:
            guesses.append(dashed[:40])
        # place + pud/usd style
        if any(w.lower() == "district" for w in content) and any(
            w.lower() == "utility" for w in content
        ):
            guesses.append("".join(w[0] for w in place).lower() + "pud")
    hosts: list[str] = []
    for g in guesses:
        for tld in (".org", ".gov", ".com", ".us"):
            hosts.append(f"www.{g}{tld}")
            hosts.append(f"{g}{tld}")
    out: list[str] = []
    seen: set[str] = set()
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:16]


def _agency_website_chase(
    rec: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Guess official buyer site from overview entity; pull free bid docs."""
    import httpx
    from urllib.parse import urljoin

    title = str(rec.get("title") or "")
    overview = _overview_text(rec, parsed)
    buyer = str((parsed or {}).get("agency") or rec.get("buyer") or "")
    entities = [
        e
        for e in _entity_candidates(title, buyer, overview)
        if e.lower() not in _STATE_NAMES and len(e) > 8
    ]
    if not entities:
        return {"docs": [], "error": "NO_ENTITY_FOR_SITE"}

    docs: list[dict[str, Any]] = []
    errors: list[str] = []
    source_url = None
    matched_entity = None
    confidence = 0.0

    with httpx.Client(
        timeout=httpx.Timeout(8.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for entity in entities[:3]:
            hosts = _entity_domain_guesses(entity)
            # Prefer .org/.gov only for first pass (faster, fewer dead TLDs)
            hosts = [h for h in hosts if h.endswith(".org") or h.endswith(".gov")] + [
                h for h in hosts if not (h.endswith(".org") or h.endswith(".gov"))
            ]
            for host in hosts[:10]:
                home = f"https://{host}/"
                try:
                    r = client.get(home)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    continue
                if r.status_code >= 400:
                    continue
                # Likely real site if entity token appears (skip articles)
                tok = [t for t in _norm(entity).split() if t not in {"the", "of", "and", "a", "an"}]
                page_l = (r.text or "").lower()
                compact = _norm(entity).replace(" ", "")[:10]
                if tok and tok[0] not in page_l and compact not in _norm(page_l):
                    continue
                bid_paths = re.findall(
                    r'href=["\']([^"\']*(?:bid|rfp|rfq|propos|procure|solicit|purchas)[^"\']*)["\']',
                    r.text or "",
                    re.I,
                )
                urls: list[str] = []
                for href in bid_paths[:12]:
                    if href.startswith("mailto:") or href.startswith("javascript:"):
                        continue
                    urls.append(urljoin(str(r.url), href))
                for path in (
                    "/bids",
                    "/bid-opportunities",
                    "/request-for-proposals-bid-requests",
                    "/rfps",
                    "/purchasing",
                    "/procurement",
                ):
                    urls.append(urljoin(str(r.url), path))
                seen_u: set[str] = set()
                title_l = title.lower()
                title_tokens = [t for t in _norm(title).split() if len(t) >= 4][:6]

                def _page_mentions_title(body: str) -> float:
                    bl = (body or "").lower()
                    if title_l[:36] and title_l[:36] in bl:
                        return 0.85
                    # soft token coverage against page text
                    if not title_tokens:
                        return 0.0
                    hit = sum(1 for t in title_tokens if t in bl)
                    return hit / max(1, len(title_tokens))

                def _extract_file_docs(page_url: str, body: str, conf: float) -> list[dict[str, Any]]:
                    found: list[dict[str, Any]] = []
                    for m in re.finditer(
                        r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|csv|zip)(?:\?[^"\']*)?)["\']',
                        body or "",
                        re.I,
                    ):
                        du = urljoin(page_url, m.group(1))
                        if _BIDNET_HOST.search(du):
                            continue
                        found.append(
                            {
                                "document_url": du,
                                "document_name": du.rsplit("/", 1)[-1][:160],
                                "document_type": "attachment",
                                "retrieval_status": "URL_DISCOVERED",
                                "free_chase": True,
                                "free_source": "official_buyer_procurement_page",
                                "match_confidence": conf,
                                "provenance": {
                                    "route": "official_buyer_procurement_page",
                                    "entity": entity,
                                    "source_url": page_url,
                                    "confidence": conf,
                                },
                            }
                        )
                    return found

                for u in urls:
                    key = u.split("#")[0].rstrip("/").lower()
                    if key in seen_u:
                        continue
                    seen_u.add(key)
                    try:
                        pr = client.get(u)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                        continue
                    if pr.status_code >= 400:
                        continue
                    body = pr.text or ""
                    page_score = max(_title_overlap(title, body[:8000]), _page_mentions_title(body))
                    got, err = _docs_from_portal_resolve(str(pr.url), rec)
                    if err and err not in {"HTTP_404"}:
                        errors.append(err)
                    if got and page_score >= 0.12:
                        for d in got:
                            d["match_confidence"] = max(0.55, page_score)
                            d["provenance"] = {
                                "route": "official_buyer_procurement_page",
                                "entity": entity,
                                "source_url": str(pr.url),
                                "confidence": page_score,
                            }
                        docs = got
                        source_url = str(pr.url)
                        matched_entity = entity
                        confidence = page_score
                        break

                    # Follow title-matching internal links (listing → solicitation detail)
                    if page_score >= 0.2 or _page_mentions_title(body) >= 0.4:
                        detail_urls: list[str] = []
                        for m in re.finditer(
                            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]{0,200})</a>',
                            body,
                            re.I,
                        ):
                            href, txt = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
                            blob = f"{href} {txt}"
                            if _page_mentions_title(blob) >= 0.35 or _title_overlap(title, blob) >= 0.35:
                                detail_urls.append(urljoin(str(pr.url), href))
                        for du in detail_urls[:6]:
                            dkey = du.split("#")[0].rstrip("/").lower()
                            if dkey in seen_u:
                                continue
                            seen_u.add(dkey)
                            try:
                                dr = client.get(du)
                            except Exception as exc:
                                errors.append(type(exc).__name__)
                                continue
                            if dr.status_code >= 400:
                                continue
                            dbody = dr.text or ""
                            dscore = max(
                                _title_overlap(title, dbody[:8000]),
                                _page_mentions_title(dbody),
                                0.7,
                            )
                            file_docs = _extract_file_docs(str(dr.url), dbody, dscore)
                            more, _e2 = _docs_from_portal_resolve(str(dr.url), rec)
                            for d in more:
                                d["match_confidence"] = dscore
                                d["provenance"] = {
                                    "route": "official_buyer_procurement_page",
                                    "entity": entity,
                                    "source_url": str(dr.url),
                                    "confidence": dscore,
                                }
                            file_docs.extend(more)
                            if file_docs:
                                docs = file_docs
                                source_url = str(dr.url)
                                matched_entity = entity
                                confidence = dscore
                                break
                        if docs:
                            break
                        # listing page itself may embed files
                        file_docs = _extract_file_docs(str(pr.url), body, max(0.55, page_score))
                        if file_docs:
                            docs = file_docs
                            source_url = str(pr.url)
                            matched_entity = entity
                            confidence = max(0.55, page_score)
                            break
                if docs:
                    break
            if docs:
                break

    if docs:
        return {
            "docs": docs,
            "confidence": confidence,
            "source_url": source_url,
            "matched_source": "official_buyer_procurement_page",
            "matched_opportunity_id": matched_entity,
        }
    # Prefer a semantic miss over a transient connect error from bad domain guesses
    if errors and all(
        e in {"ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException"} for e in errors
    ):
        err = "ConnectError"
    elif errors:
        err = "AGENCY_SITE_NO_PACKAGE"
    else:
        err = "AGENCY_SITE_NOT_FOUND"
    return {
        "docs": [],
        "error": err,
        "confidence": 0.0,
        "retry_reasons": errors[:8],
    }


def _store_free_duplicate_match(
    rec: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cross-match free portals in canonical store. Solicitation# dominates."""
    title = str(rec.get("title") or "").strip()
    sol = _norm_sol(_sol_number(rec, parsed))
    buyer = _norm(str(rec.get("buyer") or ""))
    state = _state_hint(rec, parsed)
    if len(title) < 8 and not sol:
        return {"docs": [], "confidence": 0.0, "ambiguous": False}

    if store is None:
        try:
            from phase_l.l23_full_population_funnel import load_store

            store = load_store()
        except Exception as exc:
            return {"docs": [], "error": type(exc).__name__, "confidence": 0.0, "ambiguous": False}

    self_id = str(rec.get("canonical_id") or rec.get("id") or "")
    # Without a solicitation number, full-store title scans are too expensive and too weak
    # for universe-scale free-package chase. Prefer OpenGov/agency routes instead.
    if not sol:
        return {"docs": [], "confidence": 0.0, "ambiguous": False, "skipped": "NO_SOL_FOR_STORE_MATCH"}

    hits: list[tuple[float, str, dict[str, Any]]] = []
    for cid, other in store.items():
        if not isinstance(other, dict) or str(cid) == self_id:
            continue
        plat = str(other.get("platform") or "").lower()
        if "bidnet" in plat:
            continue
        other_sol = _norm_sol(
            other.get("solicitation_event_id")
            or other.get("solicitation_number")
            or ((other.get("row_ref") or {}).get("solicitation_number") if isinstance(other.get("row_ref"), dict) else None)
        )
        score = 0.0
        if sol and other_sol and sol == other_sol:
            score = 0.99
        elif sol and other_sol and (sol in other_sol or other_sol in sol) and len(sol) >= 6:
            score = 0.92
        else:
            tscore = _title_overlap(title, str(other.get("title") or ""))
            if tscore < 0.60:
                continue
            # Title-only requires additional evidence
            extra = 0
            obuyer = _norm(str(other.get("buyer") or other.get("agency") or ""))
            if buyer and obuyer and (buyer in obuyer or obuyer in buyer):
                extra += 1
            ostate = _state_hint(other)
            if state and ostate and state == ostate:
                extra += 1
            odl = str(other.get("deadline") or "")[:10]
            rdl = str(rec.get("deadline") or "")[:10]
            if odl and rdl and odl == rdl:
                extra += 1
            if extra < 1:
                continue
            score = min(0.88, tscore + 0.05 * extra)
        if score < 0.60:
            continue
        hits.append((score, str(cid), other))

    hits.sort(key=lambda x: -x[0])
    if len(hits) >= 2 and abs(hits[0][0] - hits[1][0]) < 0.03 and hits[0][0] < 0.95:
        return {
            "docs": [],
            "confidence": hits[0][0],
            "ambiguous": True,
            "matched_opportunity_id": hits[0][1],
            "matched_source": "store_duplicate_free_portal",
        }
    if not hits:
        return {"docs": [], "confidence": 0.0, "ambiguous": False}

    score, cid, best_rec = hits[0]
    docs: list[dict[str, Any]] = []
    for d in best_rec.get("attachments_metadata") or []:
        if not isinstance(d, dict):
            continue
        u = d.get("document_url") or d.get("url")
        if not u or _BIDNET_HOST.search(str(u)):
            continue
        dd = dict(d)
        dd["document_url"] = u
        dd["free_chase"] = True
        dd["free_source"] = "store_duplicate_free_portal"
        dd["match_confidence"] = score
        dd["matched_opportunity_id"] = cid
        dd["provenance"] = {
            "route": "store_duplicate_free_portal",
            "matched_opportunity_id": cid,
            "platform": best_rec.get("platform"),
            "confidence": score,
        }
        docs.append(dd)
    if not docs:
        url = str(
            best_rec.get("authoritative_url")
            or (best_rec.get("row_ref") or {}).get("detail_url")
            or ""
        )
        if url and not _BIDNET_HOST.search(url):
            more, err = _docs_from_portal_resolve(url, rec)
            if err:
                return {"docs": [], "error": err, "confidence": score, "ambiguous": False}
            for d in more:
                d["match_confidence"] = score
                d["matched_opportunity_id"] = cid
            docs = more
    return {
        "docs": docs,
        "confidence": score,
        "ambiguous": False,
        "matched_opportunity_id": cid,
        "matched_source": str(best_rec.get("platform") or "store_duplicate_free_portal"),
        "source_url": str(
            best_rec.get("authoritative_url")
            or (best_rec.get("row_ref") or {}).get("detail_url")
            or ""
        )
        or None,
    }


def _identity_sufficient(rec: dict[str, Any], parsed: dict[str, Any] | None, codes: list[str], urls: list[str]) -> bool:
    """Enough identity to call a miss conclusive (vs retryable incomplete)."""
    sol = _sol_number(rec, parsed)
    buyer = str(rec.get("buyer") or (parsed or {}).get("agency") or "").strip()
    buyer_is_state = buyer.lower() in _STATE_NAMES
    overview = _overview_text(rec, parsed)
    entities = _entity_candidates(str(rec.get("title") or ""), buyer, overview)
    real_entity = any(
        e.lower() not in _STATE_NAMES and len(e) > 8 and " " in e for e in entities
    )
    return bool(urls or sol or real_entity or (codes and (buyer and not buyer_is_state)))


def chase_free_package(
    rec: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    store: dict[str, Any] | None = None,
    refresh_overview: bool = True,
) -> dict[str, Any]:
    """Attempt free public package recovery. Never uses BidNet membership paths."""
    started = now_utc().isoformat()
    attempts: list[dict[str, Any]] = []
    all_docs: list[dict[str, Any]] = []
    seen_u: set[str] = set()
    retry_reasons: list[str] = []
    ambiguous = False
    recovery_route = None
    source_url = None
    matched_source = None
    matched_opportunity_id = None
    confidence = 0.0
    codes_tried: list[str] = []

    parsed = dict(parsed or {})
    if refresh_overview and not _overview_text(rec, parsed):
        refreshed = refresh_public_overview(rec)
        if refreshed.get("error"):
            retry_reasons.append(str(refreshed["error"]))
            attempts.append(
                {
                    "via": "bidnet_public_overview_refresh",
                    "error": refreshed.get("error"),
                    "at": now_utc().isoformat(),
                }
            )
        else:
            for k in (
                "overview",
                "description",
                "agency",
                "agency_source_url",
                "location",
                "solicitation_number",
                "deadline",
            ):
                if refreshed.get(k) and not parsed.get(k):
                    parsed[k] = refreshed[k]
            if refreshed.get("overview") or refreshed.get("description"):
                br = rec.setdefault("bidnet_recovery", {})
                if isinstance(br, dict):
                    br["overview"] = refreshed.get("overview") or refreshed.get("description")
                if refreshed.get("description") and (
                    not rec.get("description")
                    or len(str(refreshed["description"])) > len(str(rec.get("description") or ""))
                ):
                    rec["description"] = refreshed["description"]
            attempts.append(
                {
                    "via": "bidnet_public_overview_refresh",
                    "overview": bool(refreshed.get("overview") or refreshed.get("description")),
                    "agency": refreshed.get("agency"),
                    "entities": _entity_candidates(
                        str(rec.get("title") or ""),
                        str(parsed.get("agency") or rec.get("buyer") or ""),
                        _overview_text(rec, parsed),
                    )[:5],
                    "at": now_utc().isoformat(),
                }
            )

    def absorb(docs: list[dict[str, Any]], via: str, conf: float = 0.0) -> None:
        nonlocal recovery_route, confidence
        for d in docs:
            u = str(d.get("document_url") or "")
            if not u or u in seen_u or _BIDNET_HOST.search(u):
                continue
            seen_u.add(u)
            dd = dict(d)
            dd.setdefault("free_chase_via", via)
            all_docs.append(dd)
        if docs and (recovery_route is None or conf > confidence):
            recovery_route = via
            confidence = max(confidence, conf or max((float(d.get("match_confidence") or 0) for d in docs), default=0.0))

    urls = candidate_free_urls(rec, parsed)
    for url in urls[:8]:
        docs, err = _docs_from_portal_resolve(url, rec)
        attempts.append(
            {
                "via": "agency_or_provenance_url",
                "url": url[:220],
                "docs": len(docs),
                "error": err,
                "at": now_utc().isoformat(),
            }
        )
        if err:
            retry_reasons.append(err)
            continue
        absorb(docs, "agency_or_provenance_url", 0.85 if docs else 0.0)
        if docs:
            source_url = url
            matched_source = "agency_or_provenance_url"
            break

    if len(all_docs) < 1:
        store_hit = _store_free_duplicate_match(rec, parsed=parsed, store=store)
        attempts.append(
            {
                "via": "store_duplicate_free_portal",
                "docs": len(store_hit.get("docs") or []),
                "error": store_hit.get("error"),
                "ambiguous": store_hit.get("ambiguous"),
                "confidence": store_hit.get("confidence"),
                "at": now_utc().isoformat(),
            }
        )
        if store_hit.get("error"):
            retry_reasons.append(str(store_hit["error"]))
        if store_hit.get("ambiguous"):
            ambiguous = True
            matched_opportunity_id = store_hit.get("matched_opportunity_id")
            matched_source = store_hit.get("matched_source")
            confidence = float(store_hit.get("confidence") or 0)
        else:
            absorb(list(store_hit.get("docs") or []), "store_duplicate_free_portal", float(store_hit.get("confidence") or 0))
            if store_hit.get("docs"):
                matched_opportunity_id = store_hit.get("matched_opportunity_id")
                matched_source = store_hit.get("matched_source")
                source_url = store_hit.get("source_url") or source_url

    # Fast-fail: no free URLs, no solicitation #, no overview text → skip expensive OpenGov/site HTTP.
    # State-level buyers alone are not enough to justify network chase at universe scale.
    thin_chase = (
        not urls
        and not _sol_number(rec, parsed)
        and not _overview_text(rec, parsed)
        and len(all_docs) < 1
        and not ambiguous
    )

    if len(all_docs) < 1 and not ambiguous and not thin_chase:
        og = _opengov_free_match(rec, parsed=parsed)
        codes_tried = list(og.get("codes") or [])
        attempts.append(
            {
                "via": "opengov_public_match",
                "docs": len(og.get("docs") or []),
                "codes_tried": codes_tried[:6],
                "error": og.get("error"),
                "ambiguous": og.get("ambiguous"),
                "confidence": og.get("confidence"),
                "at": now_utc().isoformat(),
            }
        )
        if og.get("error"):
            retry_reasons.append(str(og["error"]))
        if og.get("ambiguous"):
            ambiguous = True
            matched_source = og.get("matched_source")
            matched_opportunity_id = og.get("matched_opportunity_id")
        else:
            absorb(list(og.get("docs") or []), "opengov_public_match", float(og.get("confidence") or 0))
            if og.get("docs"):
                matched_source = og.get("matched_source")
                matched_opportunity_id = og.get("matched_opportunity_id")
                source_url = og.get("source_url") or source_url

    if len(all_docs) < 1 and not ambiguous and not thin_chase:
        site = _agency_website_chase(rec, parsed=parsed)
        attempts.append(
            {
                "via": "official_buyer_procurement_page",
                "docs": len(site.get("docs") or []),
                "error": site.get("error"),
                "confidence": site.get("confidence"),
                "source_url": (site.get("source_url") or "")[:220] or None,
                "at": now_utc().isoformat(),
            }
        )
        if site.get("error"):
            retry_reasons.append(str(site["error"]))
        absorb(
            list(site.get("docs") or []),
            "official_buyer_procurement_page",
            float(site.get("confidence") or 0),
        )
        if site.get("docs"):
            matched_source = site.get("matched_source")
            matched_opportunity_id = site.get("matched_opportunity_id")
            source_url = site.get("source_url") or source_url
    elif thin_chase:
        retry_reasons.append("INCOMPLETE_IDENTITY_FAST_FAIL")
        attempts.append(
            {
                "via": "fast_fail_thin_identity",
                "skipped_opengov_agency_http": True,
                "at": now_utc().isoformat(),
            }
        )

    fp = fingerprint_for_chase(rec, parsed)
    doc_types = sorted(
        {
            str(d.get("document_type") or "attachment")
            for d in all_docs
            if isinstance(d, dict)
        }
    )

    if all_docs:
        # Quality gate — do not count random PDFs as FREE_PACKAGE_FOUND
        try:
            from document_quality import (
                PARTIAL_SOLICITATION_PACKAGE,
                UNRELATED_DOCUMENTS,
                VALID_SOLICITATION_PACKAGE,
                classify_package_documents,
            )

            scored = [
                {
                    "document_name": d.get("document_name") or d.get("filename") or d.get("name") or "",
                    "text": str(d.get("document_name") or d.get("filename") or ""),
                }
                for d in all_docs
                if isinstance(d, dict)
            ]
            q = classify_package_documents(
                scored,
                title=rec.get("title"),
                buyer=rec.get("buyer") or (parsed or {}).get("agency"),
                solicitation_number=_sol_number(rec, parsed),
            )
            pq = str(q.get("package_quality") or "")
            if pq == VALID_SOLICITATION_PACKAGE:
                status = VALID_FREE_PACKAGE_FOUND
                note = "Valid free solicitation package recovered; BidNet membership not used."
            elif pq == PARTIAL_SOLICITATION_PACKAGE:
                status = PARTIAL_FREE_PACKAGE_FOUND
                note = "Partial free solicitation package recovered; BidNet membership not used."
            elif pq == UNRELATED_DOCUMENTS:
                status = PACKAGE_MATCH_AMBIGUOUS
                note = "Documents found but quality gate rejected as unrelated/non-solicitation."
                # Keep URLs for provenance; do not count as package success
            else:
                # Name-only score often ambiguous until PDF text is extracted downstream
                status = PACKAGE_MATCH_AMBIGUOUS
                note = "Documents found; package quality pending PDF text validation."
        except Exception:
            status = PACKAGE_MATCH_AMBIGUOUS
            note = "Documents found; quality gate error — not counting as free package yet."
    elif ambiguous:
        status = PACKAGE_MATCH_AMBIGUOUS
        note = "Multiple free-source matches conflict; not applying package."
    elif "INCOMPLETE_IDENTITY_FAST_FAIL" in retry_reasons:
        status = PACKAGE_RECOVERY_RETRYABLE
        note = "Incomplete buyer/solicitation/agency metadata for conclusive free chase."
    elif retry_reasons and not _identity_sufficient(rec, parsed, codes_tried, urls):
        status = PACKAGE_RECOVERY_RETRYABLE
        note = f"Retryable: {', '.join(retry_reasons[:4])}"
    elif retry_reasons and any(
        r.startswith("HTTP_") or r in {"TIMEOUT", "ConnectError", "ReadTimeout", "NO_OPENGOV_CODES"}
        for r in retry_reasons
    ):
        # Transient / incomplete search coverage
        status = PACKAGE_RECOVERY_RETRYABLE
        note = f"Retryable: {', '.join(retry_reasons[:4])}"
    elif not _identity_sufficient(rec, parsed, codes_tried, urls):
        status = PACKAGE_RECOVERY_RETRYABLE
        note = "Incomplete buyer/solicitation/agency metadata for conclusive free chase."
    else:
        status = PACKAGE_UNAVAILABLE_FREE
        note = (
            "No free public package found after conclusive free-source search. "
            "BidNet member docs require membership — economics-dead until new free evidence."
        )

    prior = (
        (rec.get("bidnet_recovery") or {}).get("free_package_chase")
        if isinstance(rec.get("bidnet_recovery"), dict)
        else None
    )
    attempt_count = int((prior or {}).get("attempt_count") or 0) + 1

    return {
        "status": status,
        "documents": all_docs,
        "document_count": len(all_docs),
        "document_types": doc_types,
        "documents_found": len(all_docs),
        "attempts": attempts,
        "attempt_count": attempt_count,
        "last_attempt_at": now_utc().isoformat(),
        "candidate_urls": urls[:8],
        "recovery_route": recovery_route,
        "source_url": source_url,
        "matched_source": matched_source,
        "matched_opportunity_id": matched_opportunity_id,
        "confidence": round(float(confidence or 0), 3),
        "fingerprint": fp,
        "codes_tried": codes_tried[:6],
        "retry_reasons": retry_reasons,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "membership_required": False,
        "note": note,
    }
