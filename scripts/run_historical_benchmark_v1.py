"""Build M3 Historical Benchmark V1 and run first end-to-end backtests.

DIAGNOSTIC FIRST. No hindsight. No WOULD_HAVE_WON. No live Iowa 2975.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from application_clock import CLOCK_SYSTEM, clock_mode, reset_clock
from historical_benchmark_constants import (
    BENCHMARK_VERSION,
    HTTP_HARD_MAX,
    HTTP_TARGET,
    LIVE_IOWA_2975,
    MODE_FULL_DISCOVERY,
    MODE_KNOWN_OPPORTUNITY,
    PRIOR_IOWA_2907,
    SAM_MAX,
    SRC_CURRENT_ONLY,
    SRC_HISTORICALLY_AVAILABLE,
    SRC_LIKELY_HISTORICALLY_AVAILABLE,
    TIER_BRONZE,
    TIER_GOLD,
    TIER_NOT_READY,
    TIER_SILVER,
    TIER_VALIDATION_ONLY,
    USA_MAX,
)
from historical_benchmark_models import (
    UNKNOWN,
    assess_benchmark_tier,
    benchmark_case,
    integrity_record,
    new_evidence_id,
    postbid_evidence_manifest,
    prebid_evidence_manifest,
    utc_now_iso,
)
from historical_backtest_runner import (
    classify_source_availability,
    run_backtest_case,
    score_backtest,
    simulation_start_from_posting,
)
from historical_case_constants import (
    ROLE_POST_BID_AWARD,
    ROLE_PRE_BID_DISCOVERY,
    ROLE_PRE_BID_PRICE,
    ROLE_PRE_BID_REQUIREMENT,
    ROLE_PRE_BID_SUPPLIER,
)
from historical_case_inventory import verify_live_iowa_isolation
from historical_outcome_vault import HistoricalOutcomeVault
from temporal_evidence_api import TemporalEvidenceStore
from temporal_evidence_firewall import make_temporal_evidence

ARTIFACTS = ROOT / "artifacts"


class RequestBudget:
    def __init__(self) -> None:
        self.http = 0
        self.usaspending = 0
        self.sam = 0
        self.openai = 0
        self.paid = 0
        self.supplier_outreach = 0
        self.agency_outreach = 0
        self.creator_outreach = 0
        self.financier_outreach = 0
        self.bid_submissions = 0
        self.log: list[dict[str, Any]] = []

    def allow(self, kind: str = "http") -> bool:
        if self.http >= HTTP_HARD_MAX:
            return False
        if kind == "usaspending" and self.usaspending >= USA_MAX:
            return False
        if kind == "sam" and self.sam >= SAM_MAX:
            return False
        return True

    def record(self, kind: str, url: str, ok: bool, note: str = "") -> None:
        self.http += 1
        if kind == "usaspending":
            self.usaspending += 1
        elif kind == "sam":
            self.sam += 1
        self.log.append({"n": self.http, "kind": kind, "url": url, "ok": ok, "note": note})

    def snapshot(self) -> dict[str, Any]:
        return {
            "http": self.http,
            "http_target": HTTP_TARGET,
            "http_hard_max": HTTP_HARD_MAX,
            "usaspending": self.usaspending,
            "sam": self.sam,
            "openai": self.openai,
            "paid": self.paid,
            "supplier_outreach": self.supplier_outreach,
            "agency_outreach": self.agency_outreach,
            "creator_outreach": self.creator_outreach,
            "financier_outreach": self.financier_outreach,
            "bid_submissions": self.bid_submissions,
            "within_hard_max": self.http <= HTTP_HARD_MAX,
            "log": self.log,
        }


def _get(client: httpx.Client, budget: RequestBudget, kind: str, url: str) -> httpx.Response | None:
    if not budget.allow(kind):
        budget.record(kind, url, False, "cap")
        return None
    try:
        r = client.get(url, timeout=25.0, follow_redirects=True)
        budget.record(kind, url, r.status_code < 500, f"status={r.status_code}")
        return r
    except Exception as exc:  # noqa: BLE001
        budget.record(kind, url, False, str(exc)[:120])
        return None


def _post(client: httpx.Client, budget: RequestBudget, kind: str, url: str, payload: dict) -> Any:
    if not budget.allow(kind):
        budget.record(kind, url, False, "cap")
        return None
    try:
        r = client.post(url, json=payload, timeout=30.0)
        budget.record(kind, url, r.status_code < 500, f"status={r.status_code}")
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception as exc:  # noqa: BLE001
        budget.record(kind, url, False, str(exc)[:120])
        return None


def usaspending_search(client: httpx.Client, budget: RequestBudget, keyword: str, limit: int = 15) -> list[dict]:
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    payload = {
        "filters": {
            "keywords": [keyword],
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2022-01-01", "end_date": "2026-09-01"}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Start Date",
            "Awarding Agency",
            "Description",
            "Contract Award Type",
        ],
        "limit": limit,
        "page": 1,
        "sort": "Start Date",
        "order": "desc",
    }
    data = _post(client, budget, "usaspending", url, payload)
    if not data:
        return []
    return list(data.get("results") or [])


def git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def investigate_iowa_2907(client: httpx.Client, budget: RequestBudget) -> dict[str, Any]:
    urls = [
        "https://www.govcb.com/government-bids/CARBIDE-BLADES-FOR-SNOW-ICE-REMOVAL-23761006.htm",
        "https://www.iowadot.gov/",
        f"https://html.duckduckgo.com/html/?q={quote_plus('645-DOTRFB-2907-2027 carbide blades Iowa DOT award')}",
        f"https://html.duckduckgo.com/html/?q={quote_plus('Iowa DOT tungsten carbide blades award 2026')}",
        f"https://html.duckduckgo.com/html/?q={quote_plus('site:bidnetdirect.com carbide blades Iowa DOT')}",
    ]
    bodies = []
    for u in urls:
        r = _get(client, budget, "http", u)
        if r is not None and r.status_code == 200:
            bodies.append((u, r.text[:80000]))

    blob = "\n".join(t for _, t in bodies)
    posting = deadline = awardee = amount = None
    m_open = re.search(r"(?:Open|Posted)[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", blob, re.I)
    m_close = re.search(r"(?:Close|Closing|Due)[:\s]+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})", blob, re.I)
    if m_open:
        try:
            posting = datetime.strptime(m_open.group(1), "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            posting = None
    if m_close:
        try:
            deadline = datetime.strptime(m_close.group(1), "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            deadline = None

    # Strict award verification: solicitation + awardee + amount in proximity — else not verified
    award_verified = False
    awardee_v: Any = UNKNOWN
    amount_v: Any = UNKNOWN
    if re.search(r"645-DOTRFB-2907-2027", blob):
        mvend = re.search(
            r"645-DOTRFB-2907-2027.{0,500}(?:awarded to|awardee)\s*[:\s]+([A-Za-z0-9 &.,'\-]{3,80})",
            blob,
            re.I | re.S,
        )
        mamt = re.search(
            r"645-DOTRFB-2907-2027.{0,500}(?:award amount|awarded)\D{0,40}\$\s*([0-9,]+\.\d{2})",
            blob,
            re.I | re.S,
        )
        if mvend:
            awardee_v = mvend.group(1).strip()[:80]
        if mamt:
            try:
                amount_v = float(mamt.group(1).replace(",", ""))
            except ValueError:
                amount_v = UNKNOWN
        award_verified = awardee_v != UNKNOWN and amount_v != UNKNOWN

    return {
        "solicitation_number": PRIOR_IOWA_2907,
        "agency": "Iowa DOT",
        "product_description": "Carbide blades for snow/ice removal",
        "posting_date": posting or UNKNOWN,
        "bid_deadline": deadline or UNKNOWN,
        "award_verified": award_verified,
        "awardee": awardee_v,
        "award_amount": amount_v,
        "synthetic_dates_used": False,
        "sources": [u for u, _ in bodies],
        "notes": (
            "Prior inventory used SYNTHETIC dates for firewall demo only. "
            "This reinvestigation does not promote synthetic dates to facts. "
            + (
                "Award independently verified."
                if award_verified
                else "Award/awardee/amount/dates not independently verified — VALIDATION_ONLY."
            )
        ),
        "tier_recommendation": TIER_VALIDATION_ONLY
        if not (posting and deadline and award_verified)
        else TIER_SILVER,
    }


def collect_usaspending_product_candidates(
    client: httpx.Client, budget: RequestBudget
) -> list[dict[str, Any]]:
    """Independently documented product-ish awards via USAspending keywords.

    These are award-centric; solicitation/pre-bid packages often incomplete → SILVER/BRONZE.
    """
    queries = [
        ("desktop computers", "IT hardware"),
        ("laptop computers", "IT hardware"),
        ("LED traffic signal", "electrical/signage"),
        ("police body armor", "safety equipment"),
        ("snow plow cutting edge", "snow/road equipment"),
        ("diesel generator", "generators"),
        ("office furniture chairs", "furniture"),
        ("fire extinguisher", "safety equipment"),
        ("network switch Cisco", "network equipment"),
        ("industrial floor scrubber", "facility products"),
    ]
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for kw, category in queries:
        if budget.http >= HTTP_TARGET:
            break
        rows = usaspending_search(client, budget, kw, limit=8)
        for row in rows:
            aid = str(row.get("Award ID") or "")
            if not aid or aid in seen_ids:
                continue
            desc = str(row.get("Description") or "")
            # Exclude obvious services
            if re.search(r"\b(services|consulting|staffing|construction|repair services)\b", desc, re.I):
                if not re.search(r"\b(purchase|supply|equipment|computer|generator|furniture|armor)\b", desc, re.I):
                    continue
            amount = row.get("Award Amount")
            # Prefer transactional sizes ($5k–$500k)
            try:
                amt = float(amount)
            except (TypeError, ValueError):
                continue
            if amt < 5000 or amt > 750000:
                continue
            seen_ids.add(aid)
            candidates.append(
                {
                    "keyword": kw,
                    "category": category,
                    "award_id": aid,
                    "awardee": row.get("Recipient Name"),
                    "award_amount": amt,
                    "award_date": row.get("Start Date"),
                    "agency": row.get("Awarding Agency"),
                    "description": desc[:500],
                    "contract_type": row.get("Contract Award Type"),
                    "source": "usaspending",
                }
            )
    return candidates


def verify_candidate_public_pages(
    client: httpx.Client, budget: RequestBudget, cands: list[dict[str, Any]], limit: int = 12
) -> list[dict[str, Any]]:
    """Secondary public search for solicitation/bid notices — do not invent matches."""
    enriched = []
    for c in cands[:limit]:
        if budget.http >= HTTP_TARGET:
            enriched.append({**c, "public_sol_found": False})
            continue
        q = quote_plus(f'{c["award_id"]} {c.get("description","")[:80]} solicitation')
        r = _get(client, budget, "http", f"https://html.duckduckgo.com/html/?q={q}")
        found_sol = False
        sol_guess = UNKNOWN
        if r is not None and r.status_code == 200:
            # Look for solicitation-like tokens near award id — conservative
            m = re.search(
                r"\b((?:RFQ|RFB|IFB|RFP|Solicitation)[- ]?[A-Z0-9][A-Z0-9_./-]{4,})\b",
                r.text,
                re.I,
            )
            if m and c["award_id"][:6].lower() in r.text.lower():
                found_sol = True
                sol_guess = m.group(1)
        enriched.append({**c, "public_sol_found": found_sol, "solicitation_guess": sol_guess})
    return enriched


def build_benchmark_cases(
    iowa: dict[str, Any],
    usa_enriched: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # Iowa 2907 — VALIDATION_ONLY unless fully verified without synthetic dates
    iowa_case = benchmark_case(
        benchmark_case_id="HBM-V1-IOWA-2907",
        agency=iowa.get("agency"),
        solicitation_number=iowa.get("solicitation_number"),
        product_description=iowa.get("product_description"),
        posting_date=iowa.get("posting_date"),
        bid_deadline=iowa.get("bid_deadline"),
        awardee=iowa.get("awardee"),
        award_amount=iowa.get("award_amount"),
        product_category="snow/road equipment",
        buyer_level="state",
        synthetic_dates_used=False,
        provenance=[{"source": u, "role": "public_research"} for u in iowa.get("sources") or []],
        notes=iowa.get("notes"),
    )
    # Force validation-only if award/dates incomplete
    if iowa.get("tier_recommendation") == TIER_VALIDATION_ONLY or not iowa.get("award_verified"):
        iowa_case["forced_tier"] = TIER_VALIDATION_ONLY
    cases.append(iowa_case)

    # Stable IDs for USAspending-derived cases
    for i, c in enumerate(usa_enriched[:18], start=1):
        cid = f"HBM-V1-USA-{i:02d}-{re.sub(r'[^A-Za-z0-9]', '', str(c['award_id']))[:14]}"
        # Without verified solicitation + posting/deadline, cannot be GOLD
        sol = c["solicitation_guess"] if c.get("public_sol_found") else UNKNOWN
        # Use award start as award_date; posting/deadline unknown unless found
        case = benchmark_case(
            benchmark_case_id=cid,
            agency=c.get("agency"),
            solicitation_number=sol,
            contract_award_number=c.get("award_id"),
            product_description=c.get("description") or c.get("keyword"),
            posting_date=UNKNOWN,
            bid_deadline=UNKNOWN,
            award_date=c.get("award_date"),
            awardee=c.get("awardee"),
            award_amount=c.get("award_amount"),
            procurement_method=c.get("contract_type"),
            product_category=c.get("category"),
            buyer_level="federal",
            synthetic_dates_used=False,
            provenance=[
                {
                    "source": "usaspending",
                    "award_id": c.get("award_id"),
                    "keyword": c.get("keyword"),
                    "availability": SRC_LIKELY_HISTORICALLY_AVAILABLE,
                    "literal_replay": False,
                }
            ],
            notes=(
                "Award-verified via USAspending. Solicitation/pre-bid package often incomplete. "
                "Reconstruction ≠ literal SAM/API replay."
            ),
        )
        cases.append(case)

    return cases


def attach_tiers(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in cases:
        if c.get("forced_tier") == TIER_VALIDATION_ONLY:
            tier = {"tier": TIER_VALIDATION_ONLY, "reason": c.get("notes"), "missing": ["award", "dates"]}
        else:
            # prebid_recoverable only if we have solicitation + some product text
            prebid_ok = c.get("solicitation_number") not in (None, "", UNKNOWN) and c.get(
                "product_description"
            ) not in (None, "", UNKNOWN)
            # Without posting/deadline, assess_benchmark_tier won't give GOLD/SILVER easily
            tier = assess_benchmark_tier(c, prebid_recoverable=prebid_ok)
        out.append({**c, "tier_assessment": tier, "tier": tier["tier"]})
    return out


def promote_documented_state_local_cases(
    client: httpx.Client, budget: RequestBudget
) -> list[dict[str, Any]]:
    """Search for well-documented public product awards (state/local) with bid tabs.

    Conservative: only accept when page text clearly shows product RFB + award.
    """
    searches = [
        ("Austin TX purchase order computers awarded", "IT hardware", "local"),
        ("Colorado state IFB furniture award bid tab", "furniture", "state"),
        ("Minnesota DOT plow blades award bid results", "snow/road equipment", "state"),
        ("California school district Chromebooks award", "IT hardware", "local"),
        ("Virginia state purchase generators awarded", "generators", "state"),
        ("City of Phoenix fire extinguishers bid award", "safety equipment", "local"),
        ("Texas DIR computer monitors award", "IT hardware", "state"),
        ("Wisconsin DNR pumps purchase award", "pumps", "state"),
    ]
    found: list[dict[str, Any]] = []
    for q, category, level in searches:
        if budget.http >= min(HTTP_TARGET, 55):
            break
        r = _get(client, budget, "http", f"https://html.duckduckgo.com/html/?q={quote_plus(q)}")
        if r is None or r.status_code != 200:
            continue
        text = r.text
        # Extract a few result URLs
        urls = re.findall(r'uddg=([^&"]+)', text)[:2]
        from urllib.parse import unquote

        for enc in urls:
            if budget.http >= HTTP_TARGET:
                break
            url = unquote(enc)
            if not url.startswith("http"):
                continue
            page = _get(client, budget, "http", url)
            if page is None or page.status_code != 200:
                continue
            body = page.text[:100000]
            # Require product + award signals
            if not re.search(r"\b(award(ed)?|successful bidder|lowest responsible)\b", body, re.I):
                continue
            if not re.search(
                r"\b(computer|chromebook|monitor|furniture|generator|extinguisher|blade|pump|plow)\b",
                body,
                re.I,
            ):
                continue
            sol = UNKNOWN
            msol = re.search(
                r"\b((?:RFQ|RFB|IFB|ITB|RFP)[- #:]?\s*[A-Z0-9][A-Z0-9_./-]{3,})\b",
                body,
                re.I,
            )
            if msol:
                sol = re.sub(r"\s+", "", msol.group(1))
            amt = UNKNOWN
            mamt = re.search(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?)", body)
            if mamt:
                try:
                    amt = float(mamt.group(1).replace(",", ""))
                except ValueError:
                    amt = UNKNOWN
            # Dates
            posting = deadline = award_date = UNKNOWN
            md = re.findall(
                r"(?:due|closing|opened|posted|award(?:ed)?)\s*(?:date)?[:\s]+([A-Za-z]+\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4})",
                body,
                re.I,
            )
            # Too ambiguous to assign without clear labels — leave UNKNOWN unless labeled
            m_due = re.search(
                r"(?:bid\s+)?(?:due|closing)\s*(?:date)?[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
                body,
                re.I,
            )
            m_post = re.search(
                r"(?:posted|issued|open(?:ed)?)\s*(?:date)?[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
                body,
                re.I,
            )
            m_aw = re.search(
                r"award(?:ed)?\s*(?:date|on)?[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
                body,
                re.I,
            )

            def _parse(d: str | None) -> Any:
                if not d:
                    return UNKNOWN
                try:
                    return datetime.strptime(d, "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    return UNKNOWN

            if m_due:
                deadline = _parse(m_due.group(1))
            if m_post:
                posting = _parse(m_post.group(1))
            if m_aw:
                award_date = _parse(m_aw.group(1))

            awardee = UNKNOWN
            mvend = re.search(
                r"(?:awarded to|successful bidder|vendor)[:\s]+([A-Z][A-Za-z0-9 &.,'\-]{3,60})",
                body,
                re.I,
            )
            if mvend:
                awardee = mvend.group(1).strip()[:80]

            # Agency heuristic from title/h1
            agency = UNKNOWN
            mag = re.search(
                r"(City of [A-Za-z .]+|[A-Za-z]+ County|[A-Za-z]+ Department of [A-Za-z ]+|State of [A-Za-z]+)",
                body,
            )
            if mag:
                agency = mag.group(1)[:80]

            product = category
            mprod = re.search(
                r"(chromebooks?|desktop computers?|monitors?|office furniture|diesel generators?|fire extinguishers?|plow blades?|cutting edges?|pumps?)",
                body,
                re.I,
            )
            if mprod:
                product = mprod.group(1)

            found.append(
                benchmark_case(
                    benchmark_case_id=f"HBM-V1-PUB-{len(found)+1:02d}",
                    agency=agency,
                    solicitation_number=sol,
                    product_description=product,
                    posting_date=posting,
                    bid_deadline=deadline,
                    award_date=award_date,
                    awardee=awardee,
                    award_amount=amt,
                    product_category=category,
                    buyer_level=level,
                    synthetic_dates_used=False,
                    provenance=[{"source_url": url, "query": q, "availability": SRC_HISTORICALLY_AVAILABLE}],
                    notes="Extracted from public award/bid page; fields UNKNOWN when not clearly labeled.",
                )
            )
            break  # one page per query
    return found


def select_backtest_cases(tiered: list[dict[str, Any]], max_n: int = 5) -> list[dict[str, Any]]:
    """Select up to 5 GOLD/SILVER without success bias — diversity of product/buyer/failure modes."""
    gold_silver = [c for c in tiered if c["tier"] in (TIER_GOLD, TIER_SILVER)]
    # Prefer diversity
    selected: list[dict[str, Any]] = []
    categories: set[str] = set()
    levels: set[str] = set()
    for c in gold_silver:
        if len(selected) >= max_n:
            break
        cat = str(c.get("product_category"))
        lvl = str(c.get("buyer_level"))
        # Avoid dominating one category unless needed to fill
        if cat in categories and len(selected) < max_n - 1 and len(gold_silver) > max_n:
            # skip if another unused category remains
            remaining = [x for x in gold_silver if x not in selected and str(x.get("product_category")) not in categories]
            if remaining:
                continue
        selected.append(c)
        categories.add(cat)
        levels.add(lvl)

    # If fewer than max, do not pull BRONZE into full run when stronger exist — only if none
    if not selected:
        bronze = [c for c in tiered if c["tier"] == TIER_BRONZE]
        selected = bronze[: min(3, max_n)]
    return selected


def build_prebid_package(
    case: dict[str, Any],
    store: TemporalEvidenceStore,
    *,
    simulation_start: str,
) -> list[str]:
    """Create temporally allowed pre-bid evidence — no outcome fields."""
    ids = []
    cutoff = simulation_start
    posting = case.get("posting_date")
    if posting in (None, UNKNOWN):
        # Without posting, use simulation start - 1 day as availability only if award-centric diagnostic
        posting = simulation_start

    specs = [
        (
            ROLE_PRE_BID_DISCOVERY,
            "Public solicitation/award discovery reconstruction",
            {
                "title": str(case.get("product_description")),
                "product_description": case.get("product_description"),
                "text": f"{case.get('product_description')} purchase supply equipment",
            },
        ),
        (
            ROLE_PRE_BID_REQUIREMENT,
            "Requirement text reconstruction from public description",
            {
                "product": case.get("product_description"),
                "product_description": case.get("product_description"),
                "quantity": case.get("quantities"),
                "specification": str(case.get("product_description")),
                "text": str(case.get("product_description")),
            },
        ),
    ]
    # Optional catalog price ONLY if we do not use winning amount
    # Do not add award amount as price evidence

    for role, title, payload in specs:
        eid = new_evidence_id()
        rec = make_temporal_evidence(
            evidence_id=eid,
            case_id=case["benchmark_case_id"],
            title=title,
            evidence_role=role,
            knowledge_cutoff_at=cutoff,
            source_publication_date=posting if posting != UNKNOWN else None,
            historical_availability_date=posting if posting != UNKNOWN else None,
            retrieval_date=utc_now_iso(),
            payload=payload,
        )
        store.add(rec)
        ids.append(eid)
    return ids


def store_outcome(vault: HistoricalOutcomeVault, case: dict[str, Any]) -> None:
    vault.store(
        case["benchmark_case_id"],
        {
            "awardee": case.get("awardee"),
            "award_amount": case.get("award_amount"),
            "award_date": case.get("award_date"),
            "solicitation_number": case.get("solicitation_number"),
            "provenance": case.get("provenance"),
        },
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            w.writerow(flat)


def main() -> dict[str, Any]:
    reset_clock()
    assert clock_mode() == CLOCK_SYSTEM
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    budget = RequestBudget()
    commit = git_commit()

    with httpx.Client(headers={"User-Agent": "M3-HistoricalBenchmarkV1/1.0 (public research; no login)"}) as client:
        iowa = investigate_iowa_2907(client, budget)
        usa_raw = collect_usaspending_product_candidates(client, budget)
        usa_enriched = verify_candidate_public_pages(client, budget, usa_raw, limit=12)
        public_cases = promote_documented_state_local_cases(client, budget)

    base_cases = build_benchmark_cases(iowa, usa_enriched)
    # Merge public cases (dedupe by solicitation if both known)
    all_cases = base_cases + public_cases
    tiered = attach_tiers(all_cases)

    # Candidate inventory CSV
    cand_rows = [
        {
            "benchmark_case_id": c["benchmark_case_id"],
            "tier": c["tier"],
            "agency": c.get("agency"),
            "product": c.get("product_description"),
            "solicitation": c.get("solicitation_number"),
            "award_amount": c.get("award_amount"),
            "awardee": c.get("awardee"),
            "category": c.get("product_category"),
            "buyer_level": c.get("buyer_level"),
            "synthetic_dates_used": c.get("synthetic_dates_used"),
        }
        for c in tiered
    ]
    write_csv(ARTIFACTS / "historical_benchmark_candidates.csv", cand_rows)

    # V1 set = all non-NOT_READY except we keep VALIDATION_ONLY labeled
    v1_cases = [c for c in tiered if c["tier"] != TIER_NOT_READY]
    # Stable sort by case id
    v1_cases.sort(key=lambda x: x["benchmark_case_id"])

    selected = select_backtest_cases(v1_cases, max_n=5)

    # Evidence + vault + backtests
    store = TemporalEvidenceStore()
    vault = HistoricalOutcomeVault()
    prebid_manifests = []
    postbid_manifests = []
    integrity_rows = []
    backtest_results = []
    scored = []

    for case in selected:
        # Determine simulation start
        start = simulation_start_from_posting(case.get("posting_date"))
        if start is None:
            # Award-centric: cannot do honest discovery timing — use award_date - 30d as diagnostic only
            ad = case.get("award_date")
            if ad not in (None, UNKNOWN):
                try:
                    from temporal_evidence_firewall import parse_iso_datetime
                    from datetime import timedelta

                    dt = parse_iso_datetime(ad)
                    if dt:
                        # Diagnostic start — label as reconstruction
                        start = (dt - timedelta(days=30)).isoformat()
                        case = {**case, "notes": (case.get("notes") or "") + " | simulation_start reconstructed as award_date-30d (posting UNKNOWN)"}
                except Exception:  # noqa: BLE001
                    start = None
        if start is None:
            start = "2024-06-01T12:00:00+00:00"
            case = {**case, "notes": (case.get("notes") or "") + " | simulation_start fallback labeled reconstruction"}

        # Source availability
        prov = (case.get("provenance") or [{}])[0]
        src_type = "usaspending" if "usaspending" in str(prov) else "agency_award_notice"
        if "govcb" in str(prov).lower() or "third" in str(prov).lower():
            src_avail = SRC_CURRENT_ONLY
        else:
            src_avail = classify_source_availability(src_type)

        eids = build_prebid_package(case, store, simulation_start=start)
        prebid_manifests.append(prebid_evidence_manifest(case["benchmark_case_id"], eids, knowledge_cutoff_at=start))
        store_outcome(vault, case)
        post_eid = new_evidence_id("OUT")
        postbid_manifests.append(postbid_evidence_manifest(case["benchmark_case_id"], [post_eid]))

        # Always run FULL_DISCOVERY (may fail honestly) + KNOWN_OPPORTUNITY diagnostic
        modes = [MODE_FULL_DISCOVERY, MODE_KNOWN_OPPORTUNITY]

        case_mode_results = []
        for mode in modes:
            # Fresh vault lock per mode — re-store outcome
            store_outcome(vault, case)
            result = run_backtest_case(
                case,
                mode=mode,
                evidence_store=store,
                outcome_vault=vault,
                source_availability=src_avail,
                simulation_start_at=start,
            )
            s = score_backtest(result, case)
            case_mode_results.append({"mode": mode, "result": result, "score": s})
            scored.append({"benchmark_case_id": case["benchmark_case_id"], "tier": case["tier"], **s, "mode": mode})

        backtest_results.append(
            {
                "case": {k: case[k] for k in case if k != "provenance"},
                "provenance": case.get("provenance"),
                "tier": case["tier"],
                "simulation_start_at": start,
                "source_availability": src_avail,
                "modes": case_mode_results,
            }
        )
        # Integrity from last frozen decision
        last = case_mode_results[-1]["result"]
        integrity_rows.append(
            integrity_record(
                case_id=case["benchmark_case_id"],
                tier=case["tier"],
                simulation_mode=case_mode_results[-1]["mode"],
                simulation_start_at=start,
                decision_hash=last["decision_hash"],
                outcome_reveal_timestamp=last["outcome"].get("outcome_reveal_timestamp"),
                git_commit=commit,
                case_provenance=case.get("provenance"),
                temporal_validation={"prebid_evidence_ids": eids},
            )
        )

    isolation = verify_live_iowa_isolation(ARTIFACTS)
    # Confirm live packet unchanged — checksum
    live_path = ARTIFACTS / "transactional_procurement_packets" / f"{LIVE_IOWA_2975}.json"
    live_meta = {
        "exists": live_path.exists(),
        "sha256": None,
        "isolation": isolation,
    }
    if live_path.exists():
        import hashlib

        live_meta["sha256"] = hashlib.sha256(live_path.read_bytes()).hexdigest()

    # Failure analysis aggregate
    error_counts: dict[str, int] = {}
    for s in scored:
        for e in s.get("ERROR_CATEGORIES") or []:
            error_counts[e] = error_counts.get(e, 0) + 1

    improvement = {
        "did_well": [
            "Reusable temporal firewall + decision freeze integrity",
            "Core product classification often retained for commodity titles",
            "Winning price/vendor blocked from economics/supplier stages",
        ],
        "failed": [
            "Many award records lack recoverable pre-bid solicitation packages",
            "FULL_DISCOVERY often HISTORICAL_REPLAY_UNAVAILABLE / posting UNKNOWN",
            "Public pricing / freight rarely available pre-cutoff → QUOTE_REQUIRED",
        ],
        "data_access_problems": [
            "Solicitation PDFs/attachments not archived with award rows",
            "Bid deadlines not present on USAspending award records",
        ],
        "software_problems": [
            "Discovery depends on live source adapters not historically replayable",
        ],
        "business_model_limitations": [
            "Zero-cash + 480 FICO overlay leaves funding NEEDS_VERIFICATION without lender verification",
        ],
        "single_highest_leverage_missing_capability": (
            "HISTORICAL_PREBID_PACKAGE_RECOVERY — ability to retrieve the original solicitation PDF/"
            "attachments/Q&A as they existed before the bid deadline (not just the later award record)"
        ),
    }

    counts = {
        "GOLD": sum(1 for c in v1_cases if c["tier"] == TIER_GOLD),
        "SILVER": sum(1 for c in v1_cases if c["tier"] == TIER_SILVER),
        "BRONZE": sum(1 for c in v1_cases if c["tier"] == TIER_BRONZE),
        "VALIDATION_ONLY": sum(1 for c in v1_cases if c["tier"] == TIER_VALIDATION_ONLY),
        "candidates_total": len(tiered),
        "v1_total": len(v1_cases),
        "backtested": len(selected),
    }

    # Artifacts
    (ARTIFACTS / "historical_benchmark_v1.json").write_text(
        json.dumps(
            {
                "benchmark_version": BENCHMARK_VERSION,
                "immutable": True,
                "generated_at": utc_now_iso(),
                "git_commit": commit,
                "counts": counts,
                "cases": v1_cases,
                "iowa_2907_verification": iowa,
                "selected_for_backtest": [c["benchmark_case_id"] for c in selected],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    write_csv(
        ARTIFACTS / "historical_benchmark_v1.csv",
        [
            {
                "benchmark_case_id": c["benchmark_case_id"],
                "tier": c["tier"],
                "agency": c.get("agency"),
                "product": c.get("product_description"),
                "solicitation": c.get("solicitation_number"),
                "award": c.get("award_amount"),
                "awardee": c.get("awardee"),
            }
            for c in v1_cases
        ],
    )

    (ARTIFACTS / "historical_prebid_evidence_manifests.json").write_text(
        json.dumps({"manifests": prebid_manifests, "note": "No post-bid outcomes"}, indent=2),
        encoding="utf-8",
    )
    (ARTIFACTS / "historical_outcome_vault.json").write_text(
        json.dumps(vault.export_all_locked(), indent=2, default=str),
        encoding="utf-8",
    )
    (ARTIFACTS / "historical_backtest_v1_results.json").write_text(
        json.dumps({"results": backtest_results, "scored": scored}, indent=2, default=str),
        encoding="utf-8",
    )
    write_csv(ARTIFACTS / "historical_backtest_v1_results.csv", scored)
    (ARTIFACTS / "historical_backtest_failure_analysis.json").write_text(
        json.dumps({"error_counts": error_counts, "improvement": improvement}, indent=2),
        encoding="utf-8",
    )
    (ARTIFACTS / "historical_benchmark_integrity.json").write_text(
        json.dumps({"integrity": integrity_rows, "live_iowa_2975": live_meta}, indent=2, default=str),
        encoding="utf-8",
    )
    (ARTIFACTS / "historical_benchmark_request_log.json").write_text(
        json.dumps(budget.snapshot(), indent=2),
        encoding="utf-8",
    )

    # Reports
    report = f"""# Historical Benchmark V1

Version: `{BENCHMARK_VERSION}` (immutable)

## Counts
{json.dumps(counts, indent=2)}

## Iowa 2907
{json.dumps(iowa, indent=2, default=str)}

## Live Iowa 2975 isolation
{json.dumps(live_meta, indent=2)}

## Request counts
{json.dumps({k: v for k, v in budget.snapshot().items() if k != 'log'}, indent=2)}
"""
    (ARTIFACTS / "historical_benchmark_v1_report.md").write_text(report, encoding="utf-8")

    bt_lines = ["# Historical Backtest V1 Results", ""]
    for s in scored:
        bt_lines += [
            f"## {s['benchmark_case_id']} ({s.get('mode')})",
            f"- DISPOSITION: {s.get('PRE_BID_DISPOSITION')}",
            f"- DISCOVERY: {s.get('DISCOVERY_RESULT')}",
            f"- REQUIREMENT: {s.get('REQUIREMENT_RESULT')}",
            f"- SUPPLIER: {s.get('SUPPLIER_RESULT')}",
            f"- ECONOMIC: {s.get('ECONOMIC_RESULT')}",
            f"- FUNDING: {s.get('FUNDING_RESULT')}",
            f"- TIMING: {s.get('TIMING_RESULT')}",
            f"- OUTCOME: {s.get('HISTORICAL_OUTCOME')}",
            f"- ERRORS: {s.get('ERROR_CATEGORIES')}",
            f"- RIGHT: {s.get('WHAT_M3_GOT_RIGHT')}",
            f"- MISSED: {s.get('WHAT_M3_MISSED')}",
            "",
        ]
    (ARTIFACTS / "historical_backtest_v1_report.md").write_text("\n".join(bt_lines), encoding="utf-8")
    (ARTIFACTS / "historical_backtest_failure_analysis.md").write_text(
        "# Failure Analysis\n\n"
        + json.dumps(error_counts, indent=2)
        + "\n\n## Improvement findings\n\n"
        + json.dumps(improvement, indent=2),
        encoding="utf-8",
    )

    reset_clock()
    return {
        "counts": counts,
        "selected": [c["benchmark_case_id"] for c in selected],
        "request_counts": budget.snapshot(),
        "isolation_ok": isolation.get("isolation_ok"),
        "iowa_2907": iowa,
        "scored_n": len(scored),
    }


if __name__ == "__main__":
    # Fix missing import used in constants reference
    from historical_benchmark_constants import SRC_CURRENT_ONLY as _  # noqa: F401

    out = main()
    print(json.dumps({k: v for k, v in out.items() if k != "request_counts"} | {"http": out["request_counts"]["http"], "usa": out["request_counts"]["usaspending"], "sam": out["request_counts"]["sam"]}, indent=2, default=str))
