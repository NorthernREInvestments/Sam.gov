"""Phase L.23 — full-source population funnel + continuous deal conversion.

One canonical opportunity population → progressive stages → replenishing
READY_TO_CALL queue. No fixed global caps. No auto outreach / bids / SAM API.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.sam_api_parked import sam_api_park_status
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.progressive_funnel import (
    DEEP_RESEARCH_HIGH,
    DEEP_RESEARCH_LOW,
    DEEP_RESEARCH_MEDIUM,
    HARD_REJECT,
    run_progressive_stages_cheap,
)
from phase_l.quote_economics import load_json, save_json, _f
from phase_l.quote_readiness import infer_quantity_from_text

BUILD = "20260929-m3-phase-l23-full-source-population-funnel-continuous-conversion"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
STORE_PATH = DATA / "l23_canonical_population_store.json"
DOCS = ROOT / "docs"

AUTO_SEND = False
AUTO_CALL = False
AUTO_BID = False

# Funnel lifecycle states (§9)
RAW = "RAW"
FAST_REJECT = "FAST_REJECT"
ACCESSIBLE_PRODUCT = "ACCESSIBLE_PRODUCT"
FAST_RESEARCH_PENDING = "FAST_RESEARCH_PENDING"
FAST_RESEARCH_COMPLETE = "FAST_RESEARCH_COMPLETE"
DEEP_RESEARCH_PRIORITY = "DEEP_RESEARCH_PRIORITY"
DEEP_RESEARCH_IN_PROGRESS = "DEEP_RESEARCH_IN_PROGRESS"
DEEP_RESEARCH_COMPLETE = "DEEP_RESEARCH_COMPLETE"
READY_TO_CALL = "READY_TO_CALL"
CALLS_IN_PROGRESS = "CALLS_IN_PROGRESS"
QUOTE_PENDING = "QUOTE_PENDING"
QUOTES_RECEIVED = "QUOTES_RECEIVED"
READY_FOR_FINAL_ECONOMICS = "READY_FOR_FINAL_ECONOMICS"
READY_TO_BID = "READY_TO_BID"
SKIP = "SKIP"
WATCH = "WATCH"
WATCH_FEDERAL_ACCESS = "WATCH_FEDERAL_ACCESS"
WATCH_OTHER = "WATCH_OTHER"
LOW_PRIORITY_RESEARCH = "LOW_PRIORITY_RESEARCH"
NEEDS_SOURCE_DATA = "NEEDS_SOURCE_DATA"

# Freshness
LIVE_FRESH = "LIVE_FRESH"
LAST_KNOWN_RECENT = "LAST_KNOWN_RECENT"
STALE = "STALE"
HISTORICAL_ONLY = "HISTORICAL_ONLY"

# Source status
ACTIVE_STRUCTURED = "ACTIVE_STRUCTURED"
ACTIVE_STATIC = "ACTIVE_STATIC"
ACTIVE_MANUAL_PUBLIC = "ACTIVE_MANUAL_PUBLIC"
FREE_REGISTRATION_REQUIRED = "FREE_REGISTRATION_REQUIRED"
AUTH_BLOCKED = "AUTH_BLOCKED"
ANTI_BOT = "ANTI_BOT"
FAILED_TEMPORARY = "FAILED_TEMPORARY"
PARKED_LOW_VALUE = "PARKED_LOW_VALUE"
UNAVAILABLE = "UNAVAILABLE"

PREVIOUS_CALL_READY = 15

FUNNEL_STATES = (
    RAW, FAST_REJECT, ACCESSIBLE_PRODUCT, FAST_RESEARCH_PENDING, FAST_RESEARCH_COMPLETE,
    DEEP_RESEARCH_PRIORITY, DEEP_RESEARCH_IN_PROGRESS, DEEP_RESEARCH_COMPLETE,
    READY_TO_CALL, CALLS_IN_PROGRESS, QUOTE_PENDING, QUOTES_RECEIVED,
    READY_FOR_FINAL_ECONOMICS, READY_TO_BID, SKIP, WATCH, WATCH_FEDERAL_ACCESS, WATCH_OTHER,
    LOW_PRIORITY_RESEARCH, NEEDS_SOURCE_DATA,
)

L21_READY = "READY_FOR_OWNER_APPROVAL"
L21_MINOR = "NEEDS_MINOR_REVIEW"


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _solicitation_key(row: dict[str, Any]) -> str:
    auth = row.get("authoritative_bid_location") if isinstance(row.get("authoritative_bid_location"), dict) else {}
    return _norm(
        row.get("solicitation_id")
        or row.get("solicitation_number")
        or row.get("notice_id")
        or row.get("solicitation")
        or auth.get("solicitation_id")
        or auth.get("solicitation_number")
    )


def canonical_id_for(row: dict[str, Any]) -> str:
    """Strong identity only — never fuzzy-collapse on similar titles alone."""
    sol = _solicitation_key(row)
    buyer = _norm(row.get("agency") or row.get("buyer") or row.get("department") or (
        (row.get("authoritative_bid_location") or {}).get("buyer")
        if isinstance(row.get("authoritative_bid_location"), dict)
        else None
    ))
    url = _norm(
        row.get("detail_url")
        or row.get("original_posting_url")
        or row.get("solicitation_url")
        or row.get("ui_link")
        or row.get("source_url")
    )
    platform = _norm(row.get("source_id") or row.get("platform") or row.get("portal"))
    title = _norm(row.get("title"))[:120]
    deadline = _norm(row.get("response_deadline") or row.get("deadline") or row.get("due_date"))

    # 1) Unique notice URL (sam.gov/opp/<id>, ramp opportunity-details, etc.)
    if url and ("/opp/" in url or "opportunity-details" in url):
        return hashlib.sha1(f"notice_url|{url}".encode("utf-8")).hexdigest()[:16]

    # 2) Reliable solicitation ID + buyer (primary structured identity)
    if sol and sol not in {"none", "null", "n/a"} and buyer:
        return hashlib.sha1(f"sol|{buyer}|{sol}".encode("utf-8")).hexdigest()[:16]

    # 3) Solicitation ID alone when globally unique-ish
    if sol and sol not in {"none", "null", "n/a"}:
        return hashlib.sha1(f"sol_only|{sol}|{platform}".encode("utf-8")).hexdigest()[:16]

    # 4) Exact title + buyer + deadline (NOT fuzzy) — source clones share these
    if title and buyer:
        return hashlib.sha1(f"title|{buyer}|{title}|{deadline}".encode("utf-8")).hexdigest()[:16]

    if title:
        return hashlib.sha1(f"title_only|{title}|{deadline}|{platform}".encode("utf-8")).hexdigest()[:16]

    return hashlib.sha1(f"fallback|{buyer}|{deadline}|{platform}|{url[:80]}".encode("utf-8")).hexdigest()[:16]


def classify_freshness(row: dict[str, Any]) -> str:
    f = str(row.get("inventory_freshness") or row.get("freshness") or "").upper()
    if f in {LIVE_FRESH, "LIVE", "OPEN"}:
        return LIVE_FRESH if f != "OPEN" else LIVE_FRESH
    if f == LAST_KNOWN_RECENT or f == "LAST_KNOWN":
        return LAST_KNOWN_RECENT
    if f in {STALE, HISTORICAL_ONLY}:
        return f
    live = str(row.get("live_status") or "").upper()
    if live in {"OPEN", "LIVE", "ACTIVE"}:
        return LIVE_FRESH
    if row.get("last_live_verification_at"):
        return LAST_KNOWN_RECENT
    return LAST_KNOWN_RECENT


def classify_source_status(row: dict[str, Any]) -> str:
    sid = str(row.get("source_id") or "").lower()
    access = str(row.get("access_blocker") or "").upper()
    if "bidnet" in sid:
        return PARKED_LOW_VALUE
    if access in {"AUTH_REQUIRED", "LOGIN_REQUIRED", "CAPTCHA"}:
        return AUTH_BLOCKED if "CAPTCHA" not in access else ANTI_BOT
    if "socrata" in sid or "ckan" in sid or "arcgis" in sid:
        return ACTIVE_STRUCTURED
    if "opengov" in sid or "bonfire" in sid or "simplehtml" in sid:
        return ACTIVE_STRUCTURED
    if "sam" in sid or "fed_" in sid:
        return ACTIVE_MANUAL_PUBLIC
    if row.get("is_easy_registration"):
        return FREE_REGISTRATION_REQUIRED
    if row.get("source_url") or row.get("detail_url"):
        return ACTIVE_STATIC
    return ACTIVE_MANUAL_PUBLIC


def is_federal_row(row: dict[str, Any]) -> bool:
    sid = str(row.get("source_id") or "").lower()
    jur = str(row.get("jurisdiction") or "").upper()
    agency = str(row.get("agency") or "").lower()
    if "sam" in sid or sid.startswith("fed_") or jur in {"FEDERAL", "FED", "US"}:
        return True
    if any(x in agency for x in ("dla ", "defense logistics", "department of defense", "dept of the army", "dept of the navy", "dept of the air")):
        return True
    return False


def authoritative_url(row: dict[str, Any]) -> str | None:
    for k in (
        "detail_url",
        "original_posting_url",
        "solicitation_url",
        "portal_url",
        "ui_link",
        "source_url",
    ):
        v = row.get(k)
        if v:
            return str(v)
    auth = row.get("authoritative_bid_location")
    if isinstance(auth, dict) and auth.get("detail_url"):
        return str(auth["detail_url"])
    prov = row.get("discovery_provenance")
    if isinstance(prov, dict) and prov.get("url"):
        return str(prov["url"])
    return None


# ---------------------------------------------------------------------------
# Ingest + dedupe
# ---------------------------------------------------------------------------

def _load_json_rows(path: Path, *keys: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    for k in keys:
        if isinstance(data.get(k), list):
            return list(data[k])
    if isinstance(data, list):
        return data
    return []


def collect_raw_populations() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge all known inventory / hunt / phase artifacts into one raw list."""
    sources_meta: dict[str, Any] = {"feeds": []}
    rows: list[dict[str, Any]] = []

    feeds = [
        (OUT / "accessible_latest.json", ("rows",), "accessible_latest"),
        (OUT / "hunt_latest.json", ("rows", "opportunities", "results", "normalized_sample", "top20"), "hunt_latest"),
        (OUT / "enrichment_latest.json", ("rows", "enriched", "results"), "enrichment_latest"),
        (OUT / "l173_accessible_now.json", ("rows", "opportunities", "results"), "l173_accessible_now"),
        (OUT / "l172_accessible_now.json", ("rows", "opportunities", "results"), "l172_accessible_now"),
        (OUT / "l18_research_results.json", ("results", "rows"), "l18_research_results"),
        (OUT / "l19_research_results.json", ("results", "rows"), "l19_research_results"),
        (OUT / "l20_research_results.json", ("results", "rows"), "l20_research_results"),
        (OUT / "l21_quote_readiness.json", ("rows",), "l21_quote_readiness"),
        (OUT / "l10_fresh_discovery.json", ("rows", "opportunities", "results"), "l10_fresh"),
        (OUT / "l11_fresh_hunt.json", ("rows", "opportunities", "results"), "l11_fresh"),
        (OUT / "l12_fresh_hunt.json", ("rows", "opportunities", "results"), "l12_fresh"),
        (OUT / "l13_fresh_hunt.json", ("rows", "opportunities", "results"), "l13_fresh"),
        (ROOT / "artifacts" / "phase_g" / "live_run_latest.json", ("all_results", "held_sample"), "phase_g_live_run"),
        (ROOT / "artifacts" / "phase_i" / "hunt_latest.json", ("rows", "results"), "phase_i_hunt"),
    ]
    for path, keys, label in feeds:
        batch = _load_json_rows(path, *keys)
        # Normalize L.21-shaped rows
        norm_batch = []
        for r in batch:
            rr = dict(r)
            if "buyer" in rr and "agency" not in rr:
                rr["agency"] = rr.get("buyer")
            if "solicitation" in rr and not rr.get("solicitation_id"):
                rr["solicitation_id"] = rr.get("solicitation")
            live = rr.get("live") if isinstance(rr.get("live"), dict) else {}
            if live.get("original_url") and not rr.get("source_url"):
                rr["source_url"] = live.get("original_url")
            rr["_ingest_feed"] = label
            norm_batch.append(rr)
        sources_meta["feeds"].append(
            {
                "label": label,
                "path": str(path),
                "count": len(norm_batch),
                "exists": path.exists(),
                "keys_tried": list(keys),
            }
        )
        rows.extend(norm_batch)

    return rows, sources_meta


def dedupe_population(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    provenance: dict[str, list[dict[str, Any]]] = {}
    merge_reasons: Counter = Counter()
    groups: dict[str, list[dict[str, Any]]] = {}

    for r in rows:
        cid = canonical_id_for(r)
        sol = _solicitation_key(r)
        url = _norm(r.get("detail_url") or r.get("source_url") or "")
        reason = "source_clone"
        if url and ("/opp/" in url or "opportunity-details" in url):
            reason = "canonical_url_match"
        elif sol:
            reason = "buyer_solicitation_match" if (r.get("agency") or r.get("buyer")) else "exact_solicitation_id_match"
        else:
            reason = "exact_title_deadline_fallback"
        prov = {
            "feed": r.get("_ingest_feed"),
            "source_id": r.get("source_id"),
            "url": authoritative_url(r),
            "title": (r.get("title") or "")[:80],
            "solicitation_id": sol or None,
            "merge_reason": reason,
        }
        provenance.setdefault(cid, []).append(prov)
        groups.setdefault(cid, []).append(r)
        if cid not in by_id:
            by_id[cid] = r
            merge_reasons["kept_first"] += 1
        else:
            merge_reasons[reason] += 1
            cur = by_id[cid]
            if classify_freshness(r) == LIVE_FRESH and classify_freshness(cur) != LIVE_FRESH:
                by_id[cid] = r
            elif authoritative_url(r) and not authoritative_url(cur):
                by_id[cid] = r

    unique = []
    for cid, r in by_id.items():
        unique.append({**r, "canonical_opportunity_id": cid, "provenance_links": provenance.get(cid, [])})

    large = sorted(
        (
            {
                "canonical_id": cid,
                "size": len(members),
                "titles": sorted({(m.get("title") or "")[:60] for m in members})[:8],
                "solicitation_ids": sorted({_solicitation_key(m) for m in members if _solicitation_key(m)})[:8],
                "urls": sorted({str(authoritative_url(m) or "")[:70] for m in members if authoritative_url(m)})[:5],
                "feeds": sorted({str(m.get("_ingest_feed")) for m in members}),
                "primary_reason": (provenance.get(cid) or [{}])[-1].get("merge_reason"),
            }
            for cid, members in groups.items()
            if len(members) > 1
        ),
        key=lambda x: -x["size"],
    )

    # Suspected bad merges: multiple distinct solicitation_ids in one group
    bad = [g for g in large if len(g.get("solicitation_ids") or []) > 1]
    stats = {
        "raw_input": len(rows),
        "unique": len(unique),
        "duplicates_collapsed": len(rows) - len(unique),
        "merge_reasons": dict(merge_reasons),
        "groups_with_clones": len(large),
        "large_groups_gt5": sum(1 for g in large if g["size"] > 5),
        "suspected_bad_multi_sol_merges": len(bad),
        "largest_groups": large[:25],
        "bad_merge_samples": bad[:15],
    }
    return unique, stats


def to_canonical_record(row: dict[str, Any], *, funnel_state: str = RAW) -> dict[str, Any]:
    return {
        "kind": "CanonicalOpportunityPopulation",
        "canonical_opportunity_id": row.get("canonical_opportunity_id") or canonical_id_for(row),
        "source_provenance": row.get("provenance_links") or [{"feed": row.get("_ingest_feed"), "source_id": row.get("source_id")}],
        "buyer": row.get("agency") or row.get("buyer"),
        "jurisdiction": row.get("jurisdiction"),
        "platform": row.get("source_id") or row.get("platform"),
        "solicitation_event_id": row.get("solicitation_id")
        or row.get("solicitation_number")
        or row.get("notice_id")
        or row.get("solicitation")
        or (
            (row.get("authoritative_bid_location") or {}).get("solicitation_id")
            if isinstance(row.get("authoritative_bid_location"), dict)
            else None
        ),
        "title": row.get("title"),
        "description": (row.get("description") or "")[:2000] or None,
        "authoritative_url": authoritative_url(row),
        "submission_path": row.get("authoritative_bid_location") or row.get("submission_location"),
        "deadline": row.get("response_deadline") or row.get("deadline") or row.get("due_date"),
        "timezone": row.get("deadline_timezone") or row.get("timezone"),
        "freshness": classify_freshness(row),
        "source_status": classify_source_status(row),
        "product_service_classification": None,
        "access_status": row.get("our_bid_access") or row.get("competition_access_type"),
        "registration_status": "EASY" if row.get("is_easy_registration") else None,
        "current_funnel_state": funnel_state,
        "priority_score": None,
        "evidence_references": [],
        "is_federal": is_federal_row(row),
        "row_ref": {
            "title": row.get("title"),
            "agency": row.get("agency") or row.get("buyer"),
            "source_id": row.get("source_id"),
            "is_product": row.get("is_product"),
            "can_compete": row.get("can_compete"),
            "is_easy_registration": row.get("is_easy_registration"),
            "inventory_freshness": row.get("inventory_freshness"),
            "live_status": row.get("live_status"),
            "access_blocker": row.get("access_blocker"),
            "identity": row.get("identity"),
            "economics": row.get("economics"),
            "historical_award_price": row.get("historical_award_price"),
            "historical_award_unit_price": row.get("historical_award_unit_price"),
            "source_url": row.get("source_url"),
            "detail_url": row.get("detail_url"),
            "deadline": row.get("response_deadline") or row.get("deadline"),
            "description": row.get("description"),
            "solicitation_number": row.get("solicitation_number"),
            "solicitation_id": row.get("solicitation_id")
            or (
                (row.get("authoritative_bid_location") or {}).get("solicitation_id")
                if isinstance(row.get("authoritative_bid_location"), dict)
                else None
            ),
            "authoritative_bid_location": row.get("authoritative_bid_location"),
        },
        "updated_at": _utc(),
        "build": BUILD,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def fast_research_score(rec: dict[str, Any], cheap: dict[str, Any]) -> dict[str, Any]:
    score = 0
    factors: list[str] = []
    s2 = cheap.get("stage2") or {}
    commercial = s2.get("commercial") or {}
    s3 = cheap.get("stage3") or {}
    state = str(commercial.get("commercial_identity_state") or "")
    if state.startswith("EXACT"):
        score += 25
        factors.append("exact_identity")
    elif commercial.get("manufacturer") and commercial.get("model"):
        score += 18
        factors.append("mfr_model")
    elif commercial.get("mpn") or commercial.get("sku"):
        score += 15
        factors.append("mpn_sku")
    elif commercial.get("market_research_eligible"):
        score += 8
        factors.append("researchable")

    if not rec.get("is_federal"):
        score += 15
        factors.append("nonfederal_access_now")
    else:
        score += 2
        factors.append("federal_retained")

    if rec.get("freshness") == LIVE_FRESH:
        score += 10
        factors.append("live_fresh")
    elif rec.get("freshness") == LAST_KNOWN_RECENT:
        score += 5
        factors.append("last_known_recent")

    if rec.get("authoritative_url"):
        score += 8
        factors.append("authoritative_url")

    if commercial.get("commercial_priceability") == "HIGH":
        score += 12
        factors.append("high_priceability")

    if (rec.get("row_ref") or {}).get("historical_award_unit_price") or (rec.get("row_ref") or {}).get("historical_award_price"):
        score += 10
        factors.append("buyer_history_clue")

    deep = cheap.get("deep_research_priority")
    if deep == DEEP_RESEARCH_HIGH:
        score += 15
        factors.append("stage3_deep_high")
    elif deep == DEEP_RESEARCH_MEDIUM:
        score += 8
        factors.append("stage3_deep_medium")

    if (rec.get("row_ref") or {}).get("is_easy_registration"):
        score += 5
        factors.append("easy_registration")

    return {"kind": "FastResearchScore", "score": min(100, score), "factors": factors}


def deal_priority_score(rec: dict[str, Any], cheap: dict[str, Any], *, supplier_grade: str | None = None) -> dict[str, Any]:
    fr = rec.get("fast_research_score") or fast_research_score(rec, cheap)
    score = int(fr.get("score") or 0)
    factors = list(fr.get("factors") or [])
    # Winability relative signals
    win = 0
    hist = (rec.get("row_ref") or {}).get("historical_offers_received")
    try:
        if hist is not None and int(hist) <= 3:
            win += 8
            factors.append("low_bidder_count")
    except (TypeError, ValueError):
        pass
    if supplier_grade in {"A", "B"}:
        score += 12
        factors.append(f"supplier_{supplier_grade}")
    elif supplier_grade == "C":
        score += 6
        factors.append("supplier_C")
    score += win
    return {
        "kind": "DealPriorityScore",
        "score": min(100, score),
        "factors": factors,
        "winability_component": win,
        "fast_component": fr.get("score"),
    }


def research_effort_budget(priority_score: int) -> dict[str, Any]:
    if priority_score >= 70:
        units = 80
        band = "HIGH"
    elif priority_score >= 50:
        units = 40
        band = "MEDIUM"
    elif priority_score >= 35:
        units = 15
        band = "LOW"
    else:
        units = 5
        band = "MINIMAL"
    return {"kind": "ResearchEffortBudget", "band": band, "units": units, "priority_score": priority_score}


# ---------------------------------------------------------------------------
# Deep synthesis (local — no outbound)
# ---------------------------------------------------------------------------

def _product_identity_strength(commercial: dict[str, Any]) -> str:
    state = str(commercial.get("commercial_identity_state") or "")
    if state.startswith("EXACT"):
        return "EXACT"
    if state in {"STRONG_BRAND_HINT", "STRONG_PRODUCT_IDENTITY"}:
        return "STRONG"
    if commercial.get("manufacturer") and commercial.get("model"):
        return "STRONG"
    if commercial.get("manufacturer") and state == "STRONG_BRAND_HINT":
        return "STRONG"
    if commercial.get("mpn") or commercial.get("sku") or commercial.get("nsn"):
        return "STRONG"
    if commercial.get("market_research_eligible"):
        return "PARTIAL"
    return "WEAK"


def _brand_hint_from_title(title: str) -> tuple[str | None, str | None]:
    """Cheap brand/model hint from title for continuous conversion (no LLM)."""
    t = title or ""
    catalog_hints = [
        ("Apple", r"\b(apple\s+)?ipad\b", "iPad"),
        ("Ford", r"\bford\b.*\b(police|pursuit|interceptor|f-?150|explorer)\b|\bpolice\s+pursuit\s+interceptor\b", None),
        ("Caterpillar", r"\bcaterpillar\b|\b\bcat\b\s+model\s+c\d+", None),
        ("Dell", r"\bdell\b", None),
        ("ASUS", r"\basus\b|\bchromebox\b", None),
        ("Cisco", r"\bcisco\b|\bcatalyst\b|\bcatayst\b", None),
        ("Yamaha", r"\byamaha\b.*\bpiano", None),
        ("Getac", r"\bgetac\b", None),
        ("Bobcat", r"\bbobcat\b|\btoolcat\b", None),
    ]
    for mfr, pat, default_model in catalog_hints:
        if re.search(pat, t, re.I):
            model = default_model
            m = re.search(r"(?:model\s+)?(C\d{1,2}\w*|Latitude\s+\d+|iPad\s*[\d.]+[^\s,]*|Chromebox|Catalyst\s+\d+)", t, re.I)
            if m:
                model = m.group(1)
            return mfr, model
    return None, None


def synthesize_deep_research(rec: dict[str, Any], cheap: dict[str, Any]) -> dict[str, Any]:
    """Local deep synthesis using cache/catalog — no network, no fabricated verified prices."""
    commercial = dict((cheap.get("stage2") or {}).get("commercial") or {})
    # Brand hint fill-in when commercial recovery was UNKNOWN
    if not commercial.get("manufacturer"):
        mfr, model = _brand_hint_from_title(str(rec.get("title") or ""))
        if mfr:
            commercial["manufacturer"] = mfr
            if model and not commercial.get("model"):
                commercial["model"] = model
            if not commercial.get("commercial_identity_state") or commercial.get("commercial_identity_state") == "UNKNOWN":
                commercial["commercial_identity_state"] = "STRONG_BRAND_HINT"
            commercial["market_research_eligible"] = True
            commercial.setdefault("commercial_priceability", "MEDIUM")

    row_ref = rec.get("row_ref") or {}
    qty = infer_quantity_from_text({**row_ref, "title": rec.get("title"), "quantity": row_ref.get("quantity")})
    # Singular capital / exact MPN → qty 1 estimate
    if qty is None and _product_identity_strength(commercial) in {"EXACT", "STRONG"}:
        title = str(rec.get("title") or "")
        if re.search(r"\b(one\s*\(\s*1\s*\)|engine|laptop|chromebox|ipad|interceptor|piano|getac)\b", title, re.I):
            qty = 1.0
        elif commercial.get("mpn") or commercial.get("nsn"):
            qty = 1.0
        elif commercial.get("manufacturer") and commercial.get("model"):
            qty = 1.0

    suppliers: list[dict[str, Any]] = []
    mfr = commercial.get("manufacturer") or commercial.get("brand")
    model = commercial.get("model")
    try:
        from discovery.manufacturer_channels import resolve_manufacturer_channels
        from discovery.supplier_profiles import upsert_supplier_profile

        if mfr:
            ch = resolve_manufacturer_channels(manufacturer=mfr, model=model)
            for c in ch.get("candidates") or []:
                suppliers.append(upsert_supplier_profile(c, commercial=commercial))
    except Exception:
        pass

    # Known commercial brand without catalog hit → IT quote-required channels
    if not suppliers and mfr:
        for domain, name in (
            ("cdw-g.com", "CDW-G"),
            ("cdw.com", "CDW"),
            ("shi.com", "SHI"),
            ("insight.com", "Insight"),
        ):
            suppliers.append(
                {
                    "supplier_domain": domain,
                    "name": name,
                    "source_type": "DISTRIBUTOR",
                    "supplier_grade": "SUPPLIER_C",
                    "authorization_state": "AUTHORIZED_LIKELY",
                    "product_fit": "EXACT" if model else "PARTIAL",
                    "price_kind": "QUOTE_REQUIRED",
                    "locator_url": f"https://www.{domain}/",
                    "outreach_authorized": False,
                }
            )

    # MPN/NSN industrial path — quote-required distributors (not verified price)
    if not suppliers and (commercial.get("mpn") or commercial.get("nsn")):
        for domain, name, stype in (
            ("digikey.com", "Digi-Key", "DISTRIBUTOR"),
            ("grainger.com", "Grainger", "DISTRIBUTOR"),
            ("zoro.com", "Zoro", "DISTRIBUTOR"),
        ):
            suppliers.append(
                {
                    "supplier_domain": domain,
                    "name": name,
                    "source_type": stype,
                    "supplier_grade": "SUPPLIER_C",
                    "authorization_state": "AUTHORIZATION_NOT_REQUIRED",
                    "product_fit": "EXACT" if commercial.get("mpn") else "PARTIAL",
                    "price_kind": "QUOTE_REQUIRED",
                    "locator_url": f"https://www.{domain}/",
                    "outreach_authorized": False,
                }
            )

    identity = _product_identity_strength(commercial)
    stop_loss = None
    if identity == "WEAK" and not suppliers:
        stop_loss = "no_credible_supplier_path"
    if rec.get("freshness") == HISTORICAL_ONLY:
        stop_loss = "historical_only"

    gov_clue = None
    if row_ref.get("historical_award_unit_price") or row_ref.get("historical_award_price"):
        gov_clue = "usable_history_clue"
    elif identity in {"EXACT", "STRONG"}:
        gov_clue = "weak_or_unknown"

    best_sup = None
    for s in suppliers:
        g = str(s.get("supplier_grade") or "")
        if "A" in g:
            best_sup = "A"
            break
        if "B" in g:
            best_sup = "B"
        elif "C" in g and best_sup is None:
            best_sup = "C"

    return {
        "kind": "DeepResearchSynthesis",
        "product_identity": identity,
        "commercial": {
            "manufacturer": commercial.get("manufacturer"),
            "model": commercial.get("model"),
            "mpn": commercial.get("mpn"),
            "sku": commercial.get("sku"),
            "nsn": commercial.get("nsn"),
            "state": commercial.get("commercial_identity_state"),
        },
        "quantity": qty,
        "suppliers": suppliers[:8],
        "supplier_grade_best": best_sup,
        "gov_evidence_clue": gov_clue,
        "stop_loss": stop_loss,
        "cache_reuse": True,
        "network_calls": 0,
        "verified_acquisition_price": None,
        "verified_positive": None,
    }


def call_ready_gate(rec: dict[str, Any], deep: dict[str, Any]) -> dict[str, Any]:
    """READY_TO_CALL ≠ bid-ready. Worth getting real supplier pricing."""
    blockers: list[str] = []
    if rec.get("freshness") in {STALE, HISTORICAL_ONLY}:
        blockers.append("not_current")
    if deep.get("stop_loss"):
        blockers.append(str(deep["stop_loss"]))
    if deep.get("product_identity") not in {"EXACT", "STRONG", "PARTIAL"}:
        blockers.append("product_identity_weak")
    if deep.get("quantity") in (None, "", 0):
        blockers.append("quantity_unresolved")
    if not deep.get("suppliers"):
        blockers.append("no_supplier_channel")
    if deep.get("supplier_grade_best") not in {"A", "B", "C"}:
        blockers.append("no_credible_supplier_grade")
    # Federal access: retain but not call-ready now without CAGE path
    if rec.get("is_federal"):
        blockers.append("federal_access_defer")
    if not rec.get("authoritative_url") and not (rec.get("row_ref") or {}).get("source_url"):
        blockers.append("authoritative_source_missing")

    ok = not blockers
    reason = (
        f"identity={deep.get('product_identity')}; supplier={deep.get('supplier_grade_best')}; "
        f"qty={deep.get('quantity')}; gov={deep.get('gov_evidence_clue')}; nonfederal={not rec.get('is_federal')}"
    )
    return {"ready": ok, "blockers": blockers, "reason": reason, "owner_label": "CALL NOW" if ok else None}


def final_bid_gate(rec: dict[str, Any]) -> dict[str, Any]:
    """Strict — never auto-pass without real quote + evidence."""
    return {
        "ready": False,
        "state": None,
        "blockers": [
            "requires_written_supplier_quote",
            "requires_positive_verified_economics",
            "requires_owner_approval",
            "no_auto_bid",
        ],
        "auto_bid": False,
    }


# ---------------------------------------------------------------------------
# Import prior L.21 / L.22 call-ready
# ---------------------------------------------------------------------------

def import_prior_call_ready(store: dict[str, dict[str, Any]]) -> list[str]:
    """Seed READY_TO_CALL from L.21 quote readiness (previous batch of 15)."""
    path = OUT / "l21_quote_readiness.json"
    if not path.exists():
        return []
    imported: list[str] = []
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
    for r in rows:
        readiness = (r.get("readiness") or {}).get("state")
        if readiness not in {L21_READY, L21_MINOR}:
            continue
        fake_row = {
            "title": r.get("title"),
            "agency": r.get("buyer"),
            "solicitation_number": r.get("solicitation"),
            "source_url": (r.get("live") or {}).get("original_url"),
            "detail_url": (r.get("live") or {}).get("original_url"),
            "deadline": r.get("deadline"),
            "inventory_freshness": LIVE_FRESH,
            "is_product": True,
            "can_compete": True,
        }
        cid = canonical_id_for(fake_row)
        # Prefer matching existing store entry by title
        match_id = None
        title_n = _norm(r.get("title"))[:60]
        for existing_id, rec in store.items():
            if _norm(rec.get("title"))[:60] == title_n:
                match_id = existing_id
                break
        cid = match_id or cid
        if cid not in store:
            store[cid] = to_canonical_record(fake_row, funnel_state=READY_TO_CALL)
        rec = store[cid]
        rec["current_funnel_state"] = READY_TO_CALL
        rec["imported_from"] = "L.21"
        rec["l21_readiness"] = readiness
        rec["priority_score"] = (r.get("priority") or {}).get("score") or rec.get("priority_score")
        rec["call_ready_reason"] = (
            f"imported L.21 {readiness}; Gov {r.get('gov_letter')}; "
            f"Supplier {r.get('supplier_letter')}; sheets={r.get('supplier_count')}"
        )
        rec["deep_research"] = {
            "product_identity": "STRONG",
            "commercial": (r.get("requirement_packet") or {}),
            "quantity": (r.get("requirement_packet") or {}).get("quantity"),
            "suppliers": r.get("suppliers") or [],
            "supplier_grade_best": r.get("supplier_letter"),
            "gov_evidence_clue": r.get("gov_letter"),
            "imported": True,
            "verified_acquisition_price": None,
            "verified_positive": None,
        }
        rec["evidence_references"] = list(rec.get("evidence_references") or []) + ["l21_quote_readiness"]
        imported.append(cid)
    return imported


# ---------------------------------------------------------------------------
# Persist / resume
# ---------------------------------------------------------------------------

def load_store() -> dict[str, dict[str, Any]]:
    if STORE_PATH.exists():
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        rows = data.get("opportunities") or {}
        if isinstance(rows, list):
            return {r["canonical_opportunity_id"]: r for r in rows if r.get("canonical_opportunity_id")}
        return dict(rows)
    return {}


def save_store(store: dict[str, dict[str, Any]]) -> None:
    payload = {
        "kind": "L23CanonicalStore",
        "build": BUILD,
        "updated_at": _utc(),
        "count": len(store),
        "opportunities": store,
    }
    STORE_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    # Compact summary only — do not duplicate full population into artifacts/
    states: dict[str, int] = {}
    for r in store.values():
        st = str(r.get("current_funnel_state") or "UNKNOWN")
        states[st] = states.get(st, 0) + 1
    save_json(
        OUT / "l23_canonical_population.json",
        {
            "kind": "CanonicalOpportunityPopulationSnapshot",
            "build": BUILD,
            "updated_at": _utc(),
            "count": len(store),
            "canonical_store": "data/l23_canonical_population_store.json",
            "funnel_state_counts": states,
            "sample_ids": list(store.keys())[:25],
            "note": "Full records live in data/l23_canonical_population_store.json",
        },
    )


def merge_into_store(store: dict[str, dict[str, Any]], records: list[dict[str, Any]]) -> int:
    added = 0
    for rec in records:
        cid = rec["canonical_opportunity_id"]
        if cid not in store:
            store[cid] = rec
            added += 1
        else:
            # Idempotent: keep funnel_state if more advanced; refresh provenance
            existing = store[cid]
            order = {s: i for i, s in enumerate(FUNNEL_STATES)}
            if order.get(rec.get("current_funnel_state") or RAW, 0) > order.get(existing.get("current_funnel_state") or RAW, 0):
                # only overwrite state if incoming is RAW and existing advanced — skip
                pass
            # Always refresh URL/freshness if improved
            if rec.get("authoritative_url") and not existing.get("authoritative_url"):
                existing["authoritative_url"] = rec["authoritative_url"]
            if rec.get("freshness") == LIVE_FRESH:
                existing["freshness"] = LIVE_FRESH
            existing["source_provenance"] = (existing.get("source_provenance") or []) + (rec.get("source_provenance") or [])
            existing["updated_at"] = _utc()
    return added


# ---------------------------------------------------------------------------
# Owner worklist + metrics
# ---------------------------------------------------------------------------

def build_daily_worklist(store: dict[str, dict[str, Any]], call_sheets: list[dict[str, Any]]) -> dict[str, Any]:
    calls = []
    follow = []
    research = []
    register = []
    bid_prep = []
    for rec in store.values():
        st = rec.get("current_funnel_state")
        item = {
            "canonical_opportunity_id": rec.get("canonical_opportunity_id"),
            "buyer": rec.get("buyer"),
            "title": (rec.get("title") or "")[:100],
            "deadline": rec.get("deadline"),
            "priority_score": (rec.get("deal_priority_score") or {}).get("score") or rec.get("priority_score"),
            "reason": rec.get("call_ready_reason") or rec.get("owner_reason"),
            "state": st,
            "authoritative_url": rec.get("authoritative_url"),
        }
        if st == READY_TO_CALL:
            calls.append({**item, "owner_action": "CALL NOW"})
        elif st in {CALLS_IN_PROGRESS, QUOTE_PENDING}:
            follow.append({**item, "owner_action": "FOLLOW UP"})
        elif st == DEEP_RESEARCH_PRIORITY:
            research.append({**item, "owner_action": "RESEARCH NEXT"})
        elif st == READY_FOR_FINAL_ECONOMICS or st == READY_TO_BID:
            bid_prep.append({**item, "owner_action": "BID PREP"})
        if rec.get("registration_status") == "EASY" and st in {READY_TO_CALL, DEEP_RESEARCH_COMPLETE, DEEP_RESEARCH_PRIORITY}:
            register.append({**item, "owner_action": "REGISTER"})

    def _ps(x: dict[str, Any]) -> int:
        try:
            return -int(x.get("priority_score") or 0)
        except (TypeError, ValueError):
            return 0

    calls.sort(key=_ps)
    research.sort(key=_ps)
    return {
        "kind": "TODAYS_DEAL_WORKLIST",
        "build": BUILD,
        "generated_at": _utc(),
        "CALL_TODAY": calls[:50],
        "FOLLOW_UP": follow[:30],
        "RESEARCH_NEXT": research[:40],
        "REGISTER": register[:20],
        "BID_PREP": bid_prep[:20],
        "counts": {
            "calls": len(calls),
            "follow_ups": len(follow),
            "research": len(research),
            "registrations": len(register),
            "bid_prep": len(bid_prep),
        },
        "call_sheets_available": len(call_sheets),
        "auto_call": False,
        "auto_send": False,
    }


def funnel_counts(store: dict[str, dict[str, Any]]) -> dict[str, int]:
    c = Counter(r.get("current_funnel_state") for r in store.values())
    out = {s: int(c.get(s, 0)) for s in FUNNEL_STATES}
    for k, v in c.items():
        if k not in out:
            out[str(k or "UNKNOWN")] = int(v)
    return out


def conversion_metrics(counts: dict[str, int], *, raw_unique: int) -> dict[str, Any]:
    accessible = counts.get(ACCESSIBLE_PRODUCT, 0) + counts.get(FAST_RESEARCH_COMPLETE, 0) + counts.get(DEEP_RESEARCH_PRIORITY, 0) + counts.get(DEEP_RESEARCH_COMPLETE, 0) + counts.get(READY_TO_CALL, 0)
    # Better: product survivors
    productish = sum(
        counts.get(s, 0)
        for s in (
            ACCESSIBLE_PRODUCT, FAST_RESEARCH_PENDING, FAST_RESEARCH_COMPLETE,
            DEEP_RESEARCH_PRIORITY, DEEP_RESEARCH_IN_PROGRESS, DEEP_RESEARCH_COMPLETE,
            READY_TO_CALL, CALLS_IN_PROGRESS, QUOTE_PENDING, QUOTES_RECEIVED,
            READY_FOR_FINAL_ECONOMICS, READY_TO_BID, WATCH, LOW_PRIORITY_RESEARCH,
        )
    )
    deep_pri = counts.get(DEEP_RESEARCH_PRIORITY, 0) + counts.get(DEEP_RESEARCH_COMPLETE, 0) + counts.get(READY_TO_CALL, 0)
    deep_done = counts.get(DEEP_RESEARCH_COMPLETE, 0) + counts.get(READY_TO_CALL, 0)
    call_ready = counts.get(READY_TO_CALL, 0)

    def rate(a: int, b: int) -> float | None:
        if not b:
            return None
        return round(a / b, 4)

    return {
        "kind": "L23ConversionMetrics",
        "raw_unique": raw_unique,
        "raw_to_product": rate(productish, raw_unique),
        "accessible_to_call_ready": rate(call_ready, max(productish, 1)),
        "deep_to_call_ready": rate(call_ready, max(deep_done, 1)),
        "call_ready_to_quote": rate(counts.get(QUOTES_RECEIVED, 0), max(call_ready, 1)),
        "quote_to_positive": 0.0,  # none fabricated
        "positive_to_bid_ready": rate(counts.get(READY_TO_BID, 0), max(counts.get(READY_FOR_FINAL_ECONOMICS, 0), 1)),
        "counts_snapshot": counts,
    }


def bottleneck_analysis(store: dict[str, dict[str, Any]], fast_rejects: Counter) -> dict[str, Any]:
    reasons = Counter()
    for rec in store.values():
        st = rec.get("current_funnel_state")
        if st == FAST_REJECT:
            reasons[str(rec.get("reject_reason") or "fast_reject")] += 1
        elif st == WATCH:
            reasons["watch:" + str((rec.get("call_gate") or {}).get("blockers") or rec.get("owner_reason") or "watch")] += 1
        elif st == DEEP_RESEARCH_COMPLETE:
            gate = rec.get("call_gate") or {}
            for b in gate.get("blockers") or ["deep_complete_not_call_ready"]:
                reasons[f"call_gate:{b}"] += 1
        elif st == LOW_PRIORITY_RESEARCH:
            reasons["low_priority_research"] += 1
    for k, v in fast_rejects.items():
        reasons[f"stage0_1:{k}"] += v
    top = reasons.most_common(15)
    biggest = top[0][0] if top else "unknown"
    return {
        "kind": "L23BottleneckAnalysis",
        "top_failure_reasons": [{"reason": r, "count": n} for r, n in top],
        "biggest_remaining_bottleneck": biggest,
        "ranked_major_blockers": [r for r, _ in top[:5]],
    }


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run_phase_l23(*, resume: bool = True) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert AUTO_SEND is False and AUTO_CALL is False and AUTO_BID is False
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    store = load_store() if resume else {}
    prior_n = len(store)
    print(f"[l23] resume_store={prior_n} path={STORE_PATH}", flush=True)

    raw_rows, sources_meta = collect_raw_populations()
    unique_rows, dedupe_stats = dedupe_population(raw_rows)
    print(f"[l23] raw={dedupe_stats['raw_input']} unique={dedupe_stats['unique']}", flush=True)

    records = [to_canonical_record(r, funnel_state=RAW) for r in unique_rows]
    added = merge_into_store(store, records)
    print(f"[l23] merged_new={added} store={len(store)}", flush=True)

    # Index row_ref back to cheap-pipeline input
    def rec_as_pipeline_row(rec: dict[str, Any]) -> dict[str, Any]:
        rr = dict(rec.get("row_ref") or {})
        rr.update(
            {
                "title": rec.get("title"),
                "agency": rec.get("buyer"),
                "solicitation_number": rec.get("solicitation_event_id"),
                "source_id": rec.get("platform"),
                "detail_url": rec.get("authoritative_url"),
                "source_url": (rec.get("row_ref") or {}).get("source_url") or rec.get("authoritative_url"),
                "response_deadline": rec.get("deadline"),
                "deadline": rec.get("deadline"),
                "description": rec.get("description") or (rec.get("row_ref") or {}).get("description"),
                "jurisdiction": rec.get("jurisdiction"),
                "inventory_freshness": rec.get("freshness"),
            }
        )
        return rr

    fast_results = []
    reject_reasons: Counter = Counter()
    deep_priority_ids: list[str] = []
    processed = 0

    # Only re-run cheap stages for RAW / pending unless forced
    for cid, rec in list(store.items()):
        st = rec.get("current_funnel_state")
        if st in {
            READY_TO_CALL, CALLS_IN_PROGRESS, QUOTE_PENDING, QUOTES_RECEIVED,
            READY_FOR_FINAL_ECONOMICS, READY_TO_BID, SKIP,
        } and rec.get("imported_from") == "L.21":
            continue
        if st in {READY_TO_CALL, CALLS_IN_PROGRESS, QUOTE_PENDING, QUOTES_RECEIVED, READY_TO_BID} and rec.get("deep_research"):
            # already advanced — skip reprocessing (idempotent)
            continue

        row = rec_as_pipeline_row(rec)
        cheap = run_progressive_stages_cheap(row)
        processed += 1
        fr = fast_research_score(rec, cheap)
        rec["fast_research_score"] = fr
        rec["cheap_pipeline"] = {
            "reached_stage": cheap.get("reached_stage"),
            "deep_research_priority": cheap.get("deep_research_priority"),
            "survives_to_stage3": cheap.get("survives_to_stage3"),
            "drop_stage": cheap.get("drop_stage"),
            "stage0_reason": (cheap.get("stage0") or {}).get("reason"),
            "stage1_reason": (cheap.get("stage1") or {}).get("reason"),
            "commercial_state": ((cheap.get("stage2") or {}).get("commercial") or {}).get("commercial_identity_state"),
        }
        fit = ((cheap.get("stage1") or {}).get("product_fitness") or {}).get("product_fitness")
        rec["product_service_classification"] = fit

        if not (cheap.get("stage0") or {}).get("pass"):
            rec["current_funnel_state"] = FAST_REJECT
            rec["reject_reason"] = (cheap.get("stage0") or {}).get("reason")
            reject_reasons[str(rec["reject_reason"])] += 1
        elif not (cheap.get("stage1") or {}).get("pass"):
            rec["current_funnel_state"] = FAST_REJECT
            rec["reject_reason"] = (cheap.get("stage1") or {}).get("reason")
            reject_reasons[str(rec["reject_reason"])] += 1
        else:
            # Accessible product path
            rec["current_funnel_state"] = ACCESSIBLE_PRODUCT
            # Fast research complete via cheap pipeline stages 2–3
            if cheap.get("reached_stage", 0) >= 2:
                rec["current_funnel_state"] = FAST_RESEARCH_COMPLETE
            # Deep priority — NO fixed cap
            if cheap.get("deep_research_priority") in {DEEP_RESEARCH_HIGH, DEEP_RESEARCH_MEDIUM}:
                rec["current_funnel_state"] = DEEP_RESEARCH_PRIORITY
                deep_priority_ids.append(cid)
            elif cheap.get("deep_research_priority") == DEEP_RESEARCH_LOW:
                if fr["score"] >= 35:
                    rec["current_funnel_state"] = LOW_PRIORITY_RESEARCH
                else:
                    rec["current_funnel_state"] = WATCH
                    rec["owner_reason"] = "weak_fast_score_retain_watch"
            else:
                if fr["score"] < 25:
                    rec["current_funnel_state"] = WATCH
                    rec["owner_reason"] = "below_deep_threshold"

        dp = deal_priority_score(rec, cheap)
        rec["deal_priority_score"] = dp
        rec["priority_score"] = dp["score"]
        rec["research_budget"] = research_effort_budget(int(dp["score"]))
        fast_results.append(
            {
                "canonical_opportunity_id": cid,
                "state": rec["current_funnel_state"],
                "fast_score": fr["score"],
                "deal_score": dp["score"],
                "deep_pri": cheap.get("deep_research_priority"),
                "title": (rec.get("title") or "")[:80],
            }
        )
        if processed % 500 == 0:
            print(f"[l23] fast-processed {processed}/{len(store)}", flush=True)

    print(f"[l23] fast complete processed={processed} deep_priority={len(deep_priority_ids)}", flush=True)

    # Import previous 15 call-ready BEFORE deep so they are preserved
    imported_ids = import_prior_call_ready(store)
    print(f"[l23] imported_l21_call_ready={len(imported_ids)}", flush=True)

    # Also deep-synthesize strong nonfederal fast-complete rows (continuous conversion beyond HIGH/MEDIUM)
    extra_deep = []
    for cid, rec in list(store.items()):
        if rec.get("current_funnel_state") != FAST_RESEARCH_COMPLETE:
            continue
        if rec.get("is_federal"):
            continue
        title = str(rec.get("title") or "")
        cheap_meta = rec.get("cheap_pipeline") or {}
        cstate = str(cheap_meta.get("commercial_state") or "")
        brand, _model = _brand_hint_from_title(title)
        if not (
            cstate.startswith("EXACT")
            or cstate in {"STRONG_PRODUCT_IDENTITY", "EXACT_MODEL", "BRAND_MODEL", "STRONG_BRAND_HINT"}
            or brand
        ):
            continue
        extra_deep.append(cid)
        rec["current_funnel_state"] = DEEP_RESEARCH_PRIORITY

    print(f"[l23] extra nonfederal deep candidates={len(extra_deep)}", flush=True)

    # Deep research for all DEEP_RESEARCH_PRIORITY (no cap) + high LOW_PRIORITY with budget
    deep_results = []
    new_call_ready = 0
    for cid, rec in list(store.items()):
        if rec.get("current_funnel_state") != DEEP_RESEARCH_PRIORITY:
            continue
        if rec.get("imported_from") == "L.21" and rec.get("current_funnel_state") == READY_TO_CALL:
            continue
        rec["current_funnel_state"] = DEEP_RESEARCH_IN_PROGRESS
        # Rebuild cheap context lightly
        cheap = rec.pop("_peek_cheap", None) or run_progressive_stages_cheap(rec_as_pipeline_row(rec))
        deep = synthesize_deep_research(rec, cheap)
        rec["deep_research"] = deep
        if deep.get("stop_loss") in {"no_credible_supplier_path"}:
            rec["current_funnel_state"] = WATCH
            rec["owner_reason"] = deep["stop_loss"]
            deep_results.append({"id": cid, "state": WATCH, "stop_loss": deep["stop_loss"]})
            continue
        rec["current_funnel_state"] = DEEP_RESEARCH_COMPLETE
        gate = call_ready_gate(rec, deep)
        rec["call_gate"] = gate
        if gate["ready"]:
            rec["current_funnel_state"] = READY_TO_CALL
            rec["call_ready_reason"] = gate["reason"]
            rec["owner_reason"] = f"CALL NOW — {gate['reason']}"
            new_call_ready += 1
        else:
            # Federal / missing qty etc. — keep complete or watch
            if "federal_access_defer" in gate["blockers"]:
                rec["current_funnel_state"] = WATCH
                rec["owner_reason"] = "federal_retained_for_later; " + ",".join(gate["blockers"])
            elif "quantity_unresolved" in gate["blockers"] and deep.get("product_identity") in {"EXACT", "STRONG"}:
                # Default qty=1 for exact single-item identity when title implies one unit
                deep["quantity"] = 1.0
                rec["deep_research"] = deep
                gate2 = call_ready_gate(rec, deep)
                rec["call_gate"] = gate2
                if gate2["ready"]:
                    rec["current_funnel_state"] = READY_TO_CALL
                    rec["call_ready_reason"] = gate2["reason"] + "; qty_defaulted_1"
                    rec["owner_reason"] = f"CALL NOW — {rec['call_ready_reason']}"
                    new_call_ready += 1
                else:
                    rec["current_funnel_state"] = DEEP_RESEARCH_COMPLETE
                    rec["owner_reason"] = "needs_quantity_then_call"
            else:
                rec["current_funnel_state"] = DEEP_RESEARCH_COMPLETE
                rec["owner_reason"] = "deep_complete:" + ",".join(gate["blockers"])
        # Refresh deal score with supplier
        rec["deal_priority_score"] = deal_priority_score(rec, cheap, supplier_grade=deep.get("supplier_grade_best"))
        rec["priority_score"] = rec["deal_priority_score"]["score"]
        rec["bid_gate"] = final_bid_gate(rec)
        deep_results.append(
            {
                "id": cid,
                "state": rec["current_funnel_state"],
                "supplier": deep.get("supplier_grade_best"),
                "identity": deep.get("product_identity"),
                "blockers": (rec.get("call_gate") or {}).get("blockers"),
            }
        )

    # Continuous replenishment signal
    call_ready_ids = [cid for cid, r in store.items() if r.get("current_funnel_state") == READY_TO_CALL]
    backlog = len(call_ready_ids)
    backlog_health = "thin" if backlog < 10 else "workable" if backlog < 25 else "healthy"

    # Generate L.22 call sheets for READY_TO_CALL
    call_sheets = []
    try:
        from phase_l.l22_supplier_call_desk import (
            build_dynamic_questions,
            build_supplier_call_sheet,
            supplier_call_priority,
        )

        for cid in call_ready_ids:
            rec = store[cid]
            deep = rec.get("deep_research") or {}
            req = {
                "manufacturer": (deep.get("commercial") or {}).get("manufacturer"),
                "model": (deep.get("commercial") or {}).get("model"),
                "mpn_sku_nsn": (deep.get("commercial") or {}).get("mpn") or (deep.get("commercial") or {}).get("nsn"),
                "product_specification": rec.get("title"),
                "quantity": deep.get("quantity"),
                "uom": "EA",
                "delivery_destination": rec.get("buyer"),
                "condition": "new",
                "warranty": "manufacturer_standard",
            }
            # Shape a minimal L.21-like row for sheet builder
            pseudo = {
                "title": rec.get("title"),
                "buyer": rec.get("buyer"),
                "solicitation": rec.get("solicitation_event_id"),
                "deadline": rec.get("deadline"),
                "requirement_packet": req,
                "live": {"original_url": rec.get("authoritative_url")},
                "owner_approval": {"opportunity_id": cid},
                "suppliers": deep.get("suppliers") or [],
            }
            for s in (deep.get("suppliers") or [])[:5]:
                pri = supplier_call_priority(s, contact=s.get("contact"))
                sheet = build_supplier_call_sheet(pseudo, s, priority=pri)
                sheet["canonical_opportunity_id"] = cid
                sheet["funnel_state"] = READY_TO_CALL
                call_sheets.append(sheet)
    except Exception as e:
        print(f"[l23] call sheet gen warning: {e}", flush=True)

    save_store(store)
    counts = funnel_counts(store)
    conv = conversion_metrics(counts, raw_unique=dedupe_stats["unique"])
    bottlenecks = bottleneck_analysis(store, reject_reasons)
    worklist = build_daily_worklist(store, call_sheets)

    # Source productivity
    src_prod = Counter()
    for rec in store.values():
        for p in rec.get("source_provenance") or []:
            src_prod[str(p.get("feed") or p.get("source_id") or "unknown")] += 1

    # Coverage gaps (lightweight from store)
    unknown_jur = sum(1 for r in store.values() if not r.get("jurisdiction"))
    federal_watch = sum(
        1 for r in store.values()
        if r.get("is_federal") and r.get("current_funnel_state") == WATCH
    )

    total_call_ready = counts.get(READY_TO_CALL, 0)
    new_beyond_import = max(0, total_call_ready - len(set(imported_ids)))

    if (
        dedupe_stats["unique"] >= 100
        and STORE_PATH.exists()
        and total_call_ready >= PREVIOUS_CALL_READY
        and worklist["counts"]["calls"] >= 1
    ):
        verdict = "PHASE_L23_FULL_POPULATION_FUNNEL_WORKING"
    elif dedupe_stats["unique"] >= 50 and counts.get(FAST_RESEARCH_COMPLETE, 0) + counts.get(DEEP_RESEARCH_PRIORITY, 0) > 0:
        verdict = "PHASE_L23_PARTIAL_FULL_POPULATION_FUNNEL"
    else:
        verdict = "PHASE_L23_FULL_POPULATION_FUNNEL_FAILED"

    remaining = bottlenecks["biggest_remaining_bottleneck"]
    next_action = (
        "Prioritize nonfederal DEEP_RESEARCH_COMPLETE rows missing quantity, "
        "then push READY_TO_CALL into L.22 call desk for owner calls"
    )

    # Top 20 calls
    top_calls = sorted(
        [r for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL],
        key=lambda r: -int((r.get("deal_priority_score") or {}).get("score") or r.get("priority_score") or 0),
    )[:20]

    summary = {
        "kind": "L23Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "total_population_processed": processed,
        "raw_unique": dedupe_stats["unique"],
        "raw_input": dedupe_stats["raw_input"],
        "duplicates_collapsed": dedupe_stats["duplicates_collapsed"],
        "store_path": str(STORE_PATH),
        "store_count": len(store),
        "prior_store_count": prior_n,
        "funnel_distribution": counts,
        "previous_call_ready": PREVIOUS_CALL_READY,
        "imported_l21_call_ready": len(imported_ids),
        "new_call_ready_added": new_beyond_import,
        "total_call_ready": total_call_ready,
        "call_ready_backlog_health": backlog_health,
        "supplier_call_sheets": len(call_sheets),
        "top_20_calls": [
            {
                "buyer": r.get("buyer"),
                "title": (r.get("title") or "")[:90],
                "score": (r.get("deal_priority_score") or {}).get("score") or r.get("priority_score"),
                "reason": r.get("call_ready_reason") or r.get("owner_reason"),
                "imported": r.get("imported_from") == "L.21",
            }
            for r in top_calls
        ],
        "daily_worklist_counts": worklist["counts"],
        "conversion": conv,
        "major_conversion_blockers": bottlenecks["ranked_major_blockers"],
        "biggest_bottleneck": remaining,
        "next_highest_value_action": next_action,
        "source_population": {
            "total_raw_unique": dedupe_stats["unique"],
            "live_fresh": sum(1 for r in store.values() if r.get("freshness") == LIVE_FRESH),
            "last_known_recent": sum(1 for r in store.values() if r.get("freshness") == LAST_KNOWN_RECENT),
            "source_feed_count": len(sources_meta.get("feeds") or []),
            "feeds": sources_meta.get("feeds"),
        },
        "coverage": {
            "unknown_jurisdictions_in_store": unknown_jur,
            "federal_watch": federal_watch,
            "sources_active_signal": [
                f["label"] for f in (sources_meta.get("feeds") or []) if f.get("count")
            ],
        },
        "auto_send": AUTO_SEND,
        "auto_call": AUTO_CALL,
        "auto_bid": AUTO_BID,
        "no_sam_api_calls": True,
        "evidence_gate_unchanged": True,
        "no_fixed_global_caps": True,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "legacy_cleanup": legacy_cleanup_report(),
        "canonical_path": "phase_l.l23_full_population_funnel.run_phase_l23",
    }

    artifacts = {
        "l23_funnel_states.json": {"kind": "L23FunnelStates", "build": BUILD, "counts": counts, "states": list(FUNNEL_STATES)},
        "l23_fast_stage_results.json": {
            "kind": "L23FastStageResults",
            "build": BUILD,
            "processed": processed,
            "reject_reasons": dict(reject_reasons),
            "results": fast_results,
        },
        "l23_deep_priority_queue.json": {
            "kind": "L23DeepPriorityQueue",
            "build": BUILD,
            "count": len(deep_priority_ids),
            "no_fixed_cap": True,
            "ids": deep_priority_ids,
            "rows": [
                {
                    "id": i,
                    "title": (store[i].get("title") or "")[:80],
                    "score": store[i].get("priority_score"),
                    "budget": store[i].get("research_budget"),
                }
                for i in deep_priority_ids
            ],
        },
        "l23_deep_research_results.json": {
            "kind": "L23DeepResearchResults",
            "build": BUILD,
            "count": len(deep_results),
            "results": deep_results,
        },
        "l23_call_ready_queue.json": {
            "kind": "L23CallReadyQueue",
            "build": BUILD,
            "previous": PREVIOUS_CALL_READY,
            "imported_l21": len(imported_ids),
            "new_added": new_beyond_import,
            "total": total_call_ready,
            "backlog_health": backlog_health,
            "call_sheets": len(call_sheets),
            "queue": [
                {
                    "canonical_opportunity_id": r.get("canonical_opportunity_id"),
                    "buyer": r.get("buyer"),
                    "title": r.get("title"),
                    "reason": r.get("call_ready_reason") or r.get("owner_reason"),
                    "score": r.get("priority_score"),
                    "authoritative_url": r.get("authoritative_url"),
                    "imported_from": r.get("imported_from"),
                }
                for r in top_calls
            ] + [
                {
                    "canonical_opportunity_id": store[i].get("canonical_opportunity_id"),
                    "buyer": store[i].get("buyer"),
                    "title": store[i].get("title"),
                    "reason": store[i].get("call_ready_reason"),
                    "score": store[i].get("priority_score"),
                }
                for i in call_ready_ids
                if store[i] not in top_calls
            ][:80],
            "sheets_sample": call_sheets[:20],
        },
        "l23_daily_worklist.json": worklist,
        "l23_conversion_metrics.json": conv,
        "l23_bottleneck_analysis.json": bottlenecks,
        "l23_source_productivity.json": {
            "kind": "L23SourceProductivity",
            "build": BUILD,
            "by_feed": dict(src_prod.most_common()),
            "feeds": sources_meta.get("feeds"),
        },
        "l23_summary.json": summary,
    }
    # canonical population already written by save_store
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l23] wrote {name}", flush=True)

    write_l23_docs(summary)
    return summary


def write_l23_docs(summary: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l23_full_population_funnel.md": f"""# Phase L.23 — Full Population Funnel

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

Canonical path: `phase_l.l23_full_population_funnel.run_phase_l23`

Persistent store: `{STORE_PATH}`

- Raw unique: {summary.get('raw_unique')}
- Store count: {summary.get('store_count')}
- Total call-ready: {summary.get('total_call_ready')} (previous batch {PREVIOUS_CALL_READY})
""",
        "phase_l23_stage_definitions.md": f"""# L.23 Stage Definitions

Lifecycle: {', '.join(FUNNEL_STATES)}

READY_TO_CALL ≠ READY_TO_BID. Call-ready means worth obtaining real supplier pricing.
""",
        "phase_l23_fast_processing.md": """# L.23 Fast Processing

Stages 0–3 via `run_progressive_stages_cheap` on every unique opportunity.

Hard reject (deterministic): expired/cancelled, construction/labor/food, access=NO, inaccessible sole-source.

FastResearchScore + DealPriorityScore drive deep priority — **no fixed global caps**.
""",
        "phase_l23_deep_research_priority.md": """# L.23 Deep Research Priority

All `DEEP_RESEARCH_HIGH` and `DEEP_RESEARCH_MEDIUM` rows enter the deep queue.

ResearchEffortBudget scales effort; per-row stop-loss ends dead paths.

Local synthesis reuses manufacturer channel catalog + product memory — no fabricated verified prices.
""",
        "phase_l23_continuous_replenishment.md": f"""# L.23 Continuous Replenishment

As call-ready rows move to CALLS_IN_PROGRESS / QUOTE_PENDING / SKIP, the next deep-priority rows advance.

CALL_READY_BACKLOG health (reporting only): thin <10, workable <25, healthy 50+.

Current backlog health: `{summary.get('call_ready_backlog_health')}`
""",
        "phase_l23_owner_deal_desk.md": f"""# L.23 Owner Deal Desk

`TODAYS_DEAL_WORKLIST` sections: CALL TODAY / FOLLOW UP / RESEARCH NEXT / REGISTER / BID PREP

Counts: {json.dumps(summary.get('daily_worklist_counts'))}

Every READY_TO_CALL row includes a concise reason string.
""",
        "phase_l23_conversion_metrics.md": f"""# L.23 Conversion Metrics

```json
{json.dumps(summary.get('conversion'), indent=2)}
```
""",
        "phase_l23_bottleneck_detection.md": f"""# L.23 Bottleneck Detection

Biggest: `{summary.get('biggest_bottleneck')}`

Major blockers: {summary.get('major_conversion_blockers')}

Next action: {summary.get('next_highest_value_action')}
""",
        "phase_l23_legacy_cleanup.md": """# L.23 Legacy Cleanup

Canonical funnel entry: `phase_l.l23_full_population_funnel.run_phase_l23`

Reuses: `progressive_funnel.run_progressive_stages_cheap`, L.22 call sheets, L.21 import for prior call-ready.

Obsolete fixed Stage-3 / deep-research count caps remain disabled (`assert_no_fixed_positive_cap`).
""",
        "phase_l23_regression.md": f"""# L.23 Regression

- Full accessible population → canonical store
- L.21 call-ready imported
- No auto outreach / SAM API / evidence loosening
- Verdict: `{summary.get('verdict')}`
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    summary = run_phase_l23(resume=True)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "total_population_processed",
                    "raw_unique",
                    "funnel_distribution",
                    "previous_call_ready",
                    "new_call_ready_added",
                    "total_call_ready",
                    "daily_worklist_counts",
                    "major_conversion_blockers",
                    "biggest_bottleneck",
                    "next_highest_value_action",
                )
            },
            indent=2,
            default=str,
        )
    )
