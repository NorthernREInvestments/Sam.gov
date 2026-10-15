"""One-pass end-to-end opportunity funnel orchestrator.

Reuses existing CFE/BidNet stage implementations. Does not rebuild clients,
extractors, or pricing engines.

Flow per opportunity:
  QUALIFY → ACCESS_DOCS → EXTRACT → SOURCE → PRICE → ECONOMICS → FINANCE → BID_PREP

On software error: classify, checkpoint, return DEFERRED (caller puts at back of line).
On business rejection: terminal exclude with minimal fingerprint.
On external dependency: checkpoint at exact stage; do not fake completion.
"""

from __future__ import annotations

import time
from typing import Any

from application_clock import now_utc

BUILD = "20261014-m3-opportunity-funnel-orchestrator-v1"

STAGE_QUALIFY = "QUALIFY"
STAGE_ACCESS_DOCS = "ACCESS_DOCS"
STAGE_EXTRACT = "EXTRACT"
STAGE_SOURCE = "SOURCE"
STAGE_PRICE = "PRICE"
STAGE_ECONOMICS = "ECONOMICS"
STAGE_FINANCE = "FINANCE"
STAGE_BID_PREP = "BID_PREP"

OUTCOME_COMPLETE = "COMPLETE"  # reached furthest auto stage
OUTCOME_BUSINESS_REJECT = "BUSINESS_REJECT"
OUTCOME_EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
OUTCOME_SOFTWARE_ERROR = "SOFTWARE_ERROR"
OUTCOME_EXPIRED = "EXPIRED"

_SOFTWARE_BLOCKERS = {
    "DETAIL_FETCH_EMPTY",
    "ABSTRACT_FETCH_FAILED_OR_WAF",
    "ABSTRACT_PARSE_UNAVAILABLE",
    "WAF_OR_CHALLENGE_PAGE",
    "PER_OPPORTUNITY_TIMEOUT",
    "PER_OPPORTUNITY_TIMEOUT_45S",
    "FETCH_FAILED",
    "PROCESS_EXCEPTION",
    "BIDNET_AUTH_REQUIRED",
    "MISSING_AUTHORITATIVE_URL",
}
_EXTERNAL_BLOCKERS = {
    "AWAITING_PURCHASING_GROUP_REGISTRATION",
    "PURCHASING_GROUP_REGISTRATION_REQUIRED",
    "AWAITING_OWNER_ACTION",
    "AWAITING_OWNER_APPROVED_OUTREACH",
    "MISSING_ACQUISITION_AND_GOV_PRICE",
    "FINANCING_UNVERIFIED",
    "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS",
    "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED",
}
_BUSINESS_BLOCKERS = {
    "DEADLINE_PASSED_REVALIDATION",
    "INELIGIBLE_PRODUCT",
    "NEGATIVE_OR_INSUFFICIENT_MARGIN",
}


def _utc() -> str:
    return now_utc().isoformat()


def fast_qualify(item: dict[str, Any], *, l23_rec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stage 1 — metadata-only qualification. No browser."""
    t0 = time.time()
    l23_rec = l23_rec or {}
    oid = str(item.get("opportunity_id") or "")
    title = str(item.get("title") or l23_rec.get("title") or "")
    days = item.get("days_left")
    if days is None:
        days = l23_rec.get("days_left")
    try:
        if days is not None and float(days) < 0:
            return {
                "ok": False,
                "stage": STAGE_QUALIFY,
                "outcome": OUTCOME_EXPIRED,
                "terminal_blocker": "DEADLINE_PASSED_REVALIDATION",
                "elapsed_s": round(time.time() - t0, 3),
                "opportunity_id": oid,
                "title": title[:160],
            }
    except (TypeError, ValueError):
        pass
    # Lightweight service heuristic from title only
    blob = title.lower()
    if blob and not any(
        k in blob
        for k in (
            "supply",
            "supplies",
            "equipment",
            "material",
            "product",
            "goods",
            "purchase",
            "commodit",
            "paper",
            "toner",
            "tool",
            "mro",
            "parts",
            "vehicle",
            "furniture",
            "chemical",
            "filter",
            "lamp",
            "led",
            "glove",
            "ppe",
        )
    ) and any(
        k in blob
        for k in (
            "consulting",
            "professional services",
            "staffing",
            "janitorial service",
            "lawn care",
            "snow removal",
            "training only",
        )
    ):
        return {
            "ok": False,
            "stage": STAGE_QUALIFY,
            "outcome": OUTCOME_BUSINESS_REJECT,
            "terminal_blocker": "INELIGIBLE_PRODUCT",
            "elapsed_s": round(time.time() - t0, 3),
            "opportunity_id": oid,
            "title": title[:160],
            "note": "metadata_service_heuristic",
        }
    return {
        "ok": True,
        "stage": STAGE_QUALIFY,
        "outcome": OUTCOME_COMPLETE,
        "elapsed_s": round(time.time() - t0, 3),
        "opportunity_id": oid,
    }


def classify_process_one_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Map CFE process_one result onto funnel outcome + furthest stage."""
    blocker = str(row.get("terminal_blocker") or row.get("package_blocker") or "")
    err = str(row.get("process_bidnet_error") or row.get("process_error") or "")
    docs = int(row.get("documents_downloaded") or 0)
    lines = int(row.get("line_count") or 0)
    complete = bool(row.get("complete_spec"))
    priced = row.get("public_unit_price") not in (None, "")
    hist = row.get("historical_unit_price") not in (None, "")
    rfq_ok = str((row.get("rfq") or {}).get("status") or "") == "AWAITING_OWNER_APPROVED_OUTREACH"
    econ = str(row.get("econ_state") or "")

    if err or blocker.startswith("PROCESS_EXCEPTION"):
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": (err.split(":")[0] if err else blocker)[:80],
        }
    if "TIMEOUT" in blocker or row.get("bounded_timed_out"):
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS if docs == 0 else STAGE_EXTRACT,
            "defer": True,
            "software_defect_key": "TIMEOUT",
        }
    if any(blocker.startswith(b) or blocker == b for b in _SOFTWARE_BLOCKERS):
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": blocker[:80] or "SOFTWARE_BLOCKER",
        }
    if blocker in _BUSINESS_BLOCKERS or "EXPIRED" in blocker:
        return {
            "outcome": OUTCOME_EXPIRED if "EXPIRED" in blocker or "DEADLINE" in blocker else OUTCOME_BUSINESS_REJECT,
            "stage_reached": STAGE_QUALIFY,
            "defer": False,
            "software_defect_key": None,
        }
    if blocker.startswith("PAID_ENTITLEMENT") or blocker in _EXTERNAL_BLOCKERS:
        stage = STAGE_ACCESS_DOCS
        if "OWNER" in blocker or "RFQ" in blocker or "OUTREACH" in blocker:
            stage = STAGE_BID_PREP if complete else STAGE_SOURCE
        elif "ACQUISITION" in blocker or "PRICE" in blocker:
            stage = STAGE_PRICE
        elif "FINANC" in blocker:
            stage = STAGE_FINANCE
        elif "PURCHASING" in blocker:
            stage = STAGE_ACCESS_DOCS
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": stage,
            "defer": True,
            "software_defect_key": None,
            "shared_access_key": (
                f"pg:{(row.get('purchasing_group_id') or '')}"
                if "PURCHASING" in blocker
                else (f"paid:{blocker}" if blocker.startswith("PAID_") else None)
            ),
        }

    # Progressive stage from evidence
    stage = STAGE_ACCESS_DOCS
    if docs > 0:
        stage = STAGE_EXTRACT
    if complete and lines > 0:
        stage = STAGE_SOURCE
    if priced or hist:
        stage = STAGE_PRICE
    if econ and econ not in {"MARGIN_UNVERIFIED", None, ""}:
        stage = STAGE_ECONOMICS
    if rfq_ok:
        stage = STAGE_BID_PREP
    if row.get("financeable"):
        stage = STAGE_FINANCE

    if complete and docs > 0 and lines > 0:
        return {
            "outcome": OUTCOME_COMPLETE,
            "stage_reached": stage,
            "defer": False,
            "software_defect_key": None,
        }
    if docs > 0 or complete:
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY if blocker else OUTCOME_COMPLETE,
            "stage_reached": stage,
            "defer": bool(blocker),
            "software_defect_key": None,
        }
    if blocker:
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": None,
        }
    return {
        "outcome": OUTCOME_SOFTWARE_ERROR,
        "stage_reached": STAGE_ACCESS_DOCS,
        "defer": True,
        "software_defect_key": "EMPTY_RESULT",
    }


def run_one_opportunity(
    item: dict[str, Any],
    *,
    client: Any,
    evidence_index: dict[str, Any],
    auth_status: dict[str, Any] | None,
    l23_rec: dict[str, Any] | None,
    auth_client: Any | None,
) -> dict[str, Any]:
    """Execute one continuous funnel journey for a single opportunity.

    Pricing runs inside process_one immediately after extraction — never waits
    for a batch slice boundary.
    """
    from buyer_intelligence.commercial_funnel_execution_v1 import process_one

    t0 = time.time()
    oid = str(item.get("opportunity_id") or "")
    stages_run: list[str] = []

    q = fast_qualify(item, l23_rec=l23_rec)
    stages_run.append(STAGE_QUALIFY)
    if not q.get("ok"):
        return {
            "opportunity_id": oid,
            "build": BUILD,
            "AUTO_SEND": False,
            "funnel_outcome": q.get("outcome"),
            "stage_reached": STAGE_QUALIFY,
            "stages_run": stages_run,
            "defer": False,
            "terminal_blocker": q.get("terminal_blocker"),
            "title": q.get("title") or item.get("title"),
            "elapsed_s": round(time.time() - t0, 3),
            "finished_at": _utc(),
            "one_pass": True,
        }

    try:
        row = process_one(
            item,
            client=client,
            evidence_index=evidence_index,
            auth_status=auth_status,
            l23_rec=l23_rec or {},
            auth_client=auth_client,
        )
    except Exception as exc:
        return {
            "opportunity_id": oid,
            "build": BUILD,
            "AUTO_SEND": False,
            "funnel_outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS,
            "stages_run": stages_run + [STAGE_ACCESS_DOCS],
            "defer": True,
            "software_defect_key": type(exc).__name__,
            "terminal_blocker": f"PROCESS_EXCEPTION:{type(exc).__name__}",
            "process_error": str(exc)[:200],
            "elapsed_s": round(time.time() - t0, 3),
            "finished_at": _utc(),
            "one_pass": True,
        }

    # process_one already ran ACCESS→EXTRACT→SOURCE→PRICE→ECONOMICS→RFQ continuously
    stages_run.extend(
        [
            STAGE_ACCESS_DOCS,
            STAGE_EXTRACT,
            STAGE_SOURCE,
            STAGE_PRICE,
            STAGE_ECONOMICS,
            STAGE_FINANCE,
            STAGE_BID_PREP,
        ]
    )
    classification = classify_process_one_outcome(row)
    row["funnel_outcome"] = classification["outcome"]
    row["stage_reached"] = classification["stage_reached"]
    row["stages_run"] = stages_run
    row["defer"] = bool(classification.get("defer"))
    row["software_defect_key"] = classification.get("software_defect_key")
    row["shared_access_key"] = classification.get("shared_access_key")
    row["one_pass"] = True
    row["orchestrator_build"] = BUILD
    row["elapsed_s"] = round(time.time() - t0, 3)
    row["finished_at"] = _utc()
    return row
