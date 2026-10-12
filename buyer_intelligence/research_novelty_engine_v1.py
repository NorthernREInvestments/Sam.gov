"""Research Novelty Engine — suppress identical re-research; rotate fresh opportunities.

Tracks per-opportunity research state and decides whether a renewed pass is justified.
Does not permanently suppress failed attempts; lowers priority for unchanged evidence.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.commercial_proof_repair_v1 import EXACT_PRODUCT_VERIFIED
from buyer_intelligence.first_revenue_validation_sprint_v1 import _days_to_deadline
from buyer_intelligence.product_line_store_v1 import load_store
from m3_data_root import data_path

BUILD = "20261012-m3-research-novelty-engine-v1"
OUT = "_og_commercial_research_pipeline_out"
STATE_REL = f"{OUT}/m3_research_novelty_state_v1.json"
SELECTION_REL = f"{OUT}/m3_research_rotation_selection_v1.json"
REPORT_REL = f"{OUT}/m3_research_novelty_report_v1.json"

# Prior commercial sprint IDs (Tampa / Dayton / Greenville and heavy-researched set)
HEAVILY_RESEARCHED = {
    "8e7ae0c1316f0d7f",  # Tampa
    "777499f8df1dcb7f",  # Dayton
    "65a9793c66fae9a8",  # Greenville
}

# Allocation targets (initial)
ALLOC_FRESH = 0.50
ALLOC_ESTABLISHED_NEW_EVIDENCE = 0.25
ALLOC_SUPPLIER_FIRST = 0.15
ALLOC_EXPLORE = 0.10

# Category freshness (days) before stale evidence justifies revisit
FRESHNESS_DAYS = {
    "default": 14,
    "commodities": 7,
    "specialty": 21,
}


def _state_path() -> Path:
    return Path(data_path(STATE_REL))


def load_novelty_state() -> dict[str, Any]:
    blob = _load(STATE_REL)
    if not isinstance(blob, dict):
        blob = {"kind": "research_novelty_state_v1", "build": BUILD, "by_opportunity": {}}
    blob.setdefault("by_opportunity", {})
    blob["build"] = BUILD
    return blob


def save_novelty_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _utc()
    state["build"] = BUILD
    _save(STATE_REL, state)


def evidence_version_hash(entry: dict[str, Any]) -> str:
    """Stable hash of product-line identity + deadline + source docs."""
    lines = entry.get("LINES") or []
    payload = {
        "deadline": entry.get("DEADLINE"),
        "title": entry.get("TITLE"),
        "buyer": entry.get("BUYER"),
        "lines": [
            {
                "mpn": ln.get("MPN"),
                "desc": (str(ln.get("DESCRIPTION") or ""))[:120],
                "qty": ln.get("QTY"),
                "uom": ln.get("UOM"),
                "identity": ln.get("IDENTITY_CLASS"),
                "src": ln.get("SOURCE_DOCUMENT"),
            }
            for ln in lines
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def task_fingerprint(task: str, method: str, sources: list[str], evidence_hash: str) -> str:
    blob = "|".join([task, method, ",".join(sorted(sources or [])), evidence_hash])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def record_research_attempt(
    opportunity_id: str,
    *,
    task: str,
    method: str,
    sources: list[str] | None = None,
    status: str,
    prices_recovered: int = 0,
    historical_recovered: int = 0,
    rfqs_produced: int = 0,
    blockers: list[str] | None = None,
    cost_usd: float = 0.0,
    operator_refresh: bool = False,
    evidence_hash: str | None = None,
) -> dict[str, Any]:
    state = load_novelty_state()
    row = state["by_opportunity"].setdefault(
        opportunity_id,
        {
            "opportunity_id": opportunity_id,
            "attempts": [],
            "methods_tried": [],
            "sources_searched": [],
            "task_fps_done": [],
            "prices_recovered_total": 0,
            "historical_recovered_total": 0,
            "rfqs_produced_total": 0,
            "last_research_at": None,
            "last_evidence_hash": None,
            "operator_refresh_requested": False,
            "diminishing_returns_score": 0,
        },
    )
    eh = evidence_hash or row.get("last_evidence_hash") or "unknown"
    fp = task_fingerprint(task, method, sources or [], eh)
    attempt = {
        "at": _utc(),
        "task": task,
        "method": method,
        "sources": list(sources or []),
        "status": status,
        "prices_recovered": prices_recovered,
        "historical_recovered": historical_recovered,
        "rfqs_produced": rfqs_produced,
        "blockers": list(blockers or []),
        "cost_usd": cost_usd,
        "evidence_hash": eh,
        "task_fp": fp,
    }
    row["attempts"].append(attempt)
    row["attempts"] = row["attempts"][-50:]
    if method and method not in row["methods_tried"]:
        row["methods_tried"].append(method)
    for s in sources or []:
        if s and s not in row["sources_searched"]:
            row["sources_searched"].append(s)
    if fp not in row["task_fps_done"]:
        row["task_fps_done"].append(fp)
    row["task_fps_done"] = row["task_fps_done"][-200:]
    row["prices_recovered_total"] = int(row.get("prices_recovered_total") or 0) + int(prices_recovered)
    row["historical_recovered_total"] = int(row.get("historical_recovered_total") or 0) + int(historical_recovered)
    row["rfqs_produced_total"] = int(row.get("rfqs_produced_total") or 0) + int(rfqs_produced)
    row["last_research_at"] = _utc()
    row["last_evidence_hash"] = eh
    if operator_refresh:
        row["operator_refresh_requested"] = True
    # Diminishing returns: identical unsuccessful pass
    if status in {"NO_NEW_EVIDENCE", "FAILED_SOURCE", "SUPPRESSED_DUPLICATE"} and prices_recovered == 0:
        row["diminishing_returns_score"] = int(row.get("diminishing_returns_score") or 0) + 1
    elif prices_recovered or historical_recovered or rfqs_produced:
        row["diminishing_returns_score"] = max(0, int(row.get("diminishing_returns_score") or 0) - 2)
    if blockers is not None:
        row["current_blockers"] = list(blockers)
    state["by_opportunity"][opportunity_id] = row
    save_novelty_state(state)
    return attempt


def should_revisit(
    opportunity_id: str,
    *,
    task: str,
    method: str,
    sources: list[str] | None = None,
    current_evidence_hash: str,
    deadline_changed: bool = False,
    new_documents: bool = False,
    new_price_published: bool = False,
    buyer_award_discovered: bool = False,
    financing_changed: bool = False,
    operator_refresh: bool = False,
    category: str = "default",
) -> dict[str, Any]:
    """Decide if research should run. Suppress identical unchanged work."""
    state = load_novelty_state()
    row = (state.get("by_opportunity") or {}).get(opportunity_id) or {}
    fp = task_fingerprint(task, method, sources or [], current_evidence_hash)

    reasons_allow: list[str] = []
    if operator_refresh or row.get("operator_refresh_requested"):
        reasons_allow.append("operator_refresh")
    if new_documents:
        reasons_allow.append("new_documents")
    if method and method not in (row.get("methods_tried") or []):
        reasons_allow.append("new_method")
    if new_price_published:
        reasons_allow.append("new_price_published")
    if buyer_award_discovered:
        reasons_allow.append("buyer_award")
    if financing_changed:
        reasons_allow.append("financing_changed")
    if deadline_changed:
        reasons_allow.append("deadline_changed")
    prev_hash = row.get("last_evidence_hash")
    if prev_hash and prev_hash != current_evidence_hash:
        reasons_allow.append("evidence_hash_changed")

    # Staleness
    last_at = row.get("last_research_at")
    fresh_days = FRESHNESS_DAYS.get(category, FRESHNESS_DAYS["default"])
    if last_at:
        try:
            ts = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if now_utc() - ts > timedelta(days=fresh_days):
                reasons_allow.append("stale_under_freshness_policy")
        except Exception:
            pass
    else:
        reasons_allow.append("never_researched")

    # Identical task already done on same evidence → suppress unless allow reason beyond never/stale alone for heavy set
    identical_done = fp in (row.get("task_fps_done") or [])
    if identical_done and not any(
        r in reasons_allow
        for r in (
            "operator_refresh",
            "new_documents",
            "new_method",
            "new_price_published",
            "buyer_award",
            "financing_changed",
            "deadline_changed",
            "evidence_hash_changed",
        )
    ):
        # Stale alone is not enough to rerun identical method on heavily researched without new evidence
        if opportunity_id in HEAVILY_RESEARCHED and "stale_under_freshness_policy" in reasons_allow and len(reasons_allow) == 1:
            return {
                "allowed": False,
                "reason": "DIMINISHING_RETURNS_HEAVY_UNCHANGED",
                "task_fp": fp,
                "diminishing_returns_score": row.get("diminishing_returns_score") or 0,
            }
        if identical_done and not reasons_allow:
            return {"allowed": False, "reason": "IDENTICAL_TASK_SUPPRESSED", "task_fp": fp}
        if identical_done and set(reasons_allow) <= {"never_researched"}:
            return {"allowed": False, "reason": "IDENTICAL_TASK_SUPPRESSED", "task_fp": fp}
        if identical_done and "stale_under_freshness_policy" not in reasons_allow and "never_researched" not in reasons_allow:
            # has real new-evidence reasons — allow
            pass
        elif identical_done and set(reasons_allow) <= {"stale_under_freshness_policy", "never_researched"}:
            # Lower priority but allow if not heavy diminishing
            if int(row.get("diminishing_returns_score") or 0) >= 3:
                return {
                    "allowed": False,
                    "reason": "DIMINISHING_RETURNS",
                    "task_fp": fp,
                    "diminishing_returns_score": row.get("diminishing_returns_score"),
                }

    if identical_done and not reasons_allow:
        return {"allowed": False, "reason": "IDENTICAL_TASK_SUPPRESSED", "task_fp": fp}

    return {
        "allowed": True,
        "reason": ",".join(reasons_allow) or "eligible",
        "task_fp": fp,
        "diminishing_returns_score": row.get("diminishing_returns_score") or 0,
        "prior_attempts": len(row.get("attempts") or []),
    }


def _is_product_resale_compatible(entry: dict[str, Any]) -> bool:
    title = str(entry.get("TITLE") or "").lower()
    blob = title + " " + " ".join(str(ln.get("DESCRIPTION") or "") for ln in (entry.get("LINES") or [])[:5]).lower()
    banned = (
        "construction",
        "renovation",
        "install only",
        "labor only",
        "subcontractor",
        "janitorial service",
        "landscap",
        "paving",
        "roofing",
    )
    if any(b in blob for b in banned) and "supply" not in blob and "product" not in blob:
        # soft filter — allow if has clear MPN product lines
        lines = entry.get("LINES") or []
        if not any(ln.get("MPN") for ln in lines):
            return False
    return True


def select_rotation_cohort(
    *,
    limit: int = 50,
    min_fresh: int = 30,
) -> dict[str, Any]:
    """Persist a 50-opp cohort: ≥30 meaningfully under-researched."""
    store = load_store(force=True)
    by = store.get("by_opportunity") or {}
    novelty = load_novelty_state()
    nov_by = novelty.get("by_opportunity") or {}

    # Seed prior CEX researched IDs
    cex_log = _load(f"{OUT}/m3_cex_v1_task_log.json") or {}
    prior_researched: set[str] = set(HEAVILY_RESEARCHED)
    for row in cex_log.get("rows") or cex_log.get("tasks") or []:
        oid = str(row.get("opportunity_id") or "")
        if oid:
            prior_researched.add(oid)
    for oid, nrow in nov_by.items():
        if (nrow.get("attempts") or []) or nrow.get("last_research_at"):
            prior_researched.add(str(oid))

    candidates: list[dict[str, Any]] = []
    for oid, e in by.items():
        if str(oid).startswith("__test"):
            continue
        title = str(e.get("TITLE") or "")
        if title.lower() in {"test", "city test"} or title.lower().startswith("city test"):
            continue
        lines = e.get("LINES") or []
        if not lines:
            continue
        if not _is_product_resale_compatible(e):
            continue
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 1:
            continue
        exact = sum(1 for ln in lines if ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED or ln.get("MPN"))
        n = len(lines)
        eh = evidence_version_hash(e)
        nrow = nov_by.get(oid) or {}
        researched = oid in prior_researched
        dim = int(nrow.get("diminishing_returns_score") or 0)
        score = 0
        lane_hint = "explore"
        # Prefer 1–5 mandatory lines, exact MPN, qty present
        with_qty = sum(1 for ln in lines if ln.get("QTY") not in (None, "", 0, "0"))
        if 1 <= n <= 5:
            score += 40
            lane_hint = "fresh_simple"
        elif n <= 12:
            score += 15
        if exact > 0:
            score += 35
        if with_qty == n and n > 0:
            score += 20
        if days and days >= 7:
            score += 12
        if e.get("SOURCE") == "OpenGov":
            score += 8
        if e.get("SOURCE") == "BidNet" and oid not in HEAVILY_RESEARCHED:
            score -= 10
        if not researched:
            score += 50
            lane_hint = "fresh"
        else:
            score -= 20 + min(30, dim * 5)
            if oid in HEAVILY_RESEARCHED:
                score -= 40
                lane_hint = "established"
                # Keep a few high-value with new-evidence potential
                if nrow.get("operator_refresh_requested"):
                    score += 60
        candidates.append(
            {
                "opportunity_id": oid,
                "title": title,
                "buyer": e.get("BUYER"),
                "source": e.get("SOURCE"),
                "deadline": e.get("DEADLINE"),
                "line_count": n,
                "exact_lines": exact,
                "with_qty": with_qty,
                "days_left": days,
                "score": score,
                "lane_hint": lane_hint,
                "previously_researched": researched,
                "evidence_hash": eh,
                "diminishing_returns_score": dim,
            }
        )

    candidates.sort(key=lambda x: -x["score"])
    n_fresh = max(min_fresh, int(limit * ALLOC_FRESH))
    n_est = max(1, int(limit * ALLOC_ESTABLISHED_NEW_EVIDENCE))
    n_sup = max(1, int(limit * ALLOC_SUPPLIER_FIRST))
    n_exp = max(1, limit - n_fresh - n_est - n_sup)

    fresh = [c for c in candidates if not c["previously_researched"]]
    established = [c for c in candidates if c["previously_researched"] and c["opportunity_id"] in HEAVILY_RESEARCHED]
    other_prior = [c for c in candidates if c["previously_researched"] and c["opportunity_id"] not in HEAVILY_RESEARCHED]

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _take(pool: list[dict[str, Any]], n: int, bucket: str) -> None:
        for c in pool:
            if len([x for x in selected if x.get("allocation_bucket") == bucket]) >= n:
                break
            if c["opportunity_id"] in seen:
                continue
            row = dict(c)
            row["allocation_bucket"] = bucket
            selected.append(row)
            seen.add(c["opportunity_id"])

    _take(fresh, n_fresh, "fresh_under_researched")
    _take(established + other_prior, n_est, "established_new_evidence")
    # Supplier-first: exact MPN + few lines
    supplier_pool = sorted(
        [c for c in candidates if c["exact_lines"] > 0 and c["line_count"] <= 5],
        key=lambda x: (-x["exact_lines"], -x["score"]),
    )
    _take(supplier_pool, n_sup, "supplier_first")
    _take(candidates, n_exp, "category_explore")
    # Fill to limit
    _take(candidates, limit, "fill")

    selected = selected[:limit]
    fresh_count = sum(1 for s in selected if not s["previously_researched"])
    # If not enough fresh, pull more fresh even if over allocation
    if fresh_count < min_fresh:
        for c in fresh:
            if c["opportunity_id"] in seen:
                continue
            row = dict(c)
            row["allocation_bucket"] = "fresh_backfill"
            selected.append(row)
            seen.add(c["opportunity_id"])
            fresh_count += 1
            if fresh_count >= min_fresh or len(selected) >= limit + 10:
                break
        selected = selected[: max(limit, min_fresh)]

    payload = {
        "kind": "research_rotation_selection_v1",
        "build": BUILD,
        "selected_at": _utc(),
        "limit": limit,
        "min_fresh": min_fresh,
        "allocation_targets": {
            "fresh": ALLOC_FRESH,
            "established_new_evidence": ALLOC_ESTABLISHED_NEW_EVIDENCE,
            "supplier_first": ALLOC_SUPPLIER_FIRST,
            "explore": ALLOC_EXPLORE,
        },
        "counts": {
            "selected": len(selected),
            "fresh": sum(1 for s in selected if not s["previously_researched"]),
            "previously_researched": sum(1 for s in selected if s["previously_researched"]),
            "heavily_researched_included": sum(1 for s in selected if s["opportunity_id"] in HEAVILY_RESEARCHED),
        },
        "opportunity_ids": [s["opportunity_id"] for s in selected],
        "opportunities": selected,
    }
    _save(SELECTION_REL, payload)
    return payload


def novelty_dashboard() -> dict[str, Any]:
    state = load_novelty_state()
    sel = _load(SELECTION_REL) or {}
    return {
        "kind": "research_novelty_dashboard",
        "build": BUILD,
        "tracked_opportunities": len(state.get("by_opportunity") or {}),
        "selection": sel.get("counts"),
        "selection_ids_persisted": len(sel.get("opportunity_ids") or []),
        "heavily_researched_guard": sorted(HEAVILY_RESEARCHED),
    }
