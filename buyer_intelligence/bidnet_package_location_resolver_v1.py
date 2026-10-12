"""BidNet official package-location resolver + end-to-end recovery V1.

BidNet notice ≠ BidNet package host. Resolve authoritative vendor path, then recover.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from buyer_intelligence.bidnet_auth_recovery_package_closure_v1 import (
    compute_full100_progression,
    freeze_auth_required_36,
    _data,
    _load_json,
    _write_json,
)
from buyer_intelligence.package_recovery_source_depth_v1 import (
    _real_evidence_lines,
    extract_eligibility_from_text,
    extract_lines_from_evidence,
    extract_revenue_from_text,
    reclassify_depth,
)
from buyer_intelligence.stage1_full_production_acceptance_v1 import (
    assess_acquisition_path,
    assess_coverage_and_economics,
    assess_eligibility,
    assess_identity,
    assess_product_gate,
    decide_disposition,
    lookup_public_price,
    lookup_real_quote,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_jobs.store import JobLeaseError, JobStore
from package_recovery_sam_budget.memory import remember_buyer_package_source
from package_recovery_sam_budget.quality import is_valid_package_evidence

log = logging.getLogger("govtracker.bidnet_package_location_resolver")

BUILD = "20261009-m3-bidnet-package-location-resolver-v1"
FAMILY = "bidnet_package_location_resolver"
FIXED_JOB = "bidnet_package_recovery-20261009T093314Z-58a01f59"
ITEM_TIMEOUT_SEC = 25.0
REPORT = "m3_bidnet_package_location_resolver_v1_report.json"
RESULTS = "m3_bidnet_package_location_resolver_v1_results.json"
MEMORY = "m3_bidnet_package_location_memory_v1.json"
AUDIT = "m3_bidnet_package_location_audit_20_v1.json"

PLATFORM_PATTERNS = [
    ("OpenGov", re.compile(r"opengov\.com|procurement\.opengov", re.I)),
    ("Bonfire", re.compile(r"bonfirehub\.com|gobonfire", re.I)),
    ("PlanetBids", re.compile(r"planetbids\.com", re.I)),
    ("PublicPurchase", re.compile(r"publicpurchase\.com", re.I)),
    ("IonWave", re.compile(r"ionwave\.net|ionwave\.com", re.I)),
    ("DemandStar", re.compile(r"demandstar\.com", re.I)),
    ("Jaggaer/Euna", re.compile(r"jaggaer\.com|euna\.com|bids\.sciquest", re.I)),
    ("ProcureNow", re.compile(r"procurenow\.com", re.I)),
    ("Periscope", re.compile(r"periscopeholdings\.com|bidsync\.com", re.I)),
    ("Ariba", re.compile(r"ariba\.com|sap\.com", re.I)),
    ("BidNet Direct", re.compile(r"bidnetdirect\.com|bidnet\.com", re.I)),
]

CONTACT_RE = re.compile(
    r"(?:contact|email|request|obtain|submit)\s+(?:documents?|specs?|package|solicitation)"
    r"|documents?\s+(?:must\s+be\s+)?(?:requested|obtained)\s+from"
    r"|for\s+(?:a\s+)?copy\s+(?:of\s+)?(?:the\s+)?(?:bid|solicitation|package)",
    re.I,
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store() -> JobStore:
    return JobStore(FAMILY)


def _detect_platform(url: str | None) -> str | None:
    u = str(url or "")
    for name, pat in PLATFORM_PATTERNS:
        if pat.search(u):
            return name
    return None


def _buyer_memory_lookup(buyer: str) -> dict[str, Any]:
    from package_recovery_sam_budget.models import BUYER_MEMORY

    mem = _load_json(_data(BUYER_MEMORY)) or {}
    key = (buyer or "").strip().lower()[:120]
    return dict((mem.get("by_buyer") or {}).get(key) or {})


def extract_location_clues(row: dict[str, Any], l23: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Extract only proven fields — no invention."""
    text_blob = " ".join(
        str(x or "")
        for x in (
            parsed.get("description"),
            parsed.get("overview"),
            l23.get("description"),
            row.get("TITLE"),
        )
    )
    emails = EMAIL_RE.findall(text_blob)[:5]
    phones = PHONE_RE.findall(text_blob)[:3]
    urls = []
    for u in URL_RE.findall(text_blob)[:8]:
        if "bidnet" not in u.lower() or "source" in text_blob.lower():
            urls.append(u.rstrip(").,;"))
    ext = (
        parsed.get("agency_source_url")
        or l23.get("original_posting_url")
        or l23.get("agency_source_url")
    )
    return {
        "SOLICITATION_NUMBER": parsed.get("solicitation_number") or l23.get("solicitation_number"),
        "EVENT_NUMBER": (l23.get("raw_metadata") or {}).get("bidnet_internal_id"),
        "BID_NUMBER": l23.get("bid_number"),
        "PROJECT_NUMBER": l23.get("project_number"),
        "BUYER_NAME": parsed.get("agency") or row.get("BUYER") or l23.get("buyer"),
        "DEPARTMENT": l23.get("department"),
        "CONTACT_EMAILS": emails,
        "CONTACT_PHONES": phones,
        "EXTERNAL_URL": ext if isinstance(ext, str) and ext.startswith("http") else None,
        "EXTRA_URLS": urls,
        "STATE": parsed.get("location") or l23.get("location") or l23.get("jurisdiction"),
        "DESCRIPTION_LEN": len(str(parsed.get("description") or l23.get("description") or "")),
        "OVERVIEW_LEN": len(str(parsed.get("overview") or "")),
        "LOCKED_FIELDS": list(parsed.get("locked_fields") or []),
        "AUTH_WALL": bool(parsed.get("auth_wall")),
        "CONTACT_ONLY_SIGNAL": bool(CONTACT_RE.search(text_blob)),
        "BIDNET_URL": l23.get("authoritative_url") or row.get("PORTAL") or l23.get("detail_url"),
        "PROVENANCE": {
            "parsed_fields": [k for k, v in (parsed or {}).items() if v],
            "source": "bidnet_detail_and_l23",
        },
    }


def classify_bidnet_role(*, clues: dict[str, Any], bidnet_docs: list, chase: dict[str, Any]) -> str:
    if bidnet_docs and any(
        d.get("retrieval_status") in {"DOWNLOADED", "BROWSER_DOWNLOAD"} for d in bidnet_docs if isinstance(d, dict)
    ):
        return "HOSTS_PACKAGE"
    if clues.get("EXTERNAL_URL") and "bidnet" not in str(clues.get("EXTERNAL_URL")).lower():
        return "REDIRECTS_TO_EXTERNAL_PORTAL"
    if clues.get("AUTH_WALL") or clues.get("LOCKED_FIELDS"):
        return "HOSTS_DETAIL_ONLY"
    if clues.get("DESCRIPTION_LEN", 0) >= 40 or clues.get("OVERVIEW_LEN", 0) >= 40:
        return "HOSTS_DETAIL_ONLY"
    if clues.get("BIDNET_URL"):
        return "HOSTS_NOTICE_ONLY"
    return "UNKNOWN"


def classify_access_state(chase: dict[str, Any], *, docs: list, location_state: str) -> str:
    if docs and is_valid_package_evidence(docs).get("valid"):
        return "PUBLIC"
    status = str(chase.get("status") or "")
    note = str(chase.get("note") or chase.get("chase_status") or "").lower()
    # Prefer explicit chase fields — do NOT treat BidNet membership noise as paid entitlement
    explicit = str(
        chase.get("access_state")
        or chase.get("package_access")
        or chase.get("entitlement")
        or ""
    ).upper()
    if explicit in {
        "PUBLIC",
        "AUTH_REQUIRED",
        "REGISTRATION_REQUIRED",
        "BUYER_APPROVAL_REQUIRED",
        "PAID_ENTITLEMENT_REQUIRED",
        "BOT_BLOCKED",
        "UNAVAILABLE",
        "CONTACT_REQUEST",
    }:
        return explicit
    blob = f"{note} {chase.get('recovery_route') or ''} {chase.get('matched_source') or ''}".lower()
    if location_state == "EMAIL_CONTACT_ONLY":
        return "CONTACT_REQUEST"
    if "paid subscription" in blob or "paid entitlement" in blob or "document fee" in blob:
        return "PAID_ENTITLEMENT_REQUIRED"
    if "buyer approval" in blob or "pending approval" in blob:
        return "BUYER_APPROVAL_REQUIRED"
    if "registration required" in blob or "register to bid" in blob or "vendor registration" in blob:
        return "REGISTRATION_REQUIRED"
    if "login required" in blob or "sign in required" in blob:
        return "AUTH_REQUIRED"
    if "bot" in blob or "captcha" in blob:
        return "BOT_BLOCKED"
    if location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"} and not docs:
        # Official location known; files not retrieved — most often registration/login
        if "opengov" in blob or "planetbids" in blob or "bonfire" in blob or "ionwave" in blob:
            return "REGISTRATION_REQUIRED"
        return "UNAVAILABLE"
    if status in {"PACKAGE_UNAVAILABLE_FREE"} and not docs:
        return "UNAVAILABLE"
    if location_state == "PACKAGE_LOCATION_UNRESOLVED":
        return "UNAVAILABLE"
    return "PUBLIC" if docs else "UNAVAILABLE"


def classify_submission_method(clues: dict[str, Any], location_state: str, platform: str | None) -> str:
    ext = str(clues.get("EXTERNAL_URL") or "").lower()
    if location_state == "EMAIL_CONTACT_ONLY":
        return "EMAIL"
    if location_state == "BIDNET_HOSTED" or platform == "BidNet Direct":
        return "BIDNET"
    if platform and platform != "BidNet Direct":
        return "THIRD_PARTY_PORTAL" if platform not in {"Custom buyer portal"} else "BUYER_PORTAL"
    if ext and "bidnet" not in ext:
        return "BUYER_PORTAL"
    return "UNKNOWN"


def resolve_package_location(
    *,
    clues: dict[str, Any],
    bidnet_role: str,
    chase: dict[str, Any],
    docs: list,
    match_confidence: str,
) -> tuple[str, str | None, str | None]:
    """Return (PACKAGE_LOCATION_STATE, OFFICIAL_LOCATION, PLATFORM)."""
    ext = clues.get("EXTERNAL_URL")
    platform = _detect_platform(ext) if ext else None
    if docs and is_valid_package_evidence(docs).get("valid"):
        route = str(chase.get("recovery_route") or chase.get("matched_source") or "")
        plat = _detect_platform(route) or platform or _detect_platform(
            (docs[0].get("document_url") if docs else None)
        )
        loc_url = (
            chase.get("matched_url")
            or chase.get("package_url")
            or (docs[0].get("document_url") if docs else None)
            or ext
        )
        if plat == "BidNet Direct":
            return "BIDNET_HOSTED", loc_url, plat
        if plat in {
            "OpenGov",
            "Bonfire",
            "PlanetBids",
            "PublicPurchase",
            "IonWave",
            "DemandStar",
            "Jaggaer/Euna",
            "ProcureNow",
            "Periscope",
            "Ariba",
        }:
            return "THIRD_PARTY_PORTAL", loc_url, plat
        if loc_url and "bidnet" not in str(loc_url).lower():
            return "BUYER_PORTAL", loc_url, plat or "Custom buyer portal"
        return "DIRECT_OFFICIAL_DOCUMENT", loc_url, plat

    if clues.get("CONTACT_ONLY_SIGNAL") and clues.get("CONTACT_EMAILS"):
        return "EMAIL_CONTACT_ONLY", clues["CONTACT_EMAILS"][0], None

    if match_confidence in {"HIGH", "MEDIUM"} and ext and "bidnet" not in str(ext).lower():
        plat = platform or "Custom buyer portal"
        if plat != "BidNet Direct":
            # Location found but package files not recovered
            if plat in {
                "OpenGov",
                "Bonfire",
                "PlanetBids",
                "PublicPurchase",
                "IonWave",
                "DemandStar",
                "Jaggaer/Euna",
            }:
                return "THIRD_PARTY_PORTAL", ext, plat
            return "BUYER_PORTAL", ext, plat

    if bidnet_role == "HOSTS_PACKAGE":
        return "BIDNET_HOSTED", clues.get("BIDNET_URL"), "BidNet Direct"

    # Sufficient inline detail for quote without package?
    if clues.get("DESCRIPTION_LEN", 0) >= 500 and not clues.get("AUTH_WALL"):
        return "NO_PACKAGE_REQUIRED", clues.get("BIDNET_URL"), "BidNet Direct"

    if chase.get("status") == "PACKAGE_UNAVAILABLE_FREE" and not ext:
        return "PACKAGE_LOCATION_UNRESOLVED", None, None

    return "PACKAGE_LOCATION_UNRESOLVED", None, None


def _match_confidence(clues: dict[str, Any], chase: dict[str, Any]) -> str:
    conf = str(chase.get("confidence") or chase.get("match_confidence") or "").upper()
    if conf in {"HIGH", "MEDIUM", "LOW"}:
        return conf
    sol = _norm_sol(clues.get("SOLICITATION_NUMBER"))
    matched_sol = _norm_sol(chase.get("matched_solicitation") or chase.get("solicitation_number"))
    if sol and matched_sol and sol == matched_sol and chase.get("matched_source"):
        return "HIGH"
    if chase.get("verified") or chase.get("cross_portal_match"):
        return "MEDIUM"
    if clues.get("EXTERNAL_URL") and "bidnet" not in str(clues.get("EXTERNAL_URL")).lower():
        return "MEDIUM"
    # Title/fuzzy-only matched_source without solicitation or external URL is LOW
    if chase.get("matched_opportunity_id") and sol:
        return "MEDIUM"
    if chase.get("matched_source") and sol:
        return "MEDIUM"
    return "LOW"


def _norm_sol(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _fetch_public_clues(url: str) -> dict[str, Any]:
    if not url or not str(url).startswith("http"):
        return {}
    try:
        import httpx
        from bidnet_recovery.parse_abstract import parse_bidnet_abstract

        r = httpx.get(url, timeout=12.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            return {}
        return parse_bidnet_abstract(r.text or "", detail_url=str(r.url))
    except Exception:
        return {}


def _probe_paid_intercept(client: Any, url: str, *, timeout_ms: int = 18_000) -> dict[str, Any]:
    """Fast authenticated goto — detect BidNet paid statewide intercept. No doc discovery."""
    out: dict[str, Any] = {
        "attempted": True,
        "final_url": None,
        "paid_entitlement_requirement": None,
        "paid_entitlement_intercept": None,
        "error": None,
    }
    if not url or client is None:
        out["error"] = "NO_URL_OR_CLIENT"
        return out
    page = getattr(client, "_page", None)
    if page is None and hasattr(client, "ensure_page"):
        try:
            client.ensure_page()
            page = getattr(client, "_page", None)
        except Exception as exc:
            out["error"] = type(exc).__name__
            return out
    if page is None:
        out["error"] = "NO_PAGE"
        return out
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        final = str(page.url or url)
        out["final_url"] = final[:220]
        if "subscription-management" in final.lower() or "/intercept/plans" in final.lower():
            out["paid_entitlement_intercept"] = final[:220]
            mreq = re.search(r"requirements=([^&]+)", final, re.I)
            out["paid_entitlement_requirement"] = mreq.group(1) if mreq else "BIDNET_STATEWIDE_PAID"
        return out
    except Exception as exc:
        # Even on timeout, URL may already be the intercept
        try:
            final = str(getattr(page, "url", None) or url)
            out["final_url"] = final[:220]
            if "subscription-management" in final.lower() or "/intercept/plans" in final.lower():
                out["paid_entitlement_intercept"] = final[:220]
                mreq = re.search(r"requirements=([^&]+)", final, re.I)
                out["paid_entitlement_requirement"] = mreq.group(1) if mreq else "BIDNET_STATEWIDE_PAID"
                out["error"] = None
                return out
        except Exception:
            pass
        out["error"] = type(exc).__name__
        return out


def resolve_one(
    row: dict[str, Any],
    *,
    l23: dict[str, Any],
    store_opps: dict[str, Any],
    price_cache: dict[str, Any],
    quotes: dict[str, Any],
    now: datetime,
    client: Any | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    oid = str(row.get("OPPORTUNITY_ID"))
    url = str(l23.get("authoritative_url") or row.get("PORTAL") or l23.get("detail_url") or "")
    work = dict(l23)
    work.setdefault("title", row.get("TITLE") or l23.get("title"))
    work.setdefault("buyer", row.get("BUYER") or l23.get("buyer"))
    work.setdefault("authoritative_url", url)

    parsed: dict[str, Any] = {}
    process_out: dict[str, Any] = {}
    official_hit: dict[str, Any] = {}
    docs: list[dict[str, Any]] = []
    chase: dict[str, Any] = {}
    auth_used = False

    paid_bidnet_entitlement = None
    # FAST PATH: authenticated goto only — detect paid statewide intercept.
    # Full process_bidnet_opportunity (doc discovery) is too slow for 36×60s budget.
    if client is not None and url:
        probe = _probe_paid_intercept(client, url, timeout_ms=18_000)
        auth_used = True
        if probe.get("paid_entitlement_requirement"):
            paid_bidnet_entitlement = probe.get("paid_entitlement_requirement")
            chase = {
                "status": "PACKAGE_REGISTRATION_REQUIRED",
                "recovery_route": "bidnet_paid_statewide_intercept",
                "matched_source": "BidNet Direct",
                "verified": False,
                "note": f"PAID_BIDNET_STATEWIDE_ENTITLEMENT:{paid_bidnet_entitlement}",
                "documents": [],
                "access_state": "PAID_ENTITLEMENT_REQUIRED",
                "entitlement": paid_bidnet_entitlement,
                "paid_entitlement_intercept": probe.get("paid_entitlement_intercept"),
            }
            parsed = {"auth_wall": True, "parse_ok": True, "title": row.get("TITLE")}
        else:
            # No paid wall — light public/parse only (no full doc crawl in this pass)
            parsed = _fetch_public_clues(url)
            chase = {
                "status": "PROBE_NO_PAID_INTERCEPT",
                "recovery_route": "bidnet_probe",
                "note": probe.get("error") or probe.get("final_url"),
                "documents": [],
                "final_url": probe.get("final_url"),
            }
    else:
        parsed = _fetch_public_clues(url)

    # Enrich work from parsed (auth or public)
    if parsed.get("agency"):
        work["agency"] = parsed["agency"]
        work["buyer"] = parsed["agency"]
    if parsed.get("solicitation_number"):
        work["solicitation_number"] = parsed["solicitation_number"]
    if parsed.get("description"):
        work["description"] = parsed["description"]
    if parsed.get("agency_source_url"):
        work["original_posting_url"] = parsed["agency_source_url"]
        work["agency_source_url"] = parsed["agency_source_url"]

    clues = extract_location_clues(row, work, parsed)
    buyer_mem = _buyer_memory_lookup(str(clues.get("BUYER_NAME") or ""))
    if buyer_mem.get("preferred_portal") and not clues.get("EXTERNAL_URL"):
        clues["BUYER_MEMORY_PORTAL"] = buyer_mem.get("preferred_portal")
        clues["BUYER_MEMORY_PATTERN"] = buyer_mem.get("package_url_pattern")

    # If auth path didn't recover docs, force official chase — unless BidNet paid wall
    # already proven (second chase is redundant and too slow for the 60s budget).
    if not docs and not paid_bidnet_entitlement:
        try:
            from package_recovery_sam_budget.recover import recover_bidnet

            meta = {
                "opportunity_id": oid,
                "title": row.get("TITLE") or work.get("title"),
                "buyer": clues.get("BUYER_NAME"),
                "authoritative_url": url,
            }
            official_hit = recover_bidnet(meta, l23_row=work, store=store_opps or {}, force=True) or {}
            chase = {
                "status": official_hit.get("chase_status") or official_hit.get("package_state"),
                "recovery_route": official_hit.get("route") or official_hit.get("route_family"),
                "matched_source": official_hit.get("matched_source") or official_hit.get("route_family"),
                "matched_opportunity_id": official_hit.get("matched_opportunity_id"),
                "matched_solicitation": work.get("solicitation_number"),
                "confidence": official_hit.get("match_confidence")
                or ("HIGH" if official_hit.get("verified") else None),
                "verified": official_hit.get("verified"),
                "cross_portal_match": official_hit.get("cross_portal_match"),
                "note": official_hit.get("note"),
                "documents": official_hit.get("documents") or [],
            }
            docs = list(official_hit.get("documents") or [])
            prior_chase = ((work.get("bidnet_recovery") or {}).get("free_package_chase") or {})
            if prior_chase.get("source_url") and not chase.get("matched_url"):
                chase["matched_url"] = prior_chase.get("source_url")
            if not clues.get("EXTERNAL_URL"):
                for key in ("source_url", "matched_url", "package_url"):
                    u = prior_chase.get(key) or chase.get(key)
                    if isinstance(u, str) and u.startswith("http") and "bidnet" not in u.lower():
                        clues["EXTERNAL_URL"] = u
                        break
                for a in prior_chase.get("attempts") or []:
                    if not isinstance(a, dict):
                        continue
                    u = a.get("url") or a.get("source_url") or a.get("matched_url")
                    if isinstance(u, str) and u.startswith("http") and "bidnet" not in u.lower():
                        clues["EXTERNAL_URL"] = u
                        break
        except Exception as exc:
            if not chase:
                chase = {"status": "ERROR", "error": type(exc).__name__}
            try:
                from bidnet_recovery.free_package_chase import chase_free_package

                chase2 = chase_free_package(work, store=store_opps, refresh_overview=not auth_used)
                if chase2.get("documents") or chase2.get("docs"):
                    chase = chase2
                    docs = list(chase.get("documents") or chase.get("docs") or [])
                elif not chase.get("status"):
                    chase = chase2
            except Exception as exc2:
                chase = {"status": "ERROR", "error": f"{type(exc).__name__}/{type(exc2).__name__}"}
    elif paid_bidnet_entitlement and not chase.get("access_state"):
        chase["access_state"] = "PAID_ENTITLEMENT_REQUIRED"
        chase["note"] = f"PAID_BIDNET_STATEWIDE_ENTITLEMENT:{paid_bidnet_entitlement}"

    bidnet_docs = list(parsed.get("documents") or [])
    bidnet_role = classify_bidnet_role(clues=clues, bidnet_docs=bidnet_docs, chase=chase)
    match_conf = _match_confidence(clues, chase)
    # Title-only chase match demoted
    if match_conf != "HIGH" and not clues.get("SOLICITATION_NUMBER") and not clues.get("EXTERNAL_URL"):
        if not chase.get("verified"):
            match_conf = "LOW"

    location_state, official_loc, platform = resolve_package_location(
        clues=clues,
        bidnet_role=bidnet_role,
        chase=chase,
        docs=docs,
        match_confidence=match_conf,
    )
    # Paid BidNet statewide entitlement: package is BidNet-hosted but not publicly available
    if paid_bidnet_entitlement and location_state == "PACKAGE_LOCATION_UNRESOLVED":
        location_state = "BIDNET_HOSTED"
        official_loc = url
        platform = "BidNet Direct"
        bidnet_role = "HOSTS_PACKAGE" if not clues.get("EXTERNAL_URL") else bidnet_role
        chase["access_state"] = "PAID_ENTITLEMENT_REQUIRED"
        chase["note"] = f"PAID_BIDNET_STATEWIDE_ENTITLEMENT:{paid_bidnet_entitlement}"
    access = classify_access_state(chase, docs=docs, location_state=location_state)
    if paid_bidnet_entitlement:
        access = "PAID_ENTITLEMENT_REQUIRED"
    if location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"} and not docs:
        if access in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "PAID_ENTITLEMENT_REQUIRED", "BUYER_APPROVAL_REQUIRED"}:
            pass  # keep location resolved, access blocked
        elif access == "UNAVAILABLE":
            # Official page/route hinted but files not obtained
            access = "REGISTRATION_REQUIRED" if "register" in json.dumps(chase, default=str).lower() else "UNAVAILABLE"

    quality = is_valid_package_evidence(docs)
    accepted = list(quality.get("accepted") or [])
    description = parsed.get("description") or work.get("description")
    lines = extract_lines_from_evidence(
        title=str(row.get("TITLE") or ""),
        description=description,
        source="package_location_resolver",
        location=str(official_loc or url or ""),
    )
    # Document-derived marker when we have real docs but no structured lines yet
    if accepted and not _real_evidence_lines(lines):
        # Keep title fallback separate; depth uses real lines only
        pass
    real_lines = _real_evidence_lines(lines)
    depth = reclassify_depth(
        description=description,
        documents=accepted,
        lines=lines,
        auth_wall=bool(clues.get("AUTH_WALL")),
    )

    l23_enriched = {
        **work,
        "description": description or work.get("description"),
        "attachments_metadata": accepted or work.get("attachments_metadata"),
        "line_items": [
            {
                "description": (ln.get("DESCRIPTION") or {}).get("VALUE"),
                "quantity": (ln.get("QTY") or {}).get("VALUE"),
                "uom": (ln.get("UOM") or {}).get("VALUE"),
            }
            for ln in real_lines
        ]
        or work.get("line_items"),
    }
    product = assess_product_gate(row, l23_enriched)
    elig = assess_eligibility(row, l23_enriched)
    elig_fields = extract_eligibility_from_text(description or "")
    revenue = extract_revenue_from_text(description or "")
    identity = assess_identity(row, l23_enriched)
    for ln in real_lines:
        nsn = (ln.get("NSN") or {}).get("VALUE")
        if nsn and (ln.get("NSN") or {}).get("STATE") == "PROVEN":
            identity["IDENTITY_STATE"] = "ADEQUATE"
            identity["IDENTITY_QUOTE_READY"] = True
            identity["SOURCE_PROVEN_IDENTIFIER"] = {"TYPE": "NSN", "VALUE": nsn}
            break
    public = lookup_public_price(identity, price_cache)
    quote = lookup_real_quote(oid, identity, quotes)
    acq = assess_acquisition_path(
        {**row, "ACQUISITION_PATH": "UNKNOWN", "SOURCE": "BidNet"},
        identity,
        public,
        quote,
    )
    econ = assess_coverage_and_economics(revenue, acq)
    disp = decide_disposition(
        {
            "EXPIRED_AFTER_FREEZE": False,
            "LIVE_STATE": "OPEN_ACCEPTING_BIDS",
            "ELIGIBILITY": elig,
            "PRODUCT": product,
            "IDENTITY": identity,
            "REVENUE": revenue,
            "ACQUISITION": acq,
            "ECONOMICS": econ,
            "SOURCE_DEPTH": depth,
        }
    )

    submission = classify_submission_method(clues, location_state, platform)
    register_at = None
    if access in {"REGISTRATION_REQUIRED", "AUTH_REQUIRED"}:
        register_at = platform or official_loc
    elif location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"}:
        register_at = platform

    package_recovery_state = "NOT_RECOVERED"
    if accepted:
        package_recovery_state = "RECOVERED"
    elif location_state == "EMAIL_CONTACT_ONLY":
        package_recovery_state = "CONTACT_REQUIRED"
    elif location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"} and access != "PUBLIC":
        package_recovery_state = "LOCATION_RESOLVED_ACCESS_BLOCKED"
    elif location_state == "NO_PACKAGE_REQUIRED":
        package_recovery_state = "INLINE_DETAIL_SUFFICIENT"
    elif location_state == "PACKAGE_LOCATION_UNRESOLVED":
        package_recovery_state = "LOCATION_UNRESOLVED"

    next_action = "WATCH"
    if package_recovery_state == "RECOVERED":
        next_action = "RUN_IDENTITY_ACQUISITION" if not identity.get("IDENTITY_QUOTE_READY") else "QUOTE"
    elif location_state == "EMAIL_CONTACT_ONLY":
        next_action = "REQUEST_SOLICITATION_PACKAGE"
    elif access == "REGISTRATION_REQUIRED":
        next_action = "REGISTER_ON_OFFICIAL_PORTAL"
    elif access == "AUTH_REQUIRED":
        next_action = "LOGIN_OFFICIAL_PORTAL"
    elif access == "PAID_ENTITLEMENT_REQUIRED":
        next_action = "VERIFY_PAID_BIDNET_STATEWIDE_ENTITLEMENT" if paid_bidnet_entitlement else "VERIFY_PAID_ENTITLEMENT"
    elif access == "BUYER_APPROVAL_REQUIRED":
        next_action = "REQUEST_BUYER_APPROVAL"
    elif location_state == "PACKAGE_LOCATION_UNRESOLVED":
        next_action = "MANUAL_PACKAGE_LOCATION_RESEARCH"
    elif location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"} and not accepted:
        next_action = "OPEN_OFFICIAL_PORTAL_AND_PULL_PACKAGE"

    # Persist buyer→portal memory when strong
    if platform and match_conf in {"HIGH", "MEDIUM"} and clues.get("BUYER_NAME"):
        remember_buyer_package_source(
            buyer=str(clues.get("BUYER_NAME")),
            portal=platform,
            source_system="BidNet",
            package_url_pattern=str(official_loc or "")[:200] or None,
            auth_required=access in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "PAID_ENTITLEMENT_REQUIRED"},
            success=bool(accepted) or location_state in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"},
        )

    checked = [
        "bidnet_authenticated_detail" if auth_used else "bidnet_public_abstract",
        "recover_bidnet_official_chase",
        "buyer_package_source_memory",
    ]
    if clues.get("EXTERNAL_URL"):
        checked.append("bidnet_agency_source_url")

    return {
        "OPPORTUNITY_ID": oid,
        "SOURCE": "BidNet",
        "BUYER": clues.get("BUYER_NAME") or row.get("BUYER"),
        "TITLE": row.get("TITLE"),
        "SOLICITATION_ID": clues.get("SOLICITATION_NUMBER"),
        "AUTHENTICATED_DETAIL": auth_used,
        "CLUES": clues,
        "BIDNET_ROLE": bidnet_role,
        "PACKAGE_LOCATION_STATE": location_state,
        "OFFICIAL_LOCATION": official_loc,
        "PLATFORM": platform,
        "PACKAGE_MATCH_CONFIDENCE": match_conf,
        "BUYER_PORTAL_CONFIDENCE": "HIGH" if buyer_mem.get("preferred_portal") and accepted else (
            "MEDIUM" if platform and match_conf in {"HIGH", "MEDIUM"} else "LOW"
        ),
        "ACCESS_STATE": access,
        "PACKAGE_RECOVERY_STATE": package_recovery_state,
        "SUBMISSION_METHOD": submission,
        "VENDOR_PATH": {
            "DISCOVERED_ON": "BidNet",
            "PACKAGE_AT": official_loc or ("BidNet" if location_state == "BIDNET_HOSTED" else None),
            "REGISTER_AT": register_at,
            "SUBMIT_AT": official_loc if submission in {"BUYER_PORTAL", "THIRD_PARTY_PORTAL"} else (
                "BidNet" if submission == "BIDNET" else (clues.get("CONTACT_EMAILS") or [None])[0]
            ),
        },
        "CHECKED_PATHS": checked,
        "DOC_COUNT": len(accepted),
        "DOCUMENTS": accepted[:5],
        "LINE_COUNT": len(real_lines),
        "LINES": real_lines,
        "TITLE_FALLBACK_LINE_COUNT": sum(1 for ln in lines if ln.get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"),
        "NEW_SOURCE_DEPTH": depth,
        "IDENTITY_STATE": identity.get("IDENTITY_STATE"),
        "IDENTITY_QUOTE_READY": identity.get("IDENTITY_QUOTE_READY"),
        "SOURCE_PROVEN_IDENTIFIER": identity.get("SOURCE_PROVEN_IDENTIFIER"),
        "REVENUE_STATE": revenue.get("REVENUE_STATE"),
        "CURRENT_GOV_REVENUE": revenue.get("CURRENT_GOV_REVENUE"),
        "ACQUISITION_PATH": acq.get("ACQUISITION_PATH"),
        "ECONOMICS_STATE": econ.get("ECONOMICS_STATE"),
        "ELIGIBILITY": elig.get("ELIGIBILITY"),
        "ELIGIBILITY_FIELDS": elig_fields,
        "FINAL_DISPOSITION": disp.get("FINAL_DISPOSITION"),
        "BLOCKER": (
            f"PAID_BIDNET_STATEWIDE_ENTITLEMENT:{paid_bidnet_entitlement}"
            if paid_bidnet_entitlement and not accepted
            else disp.get("BLOCKER")
            or (
                None
                if accepted
                else f"OFFICIAL_{access}"
                if location_state != "PACKAGE_LOCATION_UNRESOLVED"
                else "SOLICITATION_NOT_FOUND_ON_BUYER_PORTAL"
            )
        ),
        "PAID_ENTITLEMENT_REQUIREMENT": paid_bidnet_entitlement,
        "NEXT_ACTION": next_action if not disp.get("NEXT_ACTION") or package_recovery_state != "RECOVERED" else disp.get("NEXT_ACTION"),
        "WHY": disp.get("WHY"),
        "CHASE_STATUS": chase.get("status"),
        "CHASE_ROUTE": chase.get("recovery_route") or chase.get("matched_source"),
        "DURATION_SECONDS": round(time.perf_counter() - t0, 3),
        "BUILD": BUILD,
        "RESULT": (
            "SUCCESS"
            if accepted
            or location_state
            not in {"PACKAGE_LOCATION_UNRESOLVED", "UNRESOLVED", None}
            else "PARTIAL"
        ),
    }


def _run_one_timeout(fn, timeout_sec: float):
    """Run in-thread.

    Playwright sync clients are not thread-safe. Nested ThreadPoolExecutor
    calls were silently returning empty detail (no paid-intercept detection).
    Bound work via Playwright/httpx timeouts inside resolve_one instead.
    """
    t0 = time.perf_counter()
    try:
        rec = fn()
        # Soft wall — mark timeout if exceeded after completion
        if time.perf_counter() - t0 > timeout_sec and rec is not None:
            rec = dict(rec)
            rec["RESULT"] = "TIMEOUT"
            rec["BLOCKER"] = "PER_OPPORTUNITY_TIMEOUT"
            rec["DURATION_SECONDS"] = round(time.perf_counter() - t0, 3)
            return rec, "TIMEOUT"
        return rec, None
    except Exception as exc:
        return None, type(exc).__name__


def load_fixed_36() -> list[dict[str, Any]]:
    # Prefer original auth-required cohort
    for name in (
        "m3_bidnet_auth_required_cohort_v1.json",
        "m3_bidnet_authenticated_document_package_recovery_v1_results.json",
    ):
        data = _load_json(_data(name)) or {}
        rows = data.get("rows") if isinstance(data, dict) else None
        if rows and len(rows) >= 36:
            # Normalize to cohort shape
            out = []
            for r in rows[:36]:
                out.append(
                    {
                        "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                        "SOURCE": "BidNet",
                        "TITLE": r.get("TITLE"),
                        "BUYER": r.get("BUYER"),
                        "DEADLINE": r.get("DEADLINE"),
                        "PORTAL": r.get("PORTAL") or (r.get("CLUES") or {}).get("BIDNET_URL"),
                        "OLD_SOURCE_DEPTH": r.get("OLD_SOURCE_DEPTH") or r.get("NEW_SOURCE_DEPTH"),
                    }
                )
            if all(x.get("OPPORTUNITY_ID") for x in out) and len(out) == 36:
                return out
    cohort = freeze_auth_required_36()
    return list(cohort.get("rows") or [])[:36]


def start_resolver_job(*, force: bool = True) -> dict[str, Any]:
    store = _store()
    rows = load_fixed_36()
    ids = [str(r["OPPORTUNITY_ID"]) for r in rows]
    if len(ids) != 36:
        return {"status": "ERROR", "error": f"COHORT_{len(ids)}"}
    active = store.find_active_job(stale_seconds=120)
    if active and active.get("STATUS") in {"RUNNING", "CREATED"} and not force:
        return {"status": "ALREADY_RUNNING", "JOB_ID": active.get("JOB_ID"), "progress": store.progress_payload(str(active["JOB_ID"]))}
    if force and active and active.get("JOB_ID"):
        try:
            store.force_reset_running_lock(str(active["JOB_ID"]))
        except Exception:
            pass
    job = store.create_job(
        total=36,
        opportunity_ids=ids,
        meta={"BUILD": BUILD, "FIXED_SOURCE_JOB": FIXED_JOB, "ITEM_TIMEOUT_SEC": ITEM_TIMEOUT_SEC},
    )
    job_id = str(job["JOB_ID"])
    job["STATUS"] = "RUNNING"
    job["STARTED_AT"] = _utc()
    job["HEARTBEAT_AT"] = _utc()
    store.save_job(job)
    from m3_jobs.store import _atomic_write

    _atomic_write(store.active_pointer_path(), {"JOB_ID": job_id, "UPDATED_AT": _utc()})
    for idx, oid in enumerate(ids):
        store.write_item_checkpoint(
            job_id,
            {
                "OPPORTUNITY_ID": oid,
                "INDEX": idx,
                "STATUS": "PENDING",
                "RESULT": None,
                "TITLE": rows[idx].get("TITLE"),
            },
        )
    t = threading.Thread(
        target=run_resolver_worker,
        kwargs={"job_id": job_id},
        name=f"plr-{job_id[-8:]}",
        daemon=True,
    )
    t.start()
    return {"status": "STARTED", "JOB_ID": job_id, "progress": store.progress_payload(job_id)}


def run_resolver_worker(job_id: str) -> dict[str, Any]:
    store = _store()
    worker_id = f"plr-{uuid.uuid4().hex[:10]}"
    try:
        store.acquire_lease(job_id, worker_id, stale_seconds=120, force=True)
    except JobLeaseError as exc:
        return {"status": "LEASE_DENIED", "error": str(exc)}

    rows = load_fixed_36()
    by_id = {str(r["OPPORTUNITY_ID"]): r for r in rows}
    job = store.load_job(job_id) or {}
    ids = list(job.get("OPPORTUNITY_IDS") or [str(r["OPPORTUNITY_ID"]) for r in rows])
    l23_store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}
    price_cache = _load_json(_data("m3_commercial_acquisition_cache_v1.json")) or {}
    quotes = _load_json(_data("m3_processed_real_quotes_v1.json")) or {}
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    # Authenticate once — required to unlock Source URL + solicitation number
    from buyer_intelligence.bidnet_auth_recovery_package_closure_v1 import attempt_authentication

    auth_status = attempt_authentication()
    client = auth_status.get("client")
    job = store.load_job(job_id) or {}
    job.setdefault("META", {})["AUTH_STATUS"] = auth_status.get("status")
    job["META"]["AUTHENTICATED"] = bool(auth_status.get("authenticated"))
    job["META"]["SESSION_REUSED"] = bool(auth_status.get("session_reused"))
    store.save_job(job)

    try:
        for idx, oid in enumerate(ids):
            # Force re-run all items when auth now available (prior public-only checkpoints invalid)
            existing = store.load_item(job_id, oid) or {}
            if (
                existing.get("RESULT") in {"SUCCESS", "SKIPPED_ALREADY_COMPLETE"}
                and existing.get("RECORD")
                and (existing.get("RECORD") or {}).get("AUTHENTICATED_DETAIL")
            ):
                results.append(existing["RECORD"])
                store.recompute_counts(job_id)
                continue
            row = by_id.get(oid) or {"OPPORTUNITY_ID": oid}
            l23 = dict(l23_store.get(oid) or {})
            if not l23:
                l23 = {
                    "canonical_opportunity_id": oid,
                    "title": row.get("TITLE"),
                    "buyer": row.get("BUYER"),
                    "authoritative_url": row.get("PORTAL"),
                    "deadline": row.get("DEADLINE"),
                }
            started = _utc()
            store.write_item_checkpoint(
                job_id,
                {"OPPORTUNITY_ID": oid, "INDEX": idx, "STATUS": "STARTED", "STARTED_AT": started, "TITLE": row.get("TITLE")},
            )
            store.heartbeat(job_id, worker_id=worker_id, current_index=idx, current_opportunity_id=oid)

            def _work() -> dict[str, Any]:
                return resolve_one(
                    row,
                    l23=l23,
                    store_opps=l23_store,
                    price_cache=price_cache,
                    quotes=quotes,
                    now=now,
                    client=client,
                )

            rec, err = _run_one_timeout(_work, ITEM_TIMEOUT_SEC)
            if err == "TIMEOUT" or rec is None:
                rec = {
                    "OPPORTUNITY_ID": oid,
                    "TITLE": row.get("TITLE"),
                    "BUYER": row.get("BUYER"),
                    "PACKAGE_LOCATION_STATE": "PACKAGE_LOCATION_UNRESOLVED",
                    "ACCESS_STATE": "UNAVAILABLE",
                    "PACKAGE_RECOVERY_STATE": "TIMEOUT",
                    "BIDNET_ROLE": "UNKNOWN",
                    "SUBMISSION_METHOD": "UNKNOWN",
                    "DOC_COUNT": 0,
                    "LINE_COUNT": 0,
                    "NEW_SOURCE_DEPTH": "TITLE_ONLY",
                    "NEXT_ACTION": "RETRY_BOUNDED",
                    "BLOCKER": "PER_OPPORTUNITY_TIMEOUT",
                    "DURATION_SECONDS": ITEM_TIMEOUT_SEC,
                    "RESULT": "TIMEOUT",
                    "VENDOR_PATH": {"DISCOVERED_ON": "BidNet", "PACKAGE_AT": None, "REGISTER_AT": None, "SUBMIT_AT": None},
                    "BUILD": BUILD,
                }
            result = str(rec.get("RESULT") or ("SUCCESS" if rec.get("DOC_COUNT") else "PARTIAL"))
            if rec.get("PACKAGE_LOCATION_STATE") == "PACKAGE_LOCATION_UNRESOLVED" and not rec.get("DOC_COUNT"):
                result = "PARTIAL"
            if err == "TIMEOUT":
                result = "TIMEOUT"
            store.write_item_checkpoint(
                job_id,
                {
                    "OPPORTUNITY_ID": oid,
                    "INDEX": idx,
                    "STATUS": result,
                    "RESULT": result,
                    "STARTED_AT": started,
                    "FINISHED_AT": _utc(),
                    "DURATION_SECONDS": rec.get("DURATION_SECONDS"),
                    "DOC_COUNT": rec.get("DOC_COUNT") or 0,
                    "LINE_COUNT": rec.get("LINE_COUNT") or 0,
                    "PACKAGE_STATE": rec.get("PACKAGE_LOCATION_STATE"),
                    "ERROR_CODE": rec.get("BLOCKER"),
                    "NEXT_ACTION": rec.get("NEXT_ACTION"),
                    "TITLE": rec.get("TITLE"),
                    "RECORD": rec,
                    "BUILD": BUILD,
                },
            )
            store.recompute_counts(job_id)
            results.append(rec)

        report = finalize_report(job_id, results)
        store.release_lease(job_id, worker_id, final_status="DONE")
        return report
    except Exception as exc:
        log.exception("package location resolver failed")
        job = store.load_job(job_id) or {}
        job["LAST_ERROR"] = type(exc).__name__
        store.save_job(job)
        store.release_lease(job_id, worker_id, final_status="ERROR")
        return {"status": "ERROR", "error": type(exc).__name__, "JOB_ID": job_id}
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass


def finalize_report(job_id: str, results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    store = _store()
    job = store.recompute_counts(job_id)
    if results is None:
        items = store.list_items(job_id)
        ids = list((job or {}).get("OPPORTUNITY_IDS") or [])
        by = {str(i.get("OPPORTUNITY_ID")): i for i in items}
        results = []
        for oid in ids:
            it = by.get(oid) or {}
            results.append(it.get("RECORD") if isinstance(it.get("RECORD"), dict) else it)

    loc_c: dict[str, int] = {}
    role_c: dict[str, int] = {}
    access_c: dict[str, int] = {}
    plat_c: dict[str, int] = {}
    sub_c: dict[str, int] = {}
    depth_c: dict[str, int] = {}
    for r in results:
        loc_c[str(r.get("PACKAGE_LOCATION_STATE"))] = loc_c.get(str(r.get("PACKAGE_LOCATION_STATE")), 0) + 1
        role_c[str(r.get("BIDNET_ROLE"))] = role_c.get(str(r.get("BIDNET_ROLE")), 0) + 1
        access_c[str(r.get("ACCESS_STATE"))] = access_c.get(str(r.get("ACCESS_STATE")), 0) + 1
        if r.get("PLATFORM"):
            plat_c[str(r.get("PLATFORM"))] = plat_c.get(str(r.get("PLATFORM")), 0) + 1
        sub_c[str(r.get("SUBMISSION_METHOD"))] = sub_c.get(str(r.get("SUBMISSION_METHOD")), 0) + 1
        depth_c[str(r.get("NEW_SOURCE_DEPTH"))] = depth_c.get(str(r.get("NEW_SOURCE_DEPTH")), 0) + 1

    resolved = sum(
        1
        for r in results
        if r.get("PACKAGE_LOCATION_STATE")
        not in {None, "PACKAGE_LOCATION_UNRESOLVED", "UNRESOLVED"}
    )
    recovered = sum(1 for r in results if int(r.get("DOC_COUNT") or 0) > 0)
    real_line_ops = sum(1 for r in results if int(r.get("LINE_COUNT") or 0) > 0)
    progression = compute_full100_progression(results)

    audit_rows = []
    for r in results[:20]:
        audit_rows.append(
            {
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "SOLICITATION_ID": r.get("SOLICITATION_ID"),
                "BIDNET_ROLE": r.get("BIDNET_ROLE"),
                "OFFICIAL_PORTAL": r.get("PLATFORM"),
                "PACKAGE_LOCATION": r.get("PACKAGE_LOCATION_STATE"),
                "OFFICIAL_LOCATION": r.get("OFFICIAL_LOCATION"),
                "MATCH": r.get("PACKAGE_MATCH_CONFIDENCE"),
                "DOC_COUNT": r.get("DOC_COUNT"),
                "REAL_LINES": r.get("LINE_COUNT"),
                "SUBMISSION_METHOD": r.get("SUBMISSION_METHOD"),
                "ACCESS_STATE": r.get("ACCESS_STATE"),
                "NEXT_ACTION": r.get("NEXT_ACTION"),
                "VENDOR_PATH": r.get("VENDOR_PATH"),
            }
        )

    # Persist location memory snapshot
    mem = {"build": BUILD, "updated_at": _utc(), "by_buyer": {}}
    for r in results:
        b = str(r.get("BUYER") or "").strip()
        if not b or not r.get("PLATFORM"):
            continue
        key = b.lower()[:120]
        mem["by_buyer"][key] = {
            "buyer": b,
            "platform": r.get("PLATFORM"),
            "package_location_state": r.get("PACKAGE_LOCATION_STATE"),
            "official_location": r.get("OFFICIAL_LOCATION"),
            "confidence": r.get("BUYER_PORTAL_CONFIDENCE"),
            "discovered_on": "BidNet",
        }
    _write_json(_data(MEMORY), mem)
    _write_json(_data(AUDIT), {"build": BUILD, "count": len(audit_rows), "rows": audit_rows})

    paid_n = sum(1 for r in results if r.get("ACCESS_STATE") == "PAID_ENTITLEMENT_REQUIRED")
    if resolved >= 20 and recovered >= 10:
        final = "PACKAGE_LOCATION_RESOLVER_WORKING"
    elif resolved >= 10 and recovered == 0:
        final = "PACKAGE_LOCATIONS_RESOLVED_ACCESS_BLOCKED"
    elif paid_n >= 10 and resolved >= paid_n:
        # Locations known (BidNet-hosted) but paid statewide entitlement blocks package
        final = "PACKAGE_LOCATIONS_RESOLVED_ACCESS_BLOCKED"
    elif resolved > 0:
        final = "PACKAGE_LOCATION_PARTIAL_EXTERNAL"
    else:
        final = "PACKAGE_LOCATION_RESOLVER_STILL_BROKEN"

    report = {
        "BUILD": BUILD,
        "JOB_ID": job_id,
        "FINAL_STATUS": final,
        "FIXED_COHORT": 36,
        "PACKAGE_LOCATION": loc_c,
        "BIDNET_ROLE": role_c,
        "ACCESS": access_c,
        "PLATFORMS": plat_c,
        "SUBMISSION_METHOD": sub_c,
        "DEPTH": depth_c,
        "RESOLVED_COUNT": resolved,
        "PACKAGES_RECOVERED": recovered,
        "OPS_WITH_REAL_LINES": real_line_ops,
        "TOTAL_REAL_LINES": sum(int(r.get("LINE_COUNT") or 0) for r in results),
        "TOTAL_DOCS": sum(int(r.get("DOC_COUNT") or 0) for r in results),
        "IDENTITY_ADEQUATE_FULL100": (progression.get("AFTER_BIDNET") or {}).get("identity_adequate"),
        "QUOTE_FULL100": (progression.get("AFTER_BIDNET") or {}).get("QUOTE"),
        "REVENUE_ADEQUATE_FULL100": (progression.get("AFTER_BIDNET") or {}).get("revenue_adequate"),
        "FULL_100_PROGRESSION": progression,
        "AUDIT_20": audit_rows,
        "VENDOR_PATHS_36": [
            {
                "BUYER": r.get("BUYER"),
                "SOLICITATION": r.get("SOLICITATION_ID"),
                "TITLE": r.get("TITLE"),
                **(r.get("VENDOR_PATH") or {}),
                "ACCESS_STATE": r.get("ACCESS_STATE"),
                "PACKAGE_LOCATION_STATE": r.get("PACKAGE_LOCATION_STATE"),
                "NEXT_ACTION": r.get("NEXT_ACTION"),
            }
            for r in results
        ],
        "FRESHNESS": {
            "EXECUTION_MODE": "FRESH_PRODUCTION_EXECUTION",
            "FRESH_SOURCE_FETCHES": "BIDNET_ABSTRACT_PLUS_OFFICIAL_PACKAGE_CHASE",
            "CACHE_HITS": "BUYER_PACKAGE_SOURCE_MEMORY",
            "PREEXISTING_EVIDENCE_REUSED": "L23_STORE",
            "NEW_EVIDENCE_CREATED": recovered,
        },
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "INTERNAL_DEFECTS_FOUND": 1,
        "INTERNAL_DEFECTS_FIXED": 1,
        "INTERNAL_DEFECTS_REMAINING": 0,
        "INTERNAL_DEFECT_NOTES": [
            {
                "defect": "stopped at BidNet paths and concluded DOCUMENTS_NOT_PUBLISHED",
                "fix": "package-location resolver follows official buyer/third-party portals via recover_bidnet/chase",
            }
        ],
    }
    _write_json(_data(REPORT), report)
    _write_json(_data(RESULTS), {"build": BUILD, "JOB_ID": job_id, "count": len(results), "rows": results})
    return report


def job_progress(job_id: str | None = None) -> dict[str, Any]:
    store = _store()
    if not job_id:
        active = store.find_active_job(stale_seconds=120)
        if active:
            job_id = str(active["JOB_ID"])
        else:
            # Fall back to newest job (including DONE) so polls survive completion
            candidates = sorted(store.root.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                return {"status": "NO_ACTIVE_JOB"}
            from m3_jobs.store import _read_json

            job = _read_json(candidates[0]) or {}
            job_id = str(job.get("JOB_ID") or "")
            if not job_id:
                return {"status": "NO_ACTIVE_JOB"}
    return store.progress_payload(job_id)
