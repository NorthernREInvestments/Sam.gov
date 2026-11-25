"""Authoritative staged procurement inventory (offline; read-only inputs; zero external calls).

One canonical identity model across BidNet (most recent legacy acquisition), NextBid and
free public sources. Every input record receives exactly one traceable disposition:
retired (with reason), duplicate (of a canonical record), or active (with an owner queue).
Lifecycle status is computed against the runtime clock in the solicitation's own timezone.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from m3.config import get_settings
from m3.research.classify import agency_codes_from_record, assess_product_presence
from m3.research.documents import unrelated_site_document
from m3.research.owner_deliverables import build_owner_deliverables
from m3.research.procurement_requirements import (
    assess_economics,
    assess_execution_feasibility,
    assess_reseller_eligibility,
    assess_sourcing_readiness,
    classify_evidence_gap,
    owner_blocker_codes,
    qualification,
    title_only_unresolved,
    extract_requirements,
)

BUILD = "m3-master-repair-staged-inventory-v1"
LIFECYCLE_STATES = (
    "VERIFIED_OPEN", "LIKELY_OPEN", "STATUS_UNKNOWN", "EXPIRED", "CLOSED", "CANCELLED", "AWARDED", "DUPLICATE",
)
ACTIVE_LIFECYCLE = frozenset({"VERIFIED_OPEN", "LIKELY_OPEN", "STATUS_UNKNOWN"})
OWNER_QUEUES = (
    "actionable", "easy_registration", "supplier_research_ready", "commercial_research_ready", "access_required",
    "document_verification", "financing_verification", "status_verification", "blocked", "archived",
)
# Hosts already confirmed inaccessible for free automated retrieval — never re-probed here.
# BidNet: robots.txt HTTP 403 + solicitation pages return auth-wall HTML for anonymous clients.
_ACCESS_BLOCKED_HOSTS = (
    "procurement.opengov.com", "ssl.doas.state.ga.us", "doas.state.ga.us",
    "bidnetdirect.com", "www.bidnetdirect.com",
)
_ACCESS_BLOCKED_STATUSES = frozenset({"restricted", "robots_disallow"})
# Durable one-shot failure record — written once; subsequent runs never re-probe these hosts
_BIDNET_ACCESS_BLOCK = {
    "host": "bidnetdirect.com",
    "status": "robots_disallow",
    "http_status": 403,
    "detail": "robots.txt HTTP 403 Forbidden; retained solicitation_page bodies are auth-walled "
              "(Registered members only / abstractRegisterNow). Listing snapshot titles/deadlines remain valid evidence.",
    "do_not_retry": True,
    "inventory_policy": "process_retained_listings_offline; do_not_mark_closed_for_access_block",
}
FEDERAL_SOURCES = frozenset({"sam.gov", "dibbs"})
# Latest complete BidNet acquisition (statewide open-bids list walk, 1,115 pages): run end time
BIDNET_LATEST_RUN_END = "2026-10-12T19:24:27+00:00"
BIDNET_PRIOR_FEED_SEEN = "2026-10-02T21:02:38+00:00"
CHUNK = 2000

_STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver", "PR": "America/Puerto_Rico",
}
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}
# Latest offset in the continental US + HI: a date-only deadline is open until its end of day here
_LATEST_US_TZ = "Pacific/Honolulu"
# Titles that are document links, unrendered templates, or civil-service personnel notices
_JUNK_TITLE = re.compile(r"(?i)^\s*(?:https?://|www\.)|\$\{[^}]*\}|\{\{[^}]*\}\}|^\s*effective\s+date:.*provisional\s+status|^\s*$"
                         r"|\buse\s+strict\b|\bfunction\s*\(|=>|;\s*var\s+\w+\s*=")
_NON_SOLICITATION_TYPES = frozenset({
    "GENERAL_PROCUREMENT_INFORMATION", "PROCUREMENT_PORTAL", "UNRELATED_CONTENT", "SUPPORTING_DOCUMENT",
    "VENDOR_REGISTRATION", "PROCUREMENT_LISTING",
})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[staged-inventory] {msg}", flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return n


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --- Normalization -----------------------------------------------------------------------

def state_code(*values: Any) -> str | None:
    for v in values:
        s = str(v or "").strip()
        if not s:
            continue
        if len(s) == 2 and s.upper() in _STATE_TZ:
            return s.upper()
        code = _STATE_NAMES.get(s.lower())
        if code:
            return code
        m = re.search(r"\b([A-Z]{2})\b(?:\s+\d{5})?\s*$", s)
        if m and m.group(1) in _STATE_TZ:
            return m.group(1)
    return None


_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
# Daylight/standard abbreviations are unambiguous offsets (BidNet prints times in the viewer's zone)
_TZ_ABBREV = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8,
              "AKDT": -8, "AKST": -9, "HST": -10, "UTC": 0, "GMT": 0}
_TZ_ABBREV_RE = re.compile(r"\d\s*(?:[AP]\.?M\.?)?\s*\(?\b(" + "|".join(_TZ_ABBREV) + r")\b\)?", re.I)


def _parse_us_datetime(s: str) -> datetime | None:
    """'11/30/2026 2:00 PM', '11/30/2026', 'November 30, 2026 2:00 PM' → naive local datetime."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s*,?\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]M)?)?", s, re.I)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh, mi, ap = m.group(4), m.group(5), m.group(6)
    else:
        m = re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})(?:\s*,?\s*(?:at\s+)?(\d{1,2}):(\d{2})(?::\d{2})?\s*([AP]\.?M\.?)?)?", s, re.I)
        if not m or m.group(1)[:3].lower() not in _MONTHS:
            return None
        mo, d, y = _MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
        hh, mi, ap = m.group(4), m.group(5), m.group(6)
    h = int(hh) if hh else 0
    ap = (ap or "").replace(".", "").upper()
    if ap == "PM" and h < 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    try:
        return datetime(y, mo, d, h, int(mi) if mi else 0)
    except ValueError:
        return None


_UTC_NAMES = {"UTC", "ETC/UTC", "GMT", "Z", "+00:00"}
_PAGE_DATE = r"(?:[A-Z][a-z]{2},?\s+)?(?:\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})"
_PAGE_TIME = r"(?:,?\s*(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)?(?:\s*\(?(?:[ECMP][SD]T|AK[SD]T|HST)\)?)?"
_PAGE_DEADLINE_RES = (
    # "Solicitation Due Date: 12/17/26 Solicitation Due Time: 1:30 PM"
    re.compile(r"(?i)\bDue\s+Date:?\s*(?P<d>\d{1,2}/\d{1,2}/\d{2,4})\s+(?:\w+\s+){0,2}Due\s+Time:?\s*(?P<t>\d{1,2}:\d{2}\s*[AP]M)"),
    re.compile(r"(?:Bid\s+|Solicitation\s+|Proposal\s+|Response\s+)?(?:Closing|Close)\s+Date(?:\s*(?:/|and)\s*Time)?:?\s*(?P<v>"
               + _PAGE_DATE + _PAGE_TIME + ")", re.I),
    re.compile(r"(?:Submission|Proposals?|Bids?|Responses?|Offers?)\s+(?:are\s+)?(?:Due|Deadline)(?:\s+Date)?(?:\s*(?:/|and)\s*Time)?:?\s*"
               r"(?:by\s+|on\s+)?(?P<v>" + _PAGE_DATE + _PAGE_TIME + ")", re.I),
)
# Periscope/BidBuy often prints only "Bid Opening Date" as the sealed-bid due/open instant (not contract Begin/End)
_PAGE_BID_OPENING = re.compile(
    r"(?i)\bBid\s+Opening\s+Date:?\s*(?P<v>\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp][Mm])?)")
_PAGE_NON_SUBMISSION = re.compile(
    r"(?i)\b(?:Available\s+Date|Begin\s+Date|End\s+Date|Inquiry\s+Deadline|Questions?\s+Deadline|"
    r"Pre[- ]?Bid|Publication\s+Date)\b")


def page_stated_deadline(text: str) -> str | None:
    """Bid submission deadline as printed on an official page. Never uses Available/Begin/End/performance dates."""
    t = re.sub(r"\s+", " ", html.unescape(text or ""))
    for rx in _PAGE_DEADLINE_RES:
        m = rx.search(t)
        if not m:
            continue
        # Reject matches that are clearly non-submission labels immediately before the value
        pre = t[max(0, m.start() - 24): m.start()]
        if _PAGE_NON_SUBMISSION.search(pre):
            continue
        if "d" in m.groupdict() and m.group("d"):
            d = m.group("d")
            mo, dd, yy = d.split("/")
            v = f"{mo}/{dd}/{yy if len(yy) == 4 else '20' + yy} {m.group('t')}"
        else:
            v = re.sub(r"^[A-Z][a-z]{2},?\s+(?=[A-Z])", "", m.group("v").strip())
        if _parse_us_datetime(v):
            return v
    # Only when no Closing/Due label exists: Periscope Bid Opening is the operative sealed-bid deadline
    if not re.search(r"(?i)\b(?:Closing|Due)\s+Date\b|\bSubmission\s+Deadline\b", t):
        m = _PAGE_BID_OPENING.search(t)
        if m and _parse_us_datetime(m.group("v")):
            return m.group("v")
    return None


def parse_deadline(raw: Any, *, tz_name: str | None, state: str | None) -> dict[str, Any]:
    """Deadline instant in UTC plus how its timezone was established.

    Offsets in the value are exact. Naive values use the record's timezone, else its state's
    timezone; a date-only value with no locality is held open until end of day in the latest US
    timezone so a still-open record is never expired early.
    """
    s = str(raw or "").strip()
    if not s:
        return {"deadline_at": None, "deadline_basis": "missing"}
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = _parse_us_datetime(s)
        if dt is None:
            return {"deadline_at": None, "deadline_basis": "unparseable", "deadline_raw": s[:60]}
    if dt.year >= 2090 or dt.year < 2000:
        return {"deadline_at": None, "deadline_basis": "sentinel_or_invalid", "deadline_raw": s[:60]}
    placeholder = dt.tzinfo is not None and dt.utcoffset() == timedelta(0) and dt.time() in (dtime(0, 0), dtime(12, 0))
    if dt.tzinfo is not None and not placeholder:
        return {"deadline_at": dt.astimezone(timezone.utc), "deadline_basis": "explicit_offset"}
    if placeholder:
        # Aggregator feeds encode a bare date as 00:00Z / 12:00Z; the real closing time is unknown
        dt = dt.replace(tzinfo=None)
        s = s[:10]
        if str(tz_name or "").upper() in _UTC_NAMES:
            tz_name = None
    ab = _TZ_ABBREV_RE.search(s)
    if ab:
        return {"deadline_at": dt.replace(tzinfo=timezone(timedelta(hours=_TZ_ABBREV[ab.group(1).upper()]))).astimezone(timezone.utc),
                "deadline_basis": "explicit_abbreviation", "deadline_timezone": ab.group(1).upper()}
    date_only = dt.time() in (dtime(0, 0), dtime(23, 59, 59)) or len(s) <= 10
    zone = tz_name if tz_name and _valid_tz(tz_name) else (_STATE_TZ.get(state or "") if state else None)
    basis = "record_timezone" if tz_name and zone == tz_name else ("state_timezone" if zone else "latest_us_timezone")
    zone = zone or _LATEST_US_TZ
    if date_only:
        dt = datetime.combine(dt.date(), dtime(23, 59, 59))
        basis += "_end_of_day"
    return {"deadline_at": dt.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc), "deadline_basis": basis,
            "deadline_timezone": zone}


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _norm_text(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


_URL_NOISE_PARAMS = re.compile(r"^(?:utm_\w+|parenturl|external|ref|referrer|source|fbclid|gclid|lang|locale|sort|page|skip|top)$")


def _norm_url(u: Any) -> str:
    s = str(u or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^https?://(www\.)?", "", s)
    s = re.sub(r"#.*$", "", s)
    path, _, query = s.partition("?")
    path = re.sub(r"/(abstract|view|details?)/?$", "", path).rstrip("/")
    # Record ids often live in the query (rampla ?id=, commbuys ?docId=); tracking params do not identify
    params = sorted(p for p in query.split("&") if "=" in p and not _URL_NOISE_PARAMS.match(p.split("=", 1)[0]))
    return path + ("?" + "&".join(params) if params else "")


def _norm_sol(s: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def identity_keys(rec: dict[str, Any]) -> list[str]:
    """Keys that establish the same solicitation across sources (any shared key merges)."""
    keys: list[str] = []
    sol = _norm_sol(rec.get("solicitation_number"))
    buyer = _norm_text(rec.get("buyer"))[:40]
    state = rec.get("state") or ""
    if len(sol) >= 9 and re.search(r"\d{6,}", sol):
        keys.append(f"sol:{sol}")
    elif len(sol) >= 4 and (buyer or state):
        keys.append(f"sol:{sol}|{state or buyer}")
    url = _norm_url(rec.get("official_url"))
    if url and len(url) > 20 and not re.search(r"/(search|opportunities|bids|solicitations)/?$", url):
        keys.append(f"url:{url}")
    t = _norm_text(rec.get("title"))
    dl = rec.get("deadline_at")
    if t and len(t) >= 12 and dl and (buyer or state):
        keys.append(f"tbd:{t[:80]}|{state or buyer}|{str(dl)[:10]}")
    return keys


def canonical_id(keys: list[str], fallback: str) -> str:
    base = sorted(keys)[0] if keys else fallback
    return "STG-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# --- Source loaders ----------------------------------------------------------------------

def _staged(**kw: Any) -> dict[str, Any]:
    rec = {
        "source_record_id": None, "source": None, "source_family": None, "provider_record_id": None,
        "solicitation_number": None, "title": None, "buyer": None, "state": None, "official_url": None,
        "posted_at": None, "deadline_raw": None, "deadline_tz_hint": None, "first_seen_at": None,
        "last_seen_at": None, "declared_status": None, "notice_type_declared": None, "record_kind": "SOLICITATION",
        "procurement_kind": None, "agency_codes": [], "attachments": [], "provenance": {}, "acquisition_membership": None,
        "description": "", "document_text": "",
    }
    rec.update(kw)
    return rec


def load_legacy_bidnet(paths: dict[str, Path | None]) -> dict[str, Any]:
    """Legacy canonical store → BidNet records with latest-acquisition membership evidence."""
    store_p, ck_p, pre_p = paths.get("store"), paths.get("harvest_checkpoint"), paths.get("prerun_store")
    if not store_p or not store_p.exists():
        return {"available": False, "reason": f"legacy store not found: {store_p}", "records": []}
    store = json.loads(store_p.read_text(encoding="utf-8"))
    ops = store.get("opportunities") or {}
    applied: set[str] = set()
    ck_meta: dict[str, Any] = {}
    if ck_p and ck_p.exists():
        ck = json.loads(ck_p.read_text(encoding="utf-8"))
        applied = set(ck.get("applied_ids") or [])
        ck_meta = {"complete": ck.get("complete"), "pages": len(ck.get("completed_pages") or []),
                   "applied": len(applied), "stats": ck.get("stats"), "updated_at": ck.get("updated_at")}
    pre_dated: set[str] = set()
    if pre_p and pre_p.exists():
        pre = json.loads(pre_p.read_text(encoding="utf-8")).get("opportunities") or {}
        pre_dated = {k for k, v in pre.items() if v.get("platform") == "live_bidnet" and v.get("deadline")}
        del pre
    out: list[dict[str, Any]] = []
    for oid, r in ops.items():
        platform = r.get("platform")
        pv = r.get("source_provenance") or []
        retrieved = next((p.get("retrieved_at") for p in pv if p.get("retrieved_at")), None)
        if platform == "live_bidnet":
            if oid in applied:
                member = "LATEST_ACQUISITION_VERIFIED"
            elif oid in pre_dated:
                member = "LATEST_ACQUISITION_PROBABLE"
            else:
                member = "LEGACY_NOT_IN_LATEST"
        else:
            member = "LEGACY_OTHER_PLATFORM"
        assessed = (r.get("discovery_freshness_detail") or {}).get("assessed_at")
        br = r.get("bidnet_recovery") or {}
        url = r.get("authoritative_url") or ""
        pg = re.search(r"purchasingGroupId=(\d+)", url)
        out.append(_staged(
            source_record_id=f"legacy:{oid}",
            source="bidnet" if platform == "live_bidnet" else f"legacy:{platform or 'unknown'}",
            source_family="bidnet_legacy" if platform == "live_bidnet" else "legacy_other",
            provider_record_id=r.get("solicitation_event_id"),
            solicitation_number=r.get("solicitation_event_id"),
            title=r.get("title"), buyer=r.get("buyer"),
            state=state_code(r.get("jurisdiction"), r.get("buyer"), br.get("location")),
            official_url=url, posted_at=br.get("issue_date"),
            deadline_raw=r.get("deadline"), deadline_tz_hint=r.get("timezone") or r.get("deadline_timezone"),
            first_seen_at=retrieved or assessed or BIDNET_PRIOR_FEED_SEEN,
            last_seen_at=(BIDNET_LATEST_RUN_END if member.startswith("LATEST") else (assessed or BIDNET_PRIOR_FEED_SEEN)),
            declared_status=r.get("deadline_status"),
            description=str(r.get("description") or ""),
            attachments=[a for a in (r.get("attachments_metadata") or []) if isinstance(a, dict)][:10],
            acquisition_membership=member,
            provenance={"file": str(store_p), "canonical_opportunity_id": oid, "purchasing_group_id": pg.group(1) if pg else None,
                        "feeds": sorted({str(p.get("feed") or p.get("source") or p.get("source_id")) for p in pv})[:6],
                        "legacy_freshness": r.get("freshness"), "legacy_funnel": r.get("current_funnel_state"),
                        "legacy_classification": r.get("product_service_classification")},
        ))
    return {"available": True, "records": out, "store_updated_at": store.get("updated_at"), "checkpoint": ck_meta,
            "prerun_dated_ids": len(pre_dated), "files": {k: str(v) for k, v in paths.items() if v}}


def _bidnet_vault_abstracts() -> dict[str, dict[str, Any]]:
    """Offline: re-parse any retained BidNet solicitation_page bodies from the evidence vault."""
    try:
        from m3.research import evidence_vault
        from m3.research.bidnet_parse import parse_bidnet_abstract
    except Exception:
        return {}
    vault = get_settings().evidence_vault_root()
    root = vault / "records" / "bidnet"
    if not root.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for mp in root.rglob("*.json"):
        try:
            man = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rid = str(man.get("record_id") or "")
        if not rid.startswith("bidnet:"):
            continue
        pid = rid.split(":", 1)[-1]
        vers = (man.get("kinds") or {}).get("solicitation_page") or []
        if not vers:
            continue
        body = evidence_vault.get_body(vers[-1]["sha256"], root=vault)
        if not body:
            continue
        html = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        ab = parse_bidnet_abstract(html, detail_url=vers[-1].get("url"))
        ab["provider_record_id"] = pid
        ab["from_vault"] = True
        out[pid] = ab
    return out


def load_bidnet_snapshot(root: Path | None) -> dict[str, Any]:
    """Fresh public BidNet open-bids snapshot (``bidnet_acquire``), merged with any public abstracts.

    Every listing card is kept and evaluated from retained title/deadline evidence even when further
    BidNet page access is blocked. Vault solicitation pages are re-parsed offline when present.
    """
    snap = root / "snapshot.jsonl" if root else None
    if not snap or not snap.exists():
        return {"available": False, "reason": f"bidnet snapshot not found: {snap}", "records": [], "complete": False}
    cov_p = root / "coverage.json"
    cov = json.loads(cov_p.read_text(encoding="utf-8")) if cov_p.exists() else {}
    abstracts: dict[str, dict[str, Any]] = dict(_bidnet_vault_abstracts())
    ab_p = root / "abstracts" / "abstracts.jsonl"
    if ab_p.exists():
        for row in _read_jsonl(ab_p):
            pid = str(row.get("provider_record_id"))
            # Prefer the stored abstract file when both exist (explicit fetch)
            abstracts[pid] = row
    out: list[dict[str, Any]] = []
    vault_hits = 0
    for r in _read_jsonl(snap):
        pid = str(r.get("provider_record_id") or "")
        ab = abstracts.get(pid) or {}
        if ab.get("from_vault"):
            vault_hits += 1
        docs = [{"name": d.get("document_name"), "url": d.get("document_url"), "status": d.get("retrieval_status")}
                for d in (ab.get("documents") or [])][:10]
        out.append(_staged(
            source_record_id=f"bidnet:{pid or r.get('official_url')}", source="bidnet", source_family="bidnet",
            provider_record_id=pid or None, solicitation_number=ab.get("solicitation_number"),
            title=r.get("title"), buyer=ab.get("buyer") or r.get("purchasing_group"),
            state=state_code(r.get("location")), official_url=r.get("official_url"), posted_at=r.get("posted_raw"),
            # The solicitation page states the closing time; the listing often shows only the date
            deadline_raw=ab.get("deadline_raw") or r.get("deadline_raw"),
            deadline_tz_hint=ab.get("deadline_timezone") if ab.get("deadline_raw") else r.get("deadline_timezone"),
            first_seen_at=r.get("retrieved_at"), last_seen_at=ab.get("retrieved_at") or r.get("retrieved_at"),
            declared_status="open", description=str(ab.get("description") or ab.get("overview") or ""), attachments=docs,
            acquisition_membership="CURRENT_PUBLIC_SNAPSHOT",
            provenance={"file": str(snap), "result_page": r.get("result_page"), "member_agency": r.get("member_agency"),
                        "abstract_locked_fields": ab.get("locked_fields"), "abstract_auth_wall": ab.get("auth_wall"),
                        "abstract_from_vault": bool(ab.get("from_vault"))},
        ))
    # Re-checked here so a coverage.json written by an older finalizer cannot retire legacy rows
    complete = bool(cov.get("enumeration_complete")) and (cov.get("coverage_ratio") or 0) >= 0.99
    return {"available": True, "records": out, "complete": complete, "coverage": cov,
            "vault_abstracts_merged": vault_hits, "abstracts_available": len(abstracts)}


def attach_documents(records: list[dict[str, Any]], doc_root: Path) -> dict[str, Any]:
    """Merge free-acquired documents (``doc_text.jsonl``) onto records by source_record_id.

    Attachment text becomes ``document_text`` (document evidence); official-page text only extends
    ``description`` so portal chrome alone never counts as a solicitation document.
    """
    tp = doc_root / "doc_text.jsonl"
    if not tp.exists():
        return {"available": False}
    idmap_p = doc_root / "staged_id_map.json"
    idmap = json.loads(idmap_p.read_text(encoding="utf-8")) if idmap_p.exists() else {}
    by_src: dict[str, dict[str, list[str]]] = {}
    for row in _read_jsonl(tp):
        srcs = [row["source_record_id"]] if row.get("source_record_id") else idmap.get(str(row.get("staged_id")), [])
        for s in srcs:
            slot = by_src.setdefault(s, {"attachment": [], "page": [], "page_item_schedule": [], "urls": []})
            kind = row.get("kind") or "page"
            if kind not in slot:
                slot[kind] = []
            slot[kind].append(html.unescape(str(row.get("text") or "")))
            if kind == "attachment":
                slot["urls"].append(row.get("url"))
    hit = att = unrelated = revised = 0
    for r in records:
        slot = by_src.get(r.get("source_record_id"))
        if not slot:
            continue
        hit += 1
        kept: list[str] = []
        listed: list[dict[str, Any]] = []
        for text, url in zip(slot["attachment"], slot["urls"]):
            why = unrelated_site_document(url or "", text, title=str(r.get("title") or ""),
                                          solicitation_number=str(r.get("solicitation_number") or ""))
            if why:
                unrelated += 1
                listed.append({"url": url, "status": "UNRELATED_SITE_DOCUMENT", "reason": why})
            else:
                kept.append(text)
                listed.append({"url": url, "status": "EXTRACTED_FREE"})
        if kept:
            att += 1
            r["document_text"] = "\n\n".join([r.get("document_text") or "", *kept]).strip()[:400_000]
        if listed:
            r["attachments"] = [*(r.get("attachments") or []), *listed[:10]]
        # Prefer the longest retained page (complete schedule rewrite) and append any extra item pages
        page_texts = list(slot.get("page") or [])
        sched_texts = list(slot.get("page_item_schedule") or [])
        if page_texts or sched_texts:
            best = max(page_texts, key=len) if page_texts else ""
            extras = [t for t in sched_texts if t and t not in best]
            combined = best
            for t in extras:
                combined = (combined + "\n\n" + t).strip()
            # Cap raised so multi-page commodity schedules (e.g. 62 rock-salt lines) are not truncated away
            r["description"] = (str(r.get("description") or "") + "\n\n" + combined[:400_000]).strip()
            stated = page_stated_deadline(combined)
            if stated:
                # The official page outranks an aggregator feed; the feed value is kept as history
                feed = parse_deadline(r.get("deadline_raw"), tz_name=r.get("deadline_tz_hint"), state=r.get("state"))
                r["deadline_feed_raw"], r["deadline_feed_tz"] = r.get("deadline_raw"), r.get("deadline_tz_hint")
                r["deadline_raw"], r["deadline_source"] = stated, "official_page"
                # A feed's UTC zone describes its own encoding, not the buyer's local clock printed on the page
                if str(r.get("deadline_tz_hint") or "").upper() in _UTC_NAMES:
                    r["deadline_tz_hint"] = None
                page = parse_deadline(stated, tz_name=r.get("deadline_tz_hint"), state=r.get("state"))
                if feed.get("deadline_at") and page.get("deadline_at") and abs(
                        (page["deadline_at"] - feed["deadline_at"]).total_seconds()) > 3600:
                    revised += 1
                    r["deadline_revision"] = {"feed": feed["deadline_at"].isoformat(), "official_page": page["deadline_at"].isoformat()}
    return {"available": True, "records_with_documents": att, "records_enriched": hit,
            "unrelated_site_documents_excluded": unrelated, "deadlines_revised_from_official_page": revised}


def _location_states(loc: Any) -> list[Any]:
    if isinstance(loc, dict):
        return [loc.get("buyer_state"), *(loc.get("states") or [])]
    return [loc]


def load_nextbid(data_root: Path) -> dict[str, Any]:
    """NextBid durable inventory + all retained original evidence (read-only)."""
    from m3.research.nextbid_full_inventory_audit import gather_original_evidence
    from m3.research.nextbid_inventory import load_inventory
    from m3.research.nextbid_recovery import collect_population
    from m3.research.nextbid_response_store import store_root as paid_store_root
    from m3.research.nextbid_status_reconciliation import load_retained_provider_index

    inv = load_inventory()
    index = load_retained_provider_index()
    pop = (collect_population().get("by_id") or {})
    paid = paid_store_root() / "by_nb_id"
    out: list[dict[str, Any]] = []
    for nb, row in inv.items():
        ev = gather_original_evidence(nb, provider=index["by_nb"].get(nb), pop_row=pop.get(nb), inv_row=row,
                                      search_hits=index["search_hits"].get(nb) or [], paid_by_nb=paid)
        o = ev.get("original") or {}
        src = str(row.get("source") or o.get("source") or "").lower()
        out.append(_staged(
            source_record_id=f"nextbid:{nb}", source=src, source_family="nextbid", provider_record_id=nb,
            solicitation_number=row.get("solicitation_number") or o.get("solicitation_number"),
            title=row.get("title") or o.get("title"), buyer=o.get("buyer_name") or o.get("agency"),
            state=state_code(*_location_states(o.get("location"))),
            official_url=row.get("official_url") or o.get("official_url"),
            posted_at=row.get("posted_date") or o.get("posted_at"),
            deadline_raw=row.get("response_deadline") or row.get("deadline_at") or o.get("response_deadline"),
            deadline_tz_hint=o.get("deadline_timezone"),
            first_seen_at=row.get("first_seen_at"), last_seen_at=row.get("last_seen_at"),
            declared_status=row.get("lifecycle_status") or row.get("solicitation_status"),
            notice_type_declared=row.get("notice_type") or o.get("notice_type"),
            procurement_kind=row.get("procurement_kind") or o.get("procurement_kind"),
            agency_codes=agency_codes_from_record(o),
            description=str(o.get("description") or o.get("summary") or ""),
            document_text=str(ev.get("document_text") or ""),
            attachments=_nextbid_attachments(o),
            provenance={"inventory": str(data_root / "nextbid_inventory" / "opportunities.jsonl"),
                        "evidence_sources": ev.get("sources_used"), "cache_refs": (ev.get("cache_refs") or [])[:3],
                        "provider_open_filter": row.get("provider_open_filter"),
                        "offline_recovery": o.get("offline_recovery")},
        ))
    return {"available": True, "records": out}


def _nextbid_attachments(o: dict[str, Any]) -> list[dict[str, Any]]:
    """Union documents[] + source_documents + nextbid_documents (thicker metadata wins)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for key in ("attachments", "documents", "source_documents", "nextbid_documents", "document_list"):
        for a in o.get(key) or []:
            if not isinstance(a, dict):
                continue
            k = str(
                a.get("sha256") or a.get("path") or a.get("document_url") or a.get("url")
                or a.get("name") or a.get("filename") or json.dumps(a, sort_keys=True, default=str)[:120]
            )
            if k in seen:
                continue
            seen.add(k)
            out.append(a)
    return out[:50]


def load_free_sources(data_root: Path) -> dict[str, Any]:
    """Free public-source acceptance populations: union by external_key, newest population wins."""
    pdir = data_root / "acceptance_populations"
    files = []
    for p in sorted(pdir.glob("*.jsonl")):
        meta_p = p.with_suffix(".meta.json")
        written = None
        if meta_p.exists():
            written = json.loads(meta_p.read_text(encoding="utf-8")).get("written_at")
        files.append((written or datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(), p))
    files.sort()
    by_key: dict[str, dict[str, Any]] = {}
    seen: dict[str, list[str]] = defaultdict(list)
    raw_rows = 0
    for written, p in files:
        for r in _read_jsonl(p):
            raw_rows += 1
            k = str(r.get("external_key") or "")
            if not k:
                continue
            seen[k].append(written)
            by_key[k] = {**r, "_population": p.name, "_written": written}
    out: list[dict[str, Any]] = []
    for k, r in by_key.items():
        st = str(r.get("solicitation_status") or "").lower()
        rt = str(r.get("record_type") or "")
        if st == "historical_award" or rt == "HISTORICAL_AWARD":
            kind = "AWARD"
        elif st == "forecast" or rt in {"PROCUREMENT_FORECAST", "PURCHASING_PLAN"}:
            kind = "FORECAST"
        elif rt in _NON_SOLICITATION_TYPES or _JUNK_TITLE.search(str(r.get("title") or "")):
            kind = "NON_SOLICITATION"
        elif (str(r.get("identity_strength") or "") == "none" and not r.get("solicitation_number")
              and not r.get("deadline_at") and str(r.get("source_purpose") or "") == "html_link_harvest"):
            kind = "UNVERIFIED_LINK"
        else:
            kind = "SOLICITATION"
        out.append(_staged(
            source_record_id=f"free:{k}", source=str(r.get("source_key") or "free"), source_family="free_source",
            provider_record_id=k, solicitation_number=r.get("solicitation_number"), title=r.get("title"),
            buyer=r.get("buyer_name"), state=state_code(r.get("geo_state")), official_url=r.get("official_url"),
            deadline_raw=r.get("deadline_at"), first_seen_at=min(seen[k]), last_seen_at=max(seen[k]),
            declared_status=st or None, record_kind=kind, procurement_kind=r.get("procurement_type"),
            description=str(r.get("description") or ""),
            attachments=[{"url": a} if isinstance(a, str) else a for a in (r.get("attachment_candidates") or [])][:20],
            provenance={"population": r["_population"], "record_type": rt, "populations_seen": len(seen[k])},
        ))
    return {"available": True, "records": out, "raw_rows": raw_rows, "populations": [p.name for _, p in files]}


# --- Lifecycle ---------------------------------------------------------------------------

_CANCEL_TITLE = re.compile(r"(?i)\b(?:cancel+ed|cancellation|withdrawn|rescinded)\b")


def determine_lifecycle(rec: dict[str, Any], now: datetime, *, max_age_hours: int) -> dict[str, Any]:
    d = parse_deadline(rec.get("deadline_raw"), tz_name=rec.get("deadline_tz_hint"), state=rec.get("state"))
    dl: datetime | None = d.get("deadline_at")
    declared = str(rec.get("declared_status") or "").lower()
    kind = rec.get("record_kind")
    try:
        seen = datetime.fromisoformat(str(rec.get("last_seen_at")).replace("Z", "+00:00")) if rec.get("last_seen_at") else None
        if seen and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except ValueError:
        seen = None
    age_h = (now - seen).total_seconds() / 3600 if seen else None
    basis: list[str] = [f"deadline:{d['deadline_basis']}"]
    implausible = bool(dl and dl > now + timedelta(days=400))
    if kind == "AWARD" or declared in {"awarded", "historical_award"}:
        status = "AWARDED"
    elif declared in {"cancelled", "canceled"} or _CANCEL_TITLE.search(str(rec.get("title") or "")):
        status = "CANCELLED"
    elif dl and dl < now:
        status = "EXPIRED"
        basis.append("deadline_passed_in_solicitation_timezone")
    elif declared in {"closed", "expired", "inactive", "closed_confirmed"} and not dl:
        status = "CLOSED"
    elif declared in {"closed", "expired", "inactive"} and dl and dl >= now:
        status = "STATUS_UNKNOWN"
        basis.append("declared_closed_but_future_deadline")
    elif dl and not implausible:
        open_declared = declared in {"open", "active", "amended", "posted", "open_confirmed"} or bool(
            (rec.get("provenance") or {}).get("provider_open_filter"))
        if open_declared and age_h is not None and age_h <= max_age_hours:
            status = "VERIFIED_OPEN"
            basis.append(f"open_observed_{age_h:.0f}h_ago_with_future_deadline")
        else:
            status = "LIKELY_OPEN"
            basis.append(f"future_deadline_last_observed_{age_h:.0f}h_ago" if age_h is not None else "future_deadline_unobserved")
    else:
        status = "STATUS_UNKNOWN"
        basis.append("deadline_implausible_over_400_days" if implausible else "no_usable_deadline")
    return {"lifecycle_status": status, "lifecycle_basis": basis, "deadline_at": dl.isoformat() if dl else None,
            "deadline_basis": d.get("deadline_basis"), "deadline_timezone": d.get("deadline_timezone"),
            "observation_age_hours": round(age_h, 1) if age_h is not None else None}


# --- Dedup -------------------------------------------------------------------------------

_LIFE_RANK = {s: i for i, s in enumerate(LIFECYCLE_STATES)}
_FAMILY_RANK = {"bidnet": 0, "free_source": 0, "nextbid": 1, "bidnet_legacy": 2, "legacy_other": 3}


_PROVIDER_ID_FAMILIES = frozenset({"bidnet", "bidnet_legacy", "nextbid"})


def _distinct_provider_events(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Provider-issued event ids are authoritative: two different ids are two solicitations."""
    return (a.get("source_family") == b.get("source_family") and a.get("source_family") in _PROVIDER_ID_FAMILIES
            and bool(a.get("provider_record_id")) and a.get("provider_record_id") != b.get("provider_record_id"))


_TITLE_STOP = frozenset("the of and for to a an in on rfp rfq ifb bid itb synopsis solicitation correction extension notice".split())


def _title_tokens(t: Any) -> set[str]:
    return {w for w in _norm_text(t).split() if len(w) > 2 and w not in _TITLE_STOP}


def _titles_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ta, tb = _title_tokens(a.get("title")), _title_tokens(b.get("title"))
    if not ta or not tb:
        return True
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.34


def cluster_duplicates(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # A key shared by records with different titles is a listing/dataset URL or a reused
    # number, not a solicitation identity
    members: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        for k in r["identity_keys"]:
            members[k].append(i)
    ambiguous: set[str] = set()
    for k, idx in members.items():
        if len(idx) > 1 and not k.startswith("tbd:"):
            titles = {_norm_text(records[i].get("title"))[:30] for i in idx}
            if len(titles) > 3:
                ambiguous.add(k)
    for r in records:
        dropped = [k for k in r["identity_keys"] if k in ambiguous]
        if dropped:
            r["identity_keys"] = [k for k in r["identity_keys"] if k not in ambiguous]
            r["identity_keys_ambiguous"] = dropped[:3]
    # Distinct solicitation numbers never merge, even when title/buyer/date coincide
    # (DIBBS issues many RFQs for the same nomenclature on the same day)
    sols: dict[int, set[str]] = {}
    for i, r in enumerate(records):
        s = re.sub(r"[^A-Z0-9]", "", str(r.get("solicitation_number") or "").upper())
        sols[i] = {s} if len(s) >= 4 else set()
    owner: dict[str, int] = {}
    for i, r in enumerate(records):
        for k in r["identity_keys"]:
            if k in owner:
                j = owner[k]
                if k.startswith("tbd:") and _distinct_provider_events(r, records[j]):
                    continue
                if not _titles_compatible(r, records[j]):
                    continue
                a, b = find(i), find(j)
                if a != b and not (sols[a] and sols[b] and sols[a].isdisjoint(sols[b])):
                    parent[a] = b
                    sols[b] = sols[b] | sols[a]
            else:
                owner[k] = i
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(records)):
        groups[find(i)].append(i)
    return {str(k): v for k, v in groups.items()}


def _ts(v: Any) -> float:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _primary_rank(r: dict[str, Any]) -> tuple:
    text = len(r.get("description") or "") + len(r.get("document_text") or "")
    retired = 1 if r.get("retirement_reason") else 0
    return (retired, _LIFE_RANK.get(r["lifecycle_status"], 9), _FAMILY_RANK.get(r.get("source_family"), 9), -text,
            -_ts(r.get("last_seen_at")))


# --- Per-record procurement evaluation ---------------------------------------------------

def _engine_version() -> str:
    """Changes whenever classification / requirement rules change, invalidating cached evaluations."""
    import m3.research.classify as classify_mod
    import m3.research.procurement_requirements as req_mod

    h = hashlib.sha256()
    for mod in (classify_mod, req_mod):
        h.update(Path(mod.__file__).read_bytes())
    return h.hexdigest()[:16]


def _eval_key(rec: dict[str, Any], now: datetime, version: str) -> str:
    # The date is part of the key: eligibility depends on it (a mandatory meeting can pass overnight)
    blob = json.dumps([version, now.date().isoformat(), rec.get("title"), rec.get("description"), rec.get("document_text"),
                       rec.get("source"), rec.get("agency_codes"), rec.get("procurement_kind"), rec.get("notice_type_declared")],
                      default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def evaluate_record(rec: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    title = str(rec.get("title") or "")
    desc = rec.get("description") or ""
    doc = rec.get("document_text") or ""
    presence = assess_product_presence(
        title, description=desc, document_text=doc, agency_codes=rec.get("agency_codes") or None,
        procurement_kind=rec.get("procurement_kind"), notice_type=rec.get("notice_type_declared"), source=rec.get("source"),
    )
    reqs = extract_requirements(title, description=desc, document_text=doc)
    text = f"{desc} {doc}"
    elig = assess_reseller_eligibility(title=title, text=text, source=rec.get("source"),
                                       evidence_completeness=presence["evidence_completeness"],
                                       product_presence=presence["product_presence"], now=now)
    execu = assess_execution_feasibility(text=text, source=rec.get("source"),
                                         evidence_completeness=presence["evidence_completeness"])
    econ = assess_economics(line_items=reqs["line_items"], execution_state=execu["execution_state"])
    sourcing = assess_sourcing_readiness(title=title, requirements=reqs, eligibility_state=elig["eligibility_state"])
    qual = qualification(presence=presence, requirements=reqs, sourcing=sourcing)
    access = _access_block_reason(rec)
    gap = classify_evidence_gap(
        description=desc, document_text=doc, requirements=reqs, sourcing=sourcing,
        access_block=access, retrieval_status=rec.get("retrieval_status"),
    )
    blockers = owner_blocker_codes(
        title=title, requirements=reqs, sourcing=sourcing, presence=presence, eligibility=elig,
        evidence_gap=gap, deadline_at=str(rec.get("deadline_at") or "") or None, access_block=access,
    )
    return {"presence": presence, "requirements": reqs, "eligibility": elig, "execution": execu, "economics": econ,
            "sourcing": sourcing, "qualification": qual, "evidence_gap": gap, "owner_blockers": blockers}


def _access_block_reason(rec: dict[str, Any]) -> str | None:
    """Return a stable reason when free retrieval is already known blocked — never re-probes the host."""
    st = str(rec.get("retrieval_status") or "")
    if st in _ACCESS_BLOCKED_STATUSES:
        return f"retrieval:{st}:{rec.get('retrieval_detail') or 'known'}"
    try:
        host = (urlparse(str(rec.get("official_url") or "")).netloc or "").lower()
    except Exception:
        host = ""
    for blocked in _ACCESS_BLOCKED_HOSTS:
        if blocked in host or host.endswith(blocked.lstrip("www.")):
            return f"host_access_blocked:{blocked}"
    if str(rec.get("source") or "").lower() == "bidnet":
        return f"host_access_blocked:{_BIDNET_ACCESS_BLOCK['host']}"
    return None


def record_source_access_blocks(root: Path) -> dict[str, Any]:
    """Persist confirmed free-access failures once. Never retries hosts listed here."""
    path = root / "source_access_blocks.json"
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    blocks = dict(existing.get("blocks") or {})
    changed = False
    key = _BIDNET_ACCESS_BLOCK["host"]
    if key not in blocks:
        blocks[key] = {**_BIDNET_ACCESS_BLOCK, "recorded_at": _utc()}
        changed = True
    for host in ("procurement.opengov.com", "ssl.doas.state.ga.us", "doas.state.ga.us"):
        if host not in blocks:
            blocks[host] = {
                "host": host, "status": "restricted", "detail": "previously confirmed free-access block",
                "do_not_retry": True, "recorded_at": _utc(),
            }
            changed = True
    payload = {"updated_at": _utc() if changed else existing.get("updated_at"), "blocks": blocks}
    if changed or not path.exists():
        _write_json(path, payload)
    return payload


def apply_known_access_blocks(records: list[dict[str, Any]], blocks: dict[str, Any]) -> dict[str, Any]:
    """Stamp known host blocks onto records that lack a prior retrieval outcome. No network."""
    host_blocks = blocks.get("blocks") or {}
    stamped = 0
    for r in records:
        if r.get("retrieval_status") in _ACCESS_BLOCKED_STATUSES:
            continue
        reason = _access_block_reason(r)
        if not reason or not reason.startswith("host_access_blocked:"):
            continue
        host = reason.split(":", 1)[-1]
        meta = host_blocks.get(host) or host_blocks.get(host.removeprefix("www.")) or {}
        # Listing-only BidNet rows keep their titles; mark further page fetch blocked once
        if not r.get("retrieval_status"):
            r["retrieval_status"] = meta.get("status") or "robots_disallow"
            r["retrieval_detail"] = meta.get("detail") or reason
            stamped += 1
        elif not r.get("retrieval_detail") and meta.get("detail"):
            r["retrieval_detail"] = meta["detail"]
    return {"stamped": stamped, "known_hosts": sorted(host_blocks)}


def attach_retrieval_status(records: list[dict[str, Any]], doc_root: Path) -> dict[str, Any]:
    """Copy prior free-retrieval outcomes onto records (offline). Does not fetch."""
    ck_p = doc_root / "doc_checkpoint.json"
    if not ck_p.exists():
        return {"available": False}
    done = json.loads(ck_p.read_text(encoding="utf-8")).get("done") or {}
    n = 0
    for r in records:
        st = done.get(str(r.get("staged_id") or "")) or done.get(str(r.get("source_record_id") or ""))
        if not st:
            continue
        r["retrieval_status"] = st.get("status")
        r["retrieval_detail"] = st.get("reason")
        n += 1
    return {"available": True, "records_with_status": n}


def assign_owner_queue(rec: dict[str, Any], ev: dict[str, Any] | None) -> tuple[str, str]:
    """(queue, reason). Rejects are archived with a reason, never shown as opportunities."""
    life = rec["lifecycle_status"]
    if rec.get("duplicate_of"):
        return "archived", f"duplicate_of:{rec['duplicate_of']}"
    if rec.get("retirement_reason"):
        return "archived", rec["retirement_reason"]
    if life not in ACTIVE_LIFECYCLE:
        return "archived", f"lifecycle:{life}"
    if rec.get("record_kind") != "SOLICITATION":
        return "archived", f"record_kind:{rec.get('record_kind')}"
    assert ev is not None
    p = ev["presence"]
    if p["notice_type"] in {"AWARD", "FORECAST", "ASSET_SALE"}:
        return "archived", f"notice_type:{p['notice_type']}"
    if title_only_unresolved(p) and life != "STATUS_UNKNOWN":
        return "document_verification", "title_only_deliverable_unresolved|needs_solicitation_page"
    if p["product_presence"] == "NO_PRODUCT_EVIDENCE" and not title_only_unresolved(p):
        return "archived", f"no_product_evidence:{p['procurement_category']}"
    if p["procurement_category"] == "rental":
        return "archived", "rental_excluded_from_resale"
    if p["procurement_category"] == "software" and p["product_presence"] not in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}:
        return "archived", "software_classified_separately"
    if life == "STATUS_UNKNOWN":
        return "status_verification", "lifecycle_status_unknown"
    if ev["eligibility"]["eligibility_state"] == "BLOCKED":
        return "blocked", "eligibility:" + ",".join(ev["eligibility"]["eligibility_reasons"][:3])
    if ev["execution"]["execution_state"] == "EXECUTION_BLOCKED":
        return "blocked", "execution:" + ",".join(b["factor"] for b in ev["execution"]["execution_blockers"][:3])
    src = ev["sourcing"]
    access = _access_block_reason(rec)
    gap = (ev.get("evidence_gap") or {}).get("evidence_gap_kind")
    # Access blocks never close or archive an opportunity.
    # If retained listing/title evidence already makes the record research-ready, keep that queue
    # even when further portal pages are blocked (BidNet robots 403 / auth-wall).
    # Only incomplete products that need more official content go to access_required.
    if (access and p["product_presence"] in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}
            and src["sourcing_state"] == "DOCUMENT_REQUIRED"):
        return "access_required", access
    if p["product_presence"] in {"POSSIBLE_PRODUCT", "INSUFFICIENT_EVIDENCE"} or src["sourcing_state"] == "DOCUMENT_REQUIRED":
        # Retained text the software failed to extract stays here for offline rework (gap=EXTRACTION_INCOMPLETE)
        return "document_verification", (
            f"presence:{p['product_presence']}|requirements:{ev['requirements']['requirements_state']}|"
            f"evidence:{p['evidence_completeness']}|gap:{gap or 'n/a'}")
    if src["sourcing_state"] == "COMMERCIAL_RESEARCH_READY":
        return "commercial_research_ready", "pending:" + ",".join(src["sourcing_missing"][:3])
    econ = ev["economics"]
    if "supplier_unit_cost" in econ["missing_inputs"]:
        return "supplier_research_ready", "no_verified_supplier_cost"
    if "government_unit_price" in econ["missing_inputs"]:
        return "commercial_research_ready", "no_verified_government_price"
    if not ev["requirements"]["quote_ready"]:
        return "supplier_research_ready", "quote_requirements_incomplete:" + ",".join(ev["requirements"]["quote_ready_blockers"][:3])
    if econ["profit_class"] in {"PROVEN", "LIKELY"} and ev["execution"]["execution_state"] != "EXECUTION_VERIFIED":
        return "financing_verification", ev["execution"]["execution_state"]
    if ev["eligibility"]["eligibility_state"] == "EASY_REGISTRATION_REQUIRED":
        return "easy_registration", "registration_required_before_bid"
    if econ["profit_class"] in {"PROVEN", "LIKELY"}:
        return "actionable", f"profit:{econ['profit_class']}"
    return "archived", f"unprofitable_or_unproven:{econ['profit_class']}"


_PRESENCE_RANK = {"EXPLICIT_PRODUCT": 0, "LIKELY_PRODUCT": 1, "POSSIBLE_PRODUCT": 2, "INSUFFICIENT_EVIDENCE": 3}


def _queue_priority(row: dict[str, Any]) -> tuple:
    """Work order inside a queue: actionable sources first, strongest product evidence, soonest
    deadline, then records with attachments to fetch."""
    c = row.get("classification") or {}
    return (1 if row.get("federal_policy_hold") else 0, _PRESENCE_RANK.get(c.get("product_presence"), 4),
            str(row.get("deadline_at") or "9999"), 0 if row.get("attachments") else 1, str(row.get("staged_id")))


def _slim(rec: dict[str, Any], ev: dict[str, Any] | None) -> dict[str, Any]:
    keep = {k: rec.get(k) for k in (
        "staged_id", "source_record_id", "source", "source_family", "provider_record_id", "solicitation_number", "title",
        "buyer", "state", "official_url", "posted_at", "deadline_at", "deadline_basis", "deadline_timezone",
        "first_seen_at", "last_seen_at", "observation_age_hours", "lifecycle_status", "lifecycle_basis", "record_kind",
        "acquisition_membership", "absence_note", "deadline_source", "deadline_feed_raw", "deadline_revision", "duplicate_of", "duplicate_members", "retirement_reason", "owner_queue", "queue_reason",
        "retrieval_status", "retrieval_detail", "federal_policy_hold", "identity_keys", "attachments", "provenance")}
    keep["description_chars"] = len(rec.get("description") or "")
    keep["document_text_chars"] = len(rec.get("document_text") or "")
    keep["text_sha256"] = hashlib.sha256(((rec.get("description") or "") + (rec.get("document_text") or "")).encode("utf-8")).hexdigest()[:16]
    if ev:
        p = ev["presence"]
        keep["classification"] = {k: p[k] for k in (
            "product_presence", "product_detected", "discovery_retained", "procurement_category", "product_identity_confidence",
            "service_presence", "installation_requirement", "construction_requirement", "rental", "software",
            "prequalification", "notice_type", "evidence_completeness", "requirement_class", "decision_confidence")}
        keep["classification"]["signals"] = [{k: s[k] for k in ("level", "rule", "quote")} for s in p["presence_signals"][:8]]
        r = ev["requirements"]
        keep["requirements"] = {k: r[k] for k in (
            "requirements_state", "line_item_count", "complete_line_items", "quote_ready", "quote_ready_blockers",
            "solicitation_requirements", "schedule_advertised_total", "schedule_shown_from", "schedule_shown_to",
            "schedule_complete")}
        # Keep the full captured schedule — truncating to 25 hid incomplete multi-page extractions from the owner
        keep["requirements"]["line_items"] = r["line_items"]
        keep["eligibility"] = {k: ev["eligibility"][k] for k in ("eligibility_state", "eligibility_reasons", "eligibility_findings", "registration_required")}
        keep["execution"] = {k: ev["execution"][k] for k in ("execution_state", "execution_blockers", "execution_supports")}
        keep["economics"] = {k: ev["economics"][k] for k in ("profit_class", "missing_inputs", "evidence_quality", "quantity_total",
                                                              "revenue", "net_profit", "margin", "risks")}
        keep["sourcing"] = dict(ev["sourcing"])
        keep["qualification"] = dict(ev["qualification"])
        keep["evidence_gap"] = dict(ev.get("evidence_gap") or {})
        keep["owner_blockers"] = list(ev.get("owner_blockers") or [])
        keep["classification"]["presence_capped_by"] = p.get("presence_capped_by")
    return keep


# --- Run ---------------------------------------------------------------------------------

def run_staged_inventory(
    *,
    out_root: Path | None = None,
    now: datetime | None = None,
    loaders: dict[str, Callable[[], dict[str, Any]]] | None = None,
    resume: bool = True,
    require_locked: bool = True,
) -> dict[str, Any]:
    """Build the staged inventory. Offline only: no network, no database writes.

    ``require_locked`` guards repair/offline runs; the authorized daily schedule rebuilds right after
    its own acquisition and passes False.
    """
    from m3.research.nextbid_controller import acquisition_locked

    if require_locked:
        assert acquisition_locked(), "staged inventory requires the acquisition killswitch to stay locked"
    t0 = time.time()
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    root = out_root or s.staged_inventory_root()
    root.mkdir(parents=True, exist_ok=True)
    data_root = s.data_root()
    loaders = loaders or {
        "bidnet_snapshot": lambda: load_bidnet_snapshot(s.bidnet_snapshot_root()),
        "bidnet": lambda: load_legacy_bidnet(s.legacy_paths()),
        "nextbid": lambda: load_nextbid(data_root),
        "free": lambda: load_free_sources(data_root),
    }
    stage_counts: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for name, fn in loaders.items():
        t = time.time()
        res = fn()
        meta[name] = {k: v for k, v in res.items() if k != "records"}
        records.extend(res.get("records") or [])
        _log(f"loaded {name}: {len(res.get('records') or [])} records in {time.time() - t:.1f}s")
    stage_counts["loaded"] = len(records)
    meta["documents"] = attach_documents(records, root / "documents")
    meta["source_access_blocks"] = record_source_access_blocks(root)
    # A complete fresh BidNet enumeration is the current open universe; legacy BidNet rows are history
    fresh_bidnet_complete = bool((meta.get("bidnet_snapshot") or {}).get("complete"))

    for r in records:
        r.update(determine_lifecycle(r, now, max_age_hours=s.verified_open_max_age_hours))
        r["identity_keys"] = identity_keys(r) if r.get("record_kind") != "NON_SOLICITATION" else []
        if r.get("source_family") == "legacy_other":
            r["retirement_reason"] = "legacy_store_non_bidnet_record"
        elif fresh_bidnet_complete and r.get("source_family") == "bidnet_legacy":
            r["retirement_reason"] = "bidnet_legacy_superseded_by_fresh_snapshot"
        # Absence from an earlier, incomplete harvest is not closure evidence: such records keep their
        # own deadline-based lifecycle (expired, unknown or open) and are flagged for re-checking only.
        if r.get("acquisition_membership") == "LEGACY_NOT_IN_LATEST" and not r.get("retirement_reason"):
            r["absence_note"] = "absent_from_latest_legacy_acquisition_not_closure_evidence"
        r["federal_policy_hold"] = "FEDERAL_PARKED" if str(r.get("source") or "").lower() in FEDERAL_SOURCES else None

    clusters = cluster_duplicates(records)
    dup_n = 0
    for members in clusters.values():
        ranked = sorted(members, key=lambda i: _primary_rank(records[i]))
        primary = records[ranked[0]]
        sid = canonical_id(sorted({k for i in members for k in records[i]["identity_keys"]}), primary["source_record_id"])
        primary["staged_id"] = sid
        primary["duplicate_members"] = [records[i]["source_record_id"] for i in ranked[1:]] or None
        for i in ranked[1:]:
            records[i]["staged_id"] = sid
            records[i]["duplicate_of"] = primary["source_record_id"]
            records[i]["lifecycle_status_before_dedup"] = records[i]["lifecycle_status"]
            records[i]["lifecycle_status"] = "DUPLICATE"
            dup_n += 1
    stage_counts["duplicates_marked"] = dup_n
    stage_counts["canonical_records"] = len(records) - dup_n
    # After staged_id assignment so doc_checkpoint lookups by staged_id resolve
    meta["retrieval_status"] = attach_retrieval_status(records, root / "documents")
    meta["known_access_applied"] = apply_known_access_blocks(records, meta["source_access_blocks"])

    to_eval = [i for i, r in enumerate(records)
               if not r.get("duplicate_of") and not r.get("retirement_reason")
               and r["lifecycle_status"] in ACTIVE_LIFECYCLE and r.get("record_kind") == "SOLICITATION"]
    stage_counts["evaluated"] = len(to_eval)
    _log(f"lifecycle+dedup done: {len(records)} records, {dup_n} duplicates, {len(to_eval)} to evaluate")

    fp = hashlib.sha256(json.dumps([now.isoformat()[:13], len(records), stage_counts], default=str).encode()).hexdigest()[:16]
    ck_path = root / "checkpoint.json"
    parts_dir = root / "parts"
    done_chunks: set[int] = set()
    if resume and ck_path.exists():
        ck = json.loads(ck_path.read_text(encoding="utf-8"))
        if ck.get("fingerprint") == fp:
            done_chunks = set(ck.get("done_chunks") or [])
            _log(f"resuming: {len(done_chunks)} chunks already evaluated")
    evals: dict[int, dict[str, Any]] = {}
    ver = _engine_version()
    cache_p = root / "eval_cache.jsonl"
    cache = {row["k"]: row["ev"] for row in _read_jsonl(cache_p)} if cache_p.exists() else {}
    keys = {i: _eval_key(records[i], now, ver) for i in to_eval}
    cache_hits = 0
    for ci in range(0, len(to_eval), CHUNK):
        cno = ci // CHUNK
        part = parts_dir / f"eval_{cno:03d}.jsonl"
        idxs = to_eval[ci: ci + CHUNK]
        if cno in done_chunks and part.exists():
            for row in _read_jsonl(part):
                evals[int(row["i"])] = row["ev"]
            continue
        rows = []
        for i in idxs:
            ev = cache.get(keys[i])
            if ev is None:
                ev = evaluate_record(records[i], now)
            else:
                cache_hits += 1
            evals[i] = ev
            rows.append({"i": i, "ev": ev})
        _write_jsonl(part, rows)
        done_chunks.add(cno)
        _write_json(ck_path, {"fingerprint": fp, "done_chunks": sorted(done_chunks), "updated_at": _utc(), "build": BUILD})
        _log(f"evaluated chunk {cno} ({min(ci + CHUNK, len(to_eval))}/{len(to_eval)})")
    # Only this run's entries are kept, so records that left the active set fall out of the cache
    _write_jsonl(cache_p, [{"k": keys[i], "ev": evals[i]} for i in to_eval])
    stage_counts["evaluation_cache"] = {"engine_version": ver, "hits": cache_hits, "evaluated_fresh": len(to_eval) - cache_hits}

    queue_rows: dict[str, list[dict[str, Any]]] = {q: [] for q in OWNER_QUEUES}
    slim_all: list[dict[str, Any]] = []
    for i, r in enumerate(records):
        q, why = assign_owner_queue(r, evals.get(i))
        r["owner_queue"], r["queue_reason"] = q, why
        row = _slim(r, evals.get(i))
        slim_all.append(row)
        queue_rows[q].append(row)

    retirement = [
        {"source_record_id": r["source_record_id"], "staged_id": r["staged_id"], "source": r["source"],
         "title": r.get("title"), "lifecycle_status": r["lifecycle_status"], "reason": r["queue_reason"],
         "deadline_at": r.get("deadline_at"), "last_seen_at": r.get("last_seen_at"),
         "location": r.get("provenance"), "recheck_candidate": bool(
             r.get("absence_note") and r.get("deadline_at") and r["deadline_at"] >= now.isoformat()),
         "action": "RETIRE_FROM_ACTIVE_OPERATIONS_PRESERVE_RECORD"}
        for r in records if r["owner_queue"] == "archived" and r["lifecycle_status"] not in {"AWARDED"}
        and (r.get("retirement_reason") or r["lifecycle_status"] in {"EXPIRED", "CLOSED", "CANCELLED", "DUPLICATE"})
    ]
    pricing = [_slim(r, None) for r in records if r["lifecycle_status"] == "AWARDED" or r.get("record_kind") == "AWARD"]
    for q in queue_rows:
        if q != "archived":
            queue_rows[q].sort(key=_queue_priority)
            for n, row in enumerate(queue_rows[q], 1):
                row["queue_priority"] = n

    _write_jsonl(root / "inventory.jsonl", slim_all)
    for q, rows in queue_rows.items():
        if q != "archived":
            _write_jsonl(root / "queues" / f"{q}.jsonl", rows)
    _write_jsonl(root / "retirement_manifest.jsonl", retirement)
    _write_jsonl(root / "pricing_intelligence.jsonl", pricing)
    owner = build_owner_deliverables(root, slim_all, now)

    recon = reconcile(records, evals)
    input_hashes = {}
    for name, m in meta.items():
        for k, v in (m.get("files") or {}).items():
            p = Path(v)
            if p.exists() and p.stat().st_size < 400_000_000:
                input_hashes[f"{name}.{k}"] = {"path": v, "sha256": _sha256(p), "bytes": p.stat().st_size}
    disp = Counter(r["owner_queue"] for r in records)
    manifest = {
        "build": BUILD, "ran_at": _utc(), "runtime_now": now.isoformat(), "elapsed_sec": round(time.time() - t0, 1),
        "stage_counts": stage_counts, "queue_counts": dict(disp), "lifecycle_counts": dict(Counter(r["lifecycle_status"] for r in records)),
        "retirement_manifest_rows": len(retirement), "pricing_intelligence_rows": len(pricing), "owner_deliverables": owner,
        "all_records_dispositioned": sum(disp.values()) == len(records),
        "source_meta": meta, "input_hashes": input_hashes, "reconciliation": recon,
        "external_calls": 0, "database_writes": 0, "acquisition_locked": acquisition_locked(),
    }
    _write_json(root / "run_manifest.json", manifest)
    _log(f"done in {manifest['elapsed_sec']}s queues={dict(disp)}")
    return manifest


def reconcile(records: list[dict[str, Any]], evals: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Per-source reconciliation: raw, unique, duplicates, expired/closed, unknown, open, evidence."""
    by: dict[str, Counter] = defaultdict(Counter)
    for i, r in enumerate(records):
        fam = r.get("source_family") or "unknown"
        key = f"{fam}:{r.get('source')}" if fam in {"nextbid", "free_source"} else fam
        for k in (fam + ":ALL", key):
            c = by[k]
            c["raw"] += 1
            if r.get("duplicate_of"):
                c["duplicates"] += 1
                continue
            c["unique"] += 1
            life = r["lifecycle_status"]
            if r.get("retirement_reason"):
                c["retired_legacy"] += 1
            if life in {"EXPIRED", "CLOSED", "CANCELLED"}:
                c["expired_closed_cancelled"] += 1
            elif life == "AWARDED":
                c["awarded_pricing_intelligence"] += 1
            elif r.get("record_kind") in {"FORECAST", "NON_SOLICITATION", "UNVERIFIED_LINK"}:
                c[f"{r['record_kind'].lower()}_excluded"] += 1
            elif not r.get("retirement_reason"):
                c[life.lower()] += 1
            ev = evals.get(i)
            if ev:
                c[f"evidence:{ev['presence']['evidence_completeness'].lower()}"] += 1
                if ev["presence"]["product_detected"]:
                    c["product_detected"] += 1
                if ev["presence"]["discovery_retained"]:
                    c["product_retained"] += 1
                c[f"queue:{r.get('owner_queue')}"] += 1
    return {k: dict(v) for k, v in sorted(by.items())}
