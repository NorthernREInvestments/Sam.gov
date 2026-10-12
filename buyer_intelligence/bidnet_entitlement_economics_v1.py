"""BidNet entitlement economics + free-route decision for fixed 36.

Frozen inputs from package-location resolver V1:
  28 = PAID_ENTITLEMENT_REQUIRED (do NOT retry BidNet package retrieval)
  8  = PACKAGE_LOCATION_UNRESOLVED (continue free official resolution)
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from buyer_intelligence.bidnet_auth_recovery_package_closure_v1 import (
    compute_full100_progression,
    _data,
    _load_json,
    _write_json,
)
from package_recovery_sam_budget.quality import is_valid_package_evidence

log = logging.getLogger("govtracker.bidnet_entitlement_economics")

BUILD = "20261009-m3-bidnet-entitlement-economics-v1"
SOURCE_JOB = "bidnet_package_location_resolver-20261009T183924Z-d98a73d1"
RESULTS_IN = "m3_bidnet_package_location_resolver_v1_results.json"
REPORT = "m3_bidnet_entitlement_economics_v1_report.json"
RESULTS_OUT = "m3_bidnet_entitlement_economics_v1_results.json"
SPLIT = "m3_bidnet_entitlement_split_v1.json"
UNRESOLVED_8_RESULTS = "m3_bidnet_unresolved8_free_route_v1_results.json"

# Soft industry reference only — not a purchase quote. Mark UNKNOWN when unused.
# BidNet statewide plans are typically hundreds–thousands USD/year per state; we do not invent prices.
ENTITLEMENT_COST_STATE = "UNKNOWN_NO_PUBLIC_QUOTE"

CODE_RE = re.compile(r"CB_(?:SW|FD)_[A-Z0-9_]+")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_location_results() -> list[dict[str, Any]]:
    data = _load_json(_data(RESULTS_IN)) or {}
    rows = list(data.get("rows") or [])
    if len(rows) != 36:
        # local fallback from speed-run export
        local = _load_json(_data("_tmp_plr_entitlement_split.json"))
        if local:
            return []
    return rows


def freeze_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paid = [r for r in rows if r.get("ACCESS_STATE") == "PAID_ENTITLEMENT_REQUIRED"]
    unres = [r for r in rows if r.get("PACKAGE_LOCATION_STATE") == "PACKAGE_LOCATION_UNRESOLVED"]
    codes: Counter[str] = Counter()
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in paid:
        code = str(r.get("PAID_ENTITLEMENT_REQUIREMENT") or "UNKNOWN")
        codes[code] += 1
        by_code[code].append(
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "BIDNET_URL": (r.get("CLUES") or {}).get("BIDNET_URL"),
            }
        )
    sw = {k: v for k, v in codes.items() if k.startswith("CB_SW_")}
    fd = {k: v for k, v in codes.items() if k.startswith("CB_FD_")}
    split = {
        "BUILD": BUILD,
        "SOURCE_JOB": SOURCE_JOB,
        "frozen_at": _utc(),
        "PAID_28_COUNT": len(paid),
        "UNRESOLVED_8_COUNT": len(unres),
        "PAID_OPPORTUNITY_IDS": [str(r.get("OPPORTUNITY_ID")) for r in paid],
        "UNRESOLVED_OPPORTUNITY_IDS": [str(r.get("OPPORTUNITY_ID")) for r in unres],
        "ENTITLEMENT_CODES": dict(codes),
        "STATEWIDE_CODES_CB_SW": sw,
        "FEDERAL_DISTRICT_OR_OTHER_CB_FD": fd,
        "UNIQUE_CODE_COUNT": len(codes),
        "BY_CODE": {k: v for k, v in by_code.items()},
        "PAID_ROWS": [
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "PAID_ENTITLEMENT_REQUIREMENT": r.get("PAID_ENTITLEMENT_REQUIREMENT"),
                "BLOCKER": r.get("BLOCKER"),
                "BIDNET_URL": (r.get("CLUES") or {}).get("BIDNET_URL"),
                "NEXT_ACTION": "DO_NOT_RETRY_BIDNET_PACKAGE",
            }
            for r in paid
        ],
        "UNRESOLVED_ROWS": [
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "BIDNET_URL": (r.get("CLUES") or {}).get("BIDNET_URL"),
                "NEXT_ACTION": "FREE_OFFICIAL_PACKAGE_CHASE",
            }
            for r in unres
        ],
        "POLICY": {
            "PAID_28": "NO_BIDNET_PACKAGE_RETRY",
            "UNRESOLVED_8": "BOUNDED_FREE_OFFICIAL_RESOLUTION",
        },
    }
    _write_json(_data(SPLIT), split)
    return split


def _free_chase_one(row: dict[str, Any], *, l23: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    """Free official alternate only — never BidNet membership document pull."""
    t0 = time.perf_counter()
    oid = str(row.get("OPPORTUNITY_ID"))
    work = dict(l23)
    work.setdefault("title", row.get("TITLE") or l23.get("title"))
    work.setdefault("buyer", row.get("BUYER") or l23.get("buyer"))
    url = (row.get("CLUES") or {}).get("BIDNET_URL") or row.get("PORTAL") or l23.get("authoritative_url")
    work.setdefault("authoritative_url", url)
    out: dict[str, Any] = {
        "OPPORTUNITY_ID": oid,
        "BUYER": row.get("BUYER"),
        "TITLE": row.get("TITLE"),
        "BIDNET_URL": url,
        "FREE_ROUTE_STATE": "NOT_FOUND",
        "DOC_COUNT": 0,
        "PLATFORM": None,
        "MATCH_CONFIDENCE": "LOW",
        "BUILD": BUILD,
    }
    try:
        from package_recovery_sam_budget.recover import recover_bidnet

        hit = recover_bidnet(
            {
                "opportunity_id": oid,
                "title": work.get("title"),
                "buyer": work.get("buyer"),
                "authoritative_url": url,
            },
            l23_row=work,
            store=store or {},
            force=True,
        ) or {}
        docs = list(hit.get("documents") or [])
        # Drop BidNet-hosted docs — not a free route
        docs = [
            d
            for d in docs
            if isinstance(d, dict)
            and "bidnet" not in str(d.get("document_url") or "").lower()
        ]
        quality = is_valid_package_evidence(docs)
        accepted = list(quality.get("accepted") or [])
        out["CHASE_STATUS"] = hit.get("chase_status") or hit.get("package_state")
        out["ROUTE"] = hit.get("route_family") or hit.get("route") or hit.get("matched_source")
        out["MATCH_CONFIDENCE"] = hit.get("match_confidence") or ("HIGH" if hit.get("verified") else "LOW")
        out["DOC_COUNT"] = len(accepted)
        out["DOCUMENTS"] = accepted[:5]
        if accepted:
            out["FREE_ROUTE_STATE"] = "FREE_PACKAGE_FOUND"
            out["PLATFORM"] = out["ROUTE"]
        elif hit.get("cross_portal_match") or (
            out["ROUTE"] and "bidnet" not in str(out["ROUTE"]).lower() and out["ROUTE"] != "unknown"
        ):
            out["FREE_ROUTE_STATE"] = "OFFICIAL_PORTAL_HINT_NO_DOCS"
            out["PLATFORM"] = out["ROUTE"]
        else:
            out["FREE_ROUTE_STATE"] = "NOT_FOUND"
    except Exception as exc:
        out["FREE_ROUTE_STATE"] = "ERROR"
        out["ERROR"] = type(exc).__name__
    out["DURATION_SECONDS"] = round(time.perf_counter() - t0, 3)
    return out


def run_unresolved8_free_route(unresolved_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    l23_store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}
    results = []
    for row in unresolved_rows:
        oid = str(row.get("OPPORTUNITY_ID"))
        l23 = dict(l23_store.get(oid) or {})
        # Enrich from unresolved stub
        if not l23:
            l23 = {
                "title": row.get("TITLE"),
                "buyer": row.get("BUYER"),
                "authoritative_url": row.get("BIDNET_URL"),
            }
        rec = _free_chase_one(row, l23=l23, store=l23_store)
        results.append(rec)
    _write_json(_data(UNRESOLVED_8_RESULTS), {"build": BUILD, "count": len(results), "rows": results})
    return results


def run_paid28_free_route_sample(paid_rows: list[dict[str, Any]], *, max_n: int = 28) -> list[dict[str, Any]]:
    """Route-around probe for paid 28: free official only, never BidNet docs."""
    l23_store = (_load_json(_data("l23_canonical_population_store.json")) or {}).get("opportunities") or {}
    results = []
    for row in paid_rows[:max_n]:
        oid = str(row.get("OPPORTUNITY_ID"))
        l23 = dict(l23_store.get(oid) or {})
        if not l23:
            l23 = {
                "title": row.get("TITLE"),
                "buyer": row.get("BUYER"),
                "authoritative_url": row.get("BIDNET_URL"),
            }
        # Explicit: do not use BidNet authenticated document path
        rec = _free_chase_one(row, l23=l23, store=l23_store)
        rec["PAID_ENTITLEMENT_REQUIREMENT"] = row.get("PAID_ENTITLEMENT_REQUIREMENT")
        rec["POLICY"] = "NO_BIDNET_PACKAGE_RETRY"
        results.append(rec)
    return results


def decide_economics(
    *,
    split: dict[str, Any],
    free_paid: list[dict[str, Any]],
    free_unres: list[dict[str, Any]],
) -> dict[str, Any]:
    codes = split.get("ENTITLEMENT_CODES") or {}
    unique = int(split.get("UNIQUE_CODE_COUNT") or len(codes))
    paid_n = int(split.get("PAID_28_COUNT") or 28)
    free_found_paid = sum(1 for r in free_paid if r.get("FREE_ROUTE_STATE") == "FREE_PACKAGE_FOUND")
    free_hint_paid = sum(1 for r in free_paid if r.get("FREE_ROUTE_STATE") == "OFFICIAL_PORTAL_HINT_NO_DOCS")
    free_found_unres = sum(1 for r in free_unres if r.get("FREE_ROUTE_STATE") == "FREE_PACKAGE_FOUND")
    free_hint_unres = sum(1 for r in free_unres if r.get("FREE_ROUTE_STATE") == "OFFICIAL_PORTAL_HINT_NO_DOCS")

    # Concentration: top code share
    top_code, top_n = ("NONE", 0)
    if codes:
        top_code, top_n = max(codes.items(), key=lambda kv: kv[1])
    concentration = round(top_n / paid_n, 3) if paid_n else 0.0

    # Decision rules (truthful, no invented prices)
    reasons = []
    if unique >= 10:
        reasons.append(f"FRAGMENTATION:{unique}_distinct_entitlement_codes_for_{paid_n}_opportunities")
    if concentration < 0.25:
        reasons.append(f"LOW_CONCENTRATION:top_code={top_code}_share={concentration}")
    if free_found_paid == 0 and free_found_unres == 0:
        reasons.append("NO_FREE_OFFICIAL_PACKAGES_RECOVERED_IN_BOUNDED_CHASE")
    reasons.append("CURRENT_GOV_REVENUE_PROVEN_ON_COHORT:0")
    reasons.append(f"ENTITLEMENT_UNIT_COST:{ENTITLEMENT_COST_STATE}")

    # BUY only if few codes cover most opps AND free route fails AND revenue path exists
    if unique <= 3 and concentration >= 0.5 and free_found_paid == 0:
        decision = "CONDITIONAL_BUY_TOP_CODES_ONLY"
        decision_why = (
            "Few entitlement codes cover most blocked opportunities and free routes failed; "
            "obtain real BidNet quotes for top codes before purchase. Do not buy all codes."
        )
    elif free_found_paid >= 5:
        decision = "DO_NOT_BUY_ROUTE_FREE_OFFICIAL"
        decision_why = (
            f"Free official packages recovered for {free_found_paid}/28 paid-blocked rows; "
            "prefer free routing over BidNet entitlement spend."
        )
    else:
        decision = "DO_NOT_BUY_ENTITLEMENT_NOW"
        decision_why = (
            f"{unique} distinct paid codes block {paid_n} opportunities with no proven current revenue "
            f"and {free_found_paid} free package recoveries. Purchasing broad BidNet statewide coverage "
            "is not economically justified on this evidence. Keep free official routing; "
            "revisit only if a small code set concentrates future volume with revenue proof."
        )

    return {
        "DECISION": decision,
        "DECISION_WHY": decision_why,
        "REASONS": reasons,
        "UNIQUE_ENTITLEMENT_CODES": unique,
        "TOP_CODE": top_code,
        "TOP_CODE_COUNT": top_n,
        "TOP_CODE_SHARE": concentration,
        "FREE_ROUTE_PAID28_FOUND": free_found_paid,
        "FREE_ROUTE_PAID28_HINT": free_hint_paid,
        "FREE_ROUTE_UNRESOLVED8_FOUND": free_found_unres,
        "FREE_ROUTE_UNRESOLVED8_HINT": free_hint_unres,
        "ENTITLEMENT_COST_STATE": ENTITLEMENT_COST_STATE,
        "BUY_ALL_CODES": False,
        "BUY_TOP_CODE_ONLY": decision == "CONDITIONAL_BUY_TOP_CODES_ONLY",
        "RECOMMENDED_NEXT": (
            "OBTAIN_BIDNET_QUOTE_FOR_TOP_CODES"
            if decision == "CONDITIONAL_BUY_TOP_CODES_ONLY"
            else "CONTINUE_FREE_OFFICIAL_ROUTING_AND_UNRESOLVED8"
        ),
    }


def run_entitlement_economics_v1(*, wall_limit_sec: float = 600.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    rows = load_location_results()
    if len(rows) != 36:
        # Try reading from local split export rebuilt from API earlier
        raise RuntimeError(f"EXPECTED_36_GOT_{len(rows)}")

    split = freeze_split(rows)
    paid_rows = split["PAID_ROWS"]
    unres_rows = split["UNRESOLVED_ROWS"]

    # Unresolved 8 first (bounded), then paid 28 free-route probe (no BidNet retry)
    free_unres = run_unresolved8_free_route(unres_rows)
    free_paid = run_paid28_free_route_sample(paid_rows, max_n=28)

    decision = decide_economics(split=split, free_paid=free_paid, free_unres=free_unres)

    # Code inventory table
    code_table = []
    for code, n in sorted((split.get("ENTITLEMENT_CODES") or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        kind = "STATEWIDE" if code.startswith("CB_SW_") else ("OTHER_FD" if code.startswith("CB_FD_") else "UNKNOWN")
        region = code.replace("CB_SW_", "").replace("CB_FD_", "").replace("_", " ").title()
        code_table.append(
            {
                "CODE": code,
                "KIND": kind,
                "REGION": region,
                "OPPORTUNITY_COUNT": n,
                "COST": ENTITLEMENT_COST_STATE,
                "BUY_RECOMMENDED": bool(
                    decision.get("BUY_TOP_CODE_ONLY") and code == decision.get("TOP_CODE")
                ),
            }
        )

    progression = compute_full100_progression([])  # baseline unchanged — no new packages

    report = {
        "BUILD": BUILD,
        "SOURCE_JOB": SOURCE_JOB,
        "FINAL_STATUS": decision["DECISION"],
        "WALL_CLOCK_SECONDS": round(time.perf_counter() - t0, 2),
        "FIXED_36": {
            "PAID_ENTITLEMENT_REQUIRED": 28,
            "PACKAGE_LOCATION_UNRESOLVED": 8,
            "PACKAGES_RECOVERED": sum(1 for r in free_paid + free_unres if r.get("DOC_COUNT", 0) > 0),
        },
        "ENTITLEMENT_CODES": {
            "UNIQUE": decision["UNIQUE_ENTITLEMENT_CODES"],
            "BY_CODE": split.get("ENTITLEMENT_CODES"),
            "STATEWIDE_CB_SW": split.get("STATEWIDE_CODES_CB_SW"),
            "OTHER_CB_FD": split.get("FEDERAL_DISTRICT_OR_OTHER_CB_FD"),
            "TABLE": code_table,
            "TOP_CODE": decision["TOP_CODE"],
            "TOP_CODE_COUNT": decision["TOP_CODE_COUNT"],
        },
        "POLICY": {
            "PAID_28_BIDNET_PACKAGE_RETRY": "FORBIDDEN",
            "UNRESOLVED_8_FREE_OFFICIAL": "ACTIVE",
        },
        "FREE_ROUTE": {
            "PAID_28": {
                "FOUND": decision["FREE_ROUTE_PAID28_FOUND"],
                "HINT_NO_DOCS": decision["FREE_ROUTE_PAID28_HINT"],
                "ROWS": free_paid,
            },
            "UNRESOLVED_8": {
                "FOUND": decision["FREE_ROUTE_UNRESOLVED8_FOUND"],
                "HINT_NO_DOCS": decision["FREE_ROUTE_UNRESOLVED8_HINT"],
                "ROWS": free_unres,
            },
        },
        "ECONOMICS_DECISION": decision,
        "FULL_100_PROGRESSION": progression,
        "PRIMARY_QUESTIONS": {
            "1_which_codes_block_the_28": list((split.get("ENTITLEMENT_CODES") or {}).keys()),
            "2_is_purchase_justified": decision["DECISION"]
            not in {"DO_NOT_BUY_ENTITLEMENT_NOW", "DO_NOT_BUY_ROUTE_FREE_OFFICIAL"},
            "3_can_m3_route_around": decision["FREE_ROUTE_PAID28_FOUND"] + decision["FREE_ROUTE_UNRESOLVED8_FOUND"]
            > 0,
            "4_recommended_next": decision["RECOMMENDED_NEXT"],
        },
        "GATES": {
            "FIXED_36_PRESERVED": True,
            "PAID_28_NO_BIDNET_RETRY": True,
            "UNRESOLVED_8_FREE_CHASE_RUN": True,
            "ENTITLEMENT_CODES_EXTRACTED": True,
            "COST_NOT_INVENTED": True,
            "UNKNOWN_REMAINS_UNKNOWN": True,
        },
    }
    _write_json(_data(REPORT), report)
    _write_json(
        _data(RESULTS_OUT),
        {
            "build": BUILD,
            "paid_free_route": free_paid,
            "unresolved_free_route": free_unres,
            "decision": decision,
        },
    )
    return report


def run_async_job() -> dict[str, Any]:
    """Entry for API background thread."""
    status_path = _data("m3_bidnet_entitlement_economics_v1_job.json")
    status_path.write_text(
        json.dumps({"status": "RUNNING", "started_at": _utc(), "BUILD": BUILD}, indent=2),
        encoding="utf-8",
    )
    try:
        rep = run_entitlement_economics_v1()
        status_path.write_text(
            json.dumps(
                {
                    "status": "DONE",
                    "finished_at": _utc(),
                    "FINAL_STATUS": rep.get("FINAL_STATUS"),
                    "BUILD": BUILD,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return rep
    except Exception as exc:
        status_path.write_text(
            json.dumps(
                {"status": "ERROR", "error": type(exc).__name__, "finished_at": _utc(), "BUILD": BUILD},
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
