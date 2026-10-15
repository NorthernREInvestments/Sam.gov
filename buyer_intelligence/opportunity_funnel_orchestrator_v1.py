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

BUILD = "20261015-m3-opportunity-funnel-orchestrator-v1c"

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
    "COMMERCIAL_STAGE_BUDGET_DEFERRED",
    "SOURCE_PROVIDER_ERROR",
    "SOURCE_RESEARCH_INTERRUPTED",
    "PRICE_UNAVAILABLE_AFTER_SEARCH",
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


def _stage_from_evidence(row: dict[str, Any]) -> str:
    """Furthest stage supported by durable evidence (not merely attempted)."""
    docs = int(row.get("documents_downloaded") or 0)
    lines = int(row.get("line_count") or 0)
    complete = bool(row.get("complete_spec"))
    priced = row.get("public_unit_price") not in (None, "")
    hist = row.get("historical_unit_price") not in (None, "")
    rfq_ok = str((row.get("rfq") or {}).get("status") or "") == "AWAITING_OWNER_APPROVED_OUTREACH"
    econ = str(row.get("econ_state") or "")
    stage = STAGE_ACCESS_DOCS
    if docs > 0:
        stage = STAGE_EXTRACT
    if complete and lines > 0:
        stage = STAGE_SOURCE
    if priced or hist:
        stage = STAGE_PRICE
    if econ and econ not in {"MARGIN_UNVERIFIED", None, ""} and bool(
        row.get("economics_complete")
    ):
        stage = STAGE_ECONOMICS
    if rfq_ok:
        stage = STAGE_BID_PREP
    if row.get("financeable"):
        stage = STAGE_FINANCE
    return stage


def _last_completed_and_next(row: dict[str, Any], stage_reached: str) -> tuple[str, str]:
    """Derive last verified completed stage and next stage needing work."""
    explicit_last = str(row.get("last_completed_stage") or "").strip()
    explicit_next = str(row.get("next_stage") or "").strip()
    if explicit_last and explicit_next:
        return explicit_last, explicit_next
    order = [
        STAGE_ACCESS_DOCS,
        STAGE_EXTRACT,
        STAGE_SOURCE,
        STAGE_PRICE,
        STAGE_ECONOMICS,
        STAGE_FINANCE,
        STAGE_BID_PREP,
    ]
    docs = int(row.get("documents_downloaded") or 0)
    complete = bool(row.get("complete_spec"))
    priced = row.get("public_unit_price") not in (None, "")
    if complete and docs > 0:
        last = STAGE_EXTRACT
        if row.get("supplier_status") in {
            "SUPPLIER_IDENTIFIED",
            "SOURCE_UNAVAILABLE",
            "SOURCE_PROVIDER_ERROR",
        } and row.get("source_outcome") not in {
            None,
            "",
            "RESEARCH_INTERRUPTED",
            "SKIPPED_INSUFFICIENT_BUDGET",
        }:
            last = STAGE_SOURCE
        if priced:
            last = STAGE_PRICE
        next_stage = STAGE_PRICE if last == STAGE_EXTRACT else (
            STAGE_PRICE if last == STAGE_SOURCE and not priced else STAGE_ECONOMICS
        )
        if last == STAGE_PRICE:
            next_stage = STAGE_ECONOMICS
        return last, explicit_next or next_stage
    if docs > 0:
        return STAGE_ACCESS_DOCS, STAGE_EXTRACT
    if stage_reached in order:
        idx = order.index(stage_reached)
        last = order[max(0, idx - 1)] if idx > 0 else STAGE_QUALIFY
        return last, stage_reached
    return STAGE_QUALIFY, STAGE_ACCESS_DOCS


def classify_process_one_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Map CFE process_one result onto funnel outcome + furthest stage."""
    blocker = str(row.get("terminal_blocker") or row.get("package_blocker") or "")
    err = str(row.get("process_bidnet_error") or row.get("process_error") or "")
    docs = int(row.get("documents_downloaded") or 0)
    lines = int(row.get("line_count") or 0)
    complete = bool(row.get("complete_spec"))
    priced = row.get("public_unit_price") not in (None, "")

    if err or blocker.startswith("PROCESS_EXCEPTION"):
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS,
            "last_completed_stage": STAGE_QUALIFY,
            "next_stage": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": (err.split(":")[0] if err else blocker)[:80],
        }

    # Extract survived: timeout / commercial budget must not collapse to EXTRACT-only
    # SOFTWARE_ERROR when docs+lines are durable. Resume at commercial stage.
    extract_survived = docs > 0 and complete and lines > 0
    commercial_budget = blocker in {
        "COMMERCIAL_STAGE_BUDGET_DEFERRED",
        "SOURCE_RESEARCH_INTERRUPTED",
    } or bool(row.get("commercial_budget_deferred"))
    timeout_like = (
        "TIMEOUT" in blocker
        or bool(row.get("bounded_timed_out"))
        or bool(row.get("commercial_timed_out"))
    )
    if extract_survived and (timeout_like or commercial_budget):
        stage = _stage_from_evidence(row)
        if stage == STAGE_EXTRACT:
            stage = STAGE_SOURCE
        last, nxt = _last_completed_and_next(row, stage)
        if not priced:
            nxt = STAGE_PRICE if last in {STAGE_EXTRACT, STAGE_SOURCE} else nxt
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": stage if stage != STAGE_EXTRACT else STAGE_SOURCE,
            "last_completed_stage": last,
            "next_stage": nxt,
            "defer": True,
            "software_defect_key": None,
            "commercial_resume": True,
        }

    if "TIMEOUT" in blocker or row.get("bounded_timed_out"):
        stage = STAGE_ACCESS_DOCS if docs == 0 else STAGE_EXTRACT
        last, nxt = _last_completed_and_next(row, stage)
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": stage,
            "last_completed_stage": last,
            "next_stage": nxt,
            "defer": True,
            "software_defect_key": "TIMEOUT",
        }
    if any(blocker.startswith(b) or blocker == b for b in _SOFTWARE_BLOCKERS):
        return {
            "outcome": OUTCOME_SOFTWARE_ERROR,
            "stage_reached": STAGE_ACCESS_DOCS,
            "last_completed_stage": STAGE_QUALIFY,
            "next_stage": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": blocker[:80] or "SOFTWARE_BLOCKER",
        }
    if blocker in _BUSINESS_BLOCKERS or "EXPIRED" in blocker:
        return {
            "outcome": OUTCOME_EXPIRED if "EXPIRED" in blocker or "DEADLINE" in blocker else OUTCOME_BUSINESS_REJECT,
            "stage_reached": STAGE_QUALIFY,
            "last_completed_stage": STAGE_QUALIFY,
            "next_stage": STAGE_QUALIFY,
            "defer": False,
            "software_defect_key": None,
        }
    if blocker.startswith("PAID_ENTITLEMENT") or blocker in _EXTERNAL_BLOCKERS:
        stage = STAGE_ACCESS_DOCS
        if "OWNER" in blocker or "RFQ" in blocker or "OUTREACH" in blocker:
            stage = STAGE_BID_PREP if complete else STAGE_SOURCE
        elif "ACQUISITION" in blocker or "PRICE" in blocker or "BUDGET_DEFERRED" in blocker:
            stage = STAGE_PRICE if complete else STAGE_SOURCE
        elif "SOURCE_" in blocker:
            stage = STAGE_SOURCE
        elif "FINANC" in blocker:
            stage = STAGE_FINANCE
        elif "PURCHASING" in blocker:
            stage = STAGE_ACCESS_DOCS
        last, nxt = _last_completed_and_next(row, stage)
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": stage,
            "last_completed_stage": last,
            "next_stage": nxt,
            "defer": True,
            "software_defect_key": None,
            "shared_access_key": (
                f"pg:{(row.get('purchasing_group_id') or '')}"
                if "PURCHASING" in blocker
                else (f"paid:{blocker}" if blocker.startswith("PAID_") else None)
            ),
            "commercial_resume": bool(complete and docs > 0),
        }

    stage = _stage_from_evidence(row)
    last, nxt = _last_completed_and_next(row, stage)

    if complete and docs > 0 and lines > 0:
        # Missing acquisition price is not COMPLETE — remain deferred for commercial resume
        if not priced and blocker in {
            "MISSING_ACQUISITION_AND_GOV_PRICE",
            "PRICE_UNAVAILABLE_AFTER_SEARCH",
            "",
        }:
            return {
                "outcome": OUTCOME_EXTERNAL_DEPENDENCY if blocker else OUTCOME_COMPLETE,
                "stage_reached": stage,
                "last_completed_stage": last,
                "next_stage": STAGE_PRICE if not priced else nxt,
                "defer": bool(blocker) or not priced,
                "software_defect_key": None,
                "commercial_resume": not priced,
            }
        return {
            "outcome": OUTCOME_COMPLETE,
            "stage_reached": stage,
            "last_completed_stage": last,
            "next_stage": nxt,
            "defer": False,
            "software_defect_key": None,
        }
    if docs > 0 or complete:
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY if blocker else OUTCOME_COMPLETE,
            "stage_reached": stage,
            "last_completed_stage": last,
            "next_stage": nxt,
            "defer": bool(blocker),
            "software_defect_key": None,
        }
    if blocker:
        return {
            "outcome": OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": STAGE_ACCESS_DOCS,
            "last_completed_stage": STAGE_QUALIFY,
            "next_stage": STAGE_ACCESS_DOCS,
            "defer": True,
            "software_defect_key": None,
        }
    return {
        "outcome": OUTCOME_SOFTWARE_ERROR,
        "stage_reached": STAGE_ACCESS_DOCS,
        "last_completed_stage": STAGE_QUALIFY,
        "next_stage": STAGE_ACCESS_DOCS,
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

    timings: dict[str, float] = {}
    q = fast_qualify(item, l23_rec=l23_rec)
    timings["qualify_s"] = float(q.get("elapsed_s") or 0)
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
            "timings": timings,
        }

    # Shared-access fast path — no Playwright when barrier already scoped.
    t_access = time.time()
    try:
        from buyer_intelligence.shared_access_registry_v1 import resolve_fast_access

        access = resolve_fast_access(item, l23_rec=l23_rec)
    except Exception as exc:
        access = {"decision": "UNKNOWN", "defer": False, "error": type(exc).__name__}
    timings["access_lookup_s"] = round(time.time() - t_access, 4)
    if access.get("defer") and access.get("access_fast_path"):
        stages_run.append(STAGE_ACCESS_DOCS)
        return {
            "opportunity_id": oid,
            "build": BUILD,
            "AUTO_SEND": False,
            "title": item.get("title") or (l23_rec or {}).get("title"),
            "authoritative_url": item.get("authoritative_url"),
            "buyer": item.get("buyer") or (l23_rec or {}).get("buyer"),
            "funnel_outcome": access.get("funnel_outcome") or OUTCOME_EXTERNAL_DEPENDENCY,
            "stage_reached": STAGE_ACCESS_DOCS,
            "stages_run": stages_run,
            "defer": True,
            "terminal_blocker": access.get("terminal_blocker"),
            "package_blocker": access.get("package_blocker") or access.get("terminal_blocker"),
            "shared_access_key": access.get("shared_access_key"),
            "purchasing_group_id": access.get("purchasing_group_id"),
            "purchasing_group_registration_status": access.get(
                "purchasing_group_registration_status"
            ),
            "access_fast_path": True,
            "access_decision": access.get("decision"),
            "access_basis": access.get("access_basis"),
            "fetch_via": access.get("fetch_via") or "shared_access_registry_fast_path",
            "elapsed_s": round(time.time() - t0, 3),
            "finished_at": _utc(),
            "one_pass": True,
            "timings": timings,
        }

    try:
        t_proc = time.time()
        row = process_one(
            item,
            client=client,
            evidence_index=evidence_index,
            auth_status=auth_status,
            l23_rec=l23_rec or {},
            auth_client=auth_client,
        )
        timings["process_one_s"] = round(time.time() - t_proc, 3)
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
            "timings": timings,
        }

    # Learn verified access scope for subsequent opportunities
    try:
        from buyer_intelligence.shared_access_registry_v1 import learn_access

        learn_access(
            opportunity_id=oid,
            url=row.get("authoritative_url") or item.get("authoritative_url"),
            buyer=row.get("buyer") or item.get("buyer"),
            purchasing_group_id=row.get("purchasing_group_id"),
            paid_entitlement=(
                str(row.get("terminal_blocker") or row.get("package_blocker") or "")
                if str(row.get("terminal_blocker") or row.get("package_blocker") or "").startswith(
                    "PAID_ENTITLEMENT"
                )
                else row.get("paid_entitlement_requirement")
            ),
            blocker=str(row.get("terminal_blocker") or row.get("package_blocker") or "") or None,
        )
    except Exception:
        pass

    classification = classify_process_one_outcome(row)
    # Honest stage telemetry: only stages up to the furthest reached, not a fiction list
    stage_order = [
        STAGE_ACCESS_DOCS,
        STAGE_EXTRACT,
        STAGE_SOURCE,
        STAGE_PRICE,
        STAGE_ECONOMICS,
        STAGE_FINANCE,
        STAGE_BID_PREP,
    ]
    reached = str(classification.get("stage_reached") or STAGE_ACCESS_DOCS)
    if reached in stage_order:
        stages_run.extend(stage_order[: stage_order.index(reached) + 1])
    else:
        stages_run.append(STAGE_ACCESS_DOCS)
    # Mark pricing executed vs skipped for operators
    pr = row.get("price_research") or {}
    if pr.get("ok") or int(pr.get("price_evidence_count") or 0) > 0:
        if STAGE_PRICE not in stages_run:
            stages_run.append(STAGE_PRICE)
    row["funnel_outcome"] = classification["outcome"]
    row["stage_reached"] = classification["stage_reached"]
    row["last_completed_stage"] = classification.get("last_completed_stage") or row.get(
        "last_completed_stage"
    )
    row["next_stage"] = classification.get("next_stage") or row.get("next_stage")
    row["stages_run"] = stages_run
    row["defer"] = bool(classification.get("defer"))
    row["software_defect_key"] = classification.get("software_defect_key")
    row["shared_access_key"] = classification.get("shared_access_key") or row.get(
        "shared_access_key"
    )
    row["commercial_resume"] = bool(
        classification.get("commercial_resume") or row.get("commercial_resume")
    )
    row["one_pass"] = True
    row["orchestrator_build"] = BUILD
    row["elapsed_s"] = round(time.time() - t0, 3)
    row["finished_at"] = _utc()
    row["timings"] = {**(row.get("timings") or {}), **timings}
    row["access_decision"] = access.get("decision")
    return row
