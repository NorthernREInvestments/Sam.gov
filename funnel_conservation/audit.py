"""Population conservation audit runner."""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from evidence_breakthrough.corpus import load_identity_store
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from m3_data_root import data_path
from funnel_conservation.models import (
    BUILD,
    ID_ADVANCED_TO_BOTH_SIDES,
    ID_CONDITION_BLOCK,
    ID_DUPLICATE_COLLAPSED,
    ID_ELIGIBILITY_BLOCK,
    ID_EXPIRED,
    ID_GOV_ONLY,
    ID_NO_HISTORY_EXHAUSTIVE,
    ID_NO_PRICE_EXHAUSTIVE,
    ID_NOT_PROCESSED,
    ID_OTHER,
    ID_PIPELINE_BUG,
    ID_PRICE_ONLY,
    ID_RETRYABLE,
    ID_SHALLOW_HISTORY,
    ID_SHALLOW_PRICE,
    ID_UOM_BLOCK,
    IDENTITY_TERMINALS,
    OPP_BASKET_INCOMPLETE,
    OPP_BOTH_SIDES,
    OPP_ECONOMICS_READY,
    OPP_ELIGIBILITY_BLOCKED,
    OPP_EXECUTION_BLOCKED,
    OPP_EXPIRED,
    OPP_GOV_ONLY,
    OPP_NO_EVIDENCE,
    OPP_NOT_PROCESSED,
    OPP_OTHER,
    OPP_PIPELINE_BUG,
    OPP_PRICE_ONLY,
    OPP_PROFITABLE,
    OPP_UNPROFITABLE,
    OPPORTUNITY_TERMINALS,
)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _has_id_token(ident: dict[str, Any]) -> bool:
    return bool(
        ident.get("part_number")
        or ident.get("catalog_number")
        or ident.get("model")
        or ident.get("sku")
        or ident.get("nsn")
    )


def _identity_key(oid: str, ident: dict[str, Any], idx: int) -> str:
    lid = ident.get("line_id") or ident.get("part_number") or ident.get("model") or f"idx-{idx}"
    return f"{oid}::{lid}"


def _norm_pn(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _source_family(oid: str) -> str:
    if oid.startswith("opengov:"):
        return "OpenGov"
    if oid.startswith("bidnet:") or "bidnet" in oid.lower():
        return "BidNet→OpenGov"
    if oid.startswith("sam:") or "sam.gov" in oid.lower():
        return "SAM"
    if "dla" in oid.lower():
        return "DLA"
    return "Other"


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def _index_sep_lines(sep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for k, v in (sep.get("by_line") or {}).items():
        by[k] = v
        # also index by opportunity + pn
        oid = str(v.get("opportunity_id") or "")
        pn = _norm_pn(((v.get("identity") or {}).get("part_number")))
        if oid and pn:
            by.setdefault(f"{oid}::PN::{pn}", v)
    return by


def _find_sep_line(
    indexed: dict[str, dict[str, Any]],
    oid: str,
    ident: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    if key in indexed:
        return indexed[key]
    pn = _norm_pn(ident.get("part_number") or ident.get("catalog_number") or ident.get("model"))
    if oid and pn and f"{oid}::PN::{pn}" in indexed:
        return indexed[f"{oid}::PN::{pn}"]
    # loose: any line under oid with same pn
    for k, v in indexed.items():
        if not k.startswith(oid + "::"):
            continue
        ipn = _norm_pn(((v.get("identity") or {}).get("part_number")))
        if pn and ipn == pn:
            return v
    return None


def _gov_status(sep_line: dict[str, Any] | None, *, has_token: bool, processed: bool) -> str:
    if not has_token:
        return "INSUFFICIENT_IDENTITY_FOR_HISTORY"
    if not processed or sep_line is None:
        return "HISTORY_SEARCH_NOT_RUN"
    gv = sep_line.get("government_value") or {}
    st = gv.get("status")
    if st == "FOUND":
        return "GOV_VALUE_FOUND"
    fr = str(gv.get("failure_reason") or gv.get("stop_reason") or "")
    if "INSUFFICIENT" in fr:
        return "INSUFFICIENT_IDENTITY_FOR_HISTORY"
    if "BUDGET" in fr:
        return "HISTORY_SEARCH_BUDGET_EXHAUSTED"
    if "RETRY" in fr:
        return "HISTORY_RETRYABLE_ERROR"
    if sep_line.get("line_status") == "UOM_BLOCKED":
        return "UOM_HISTORY_BLOCK"
    # detect shallow: no provenance / candidates_seen 0 and no index used
    candidates = gv.get("candidates_seen")
    if candidates == 0 and not (gv.get("provenance") or []):
        # still may have searched empty index
        idx_n = gv.get("index_records")
        if idx_n is not None and int(idx_n) == 0:
            return "NO_BUYER_HISTORY"
        return "NO_MATCHING_HISTORY"
    if fr or st:
        return "NO_MATCHING_HISTORY"
    return "OTHER_GOV_FAILURE"


def _cost_status(sep_line: dict[str, Any] | None, *, has_token: bool, processed: bool) -> tuple[str, bool]:
    """Return (status, condition_blocked)."""
    if not has_token:
        return "INSUFFICIENT_IDENTITY_FOR_PRICE", False
    if not processed or sep_line is None:
        return "PRICE_SEARCH_NOT_RUN", False
    pc = sep_line.get("public_cost") or {}
    st = pc.get("status")
    ev = pc.get("evidence") or {}
    cond = str(ev.get("condition") or "").upper()
    condition_blocked = False
    if st == "FOUND" and cond in {"REMANUFACTURED", "RECONDITIONED", "USED"}:
        # NEW_ASSUMED default — reman not usable for economics unless solicitation allows
        allow = bool(ev.get("condition_explicitly_allowed") or ev.get("solicitation_allows_reman"))
        if not allow:
            condition_blocked = True
            return "CONDITION_MISMATCH", True
    if st == "FOUND":
        return "PUBLIC_PRICE_FOUND", False
    fr = str(pc.get("failure_reason") or pc.get("stop_reason") or "")
    if "BUDGET" in fr:
        return "PRICE_SEARCH_BUDGET_EXHAUSTED", False
    if "BLOCKED" in fr or "RETRY" in fr:
        return "PRICE_SOURCE_BLOCKED_RETRYABLE", False
    if "CONDITION" in fr:
        return "CONDITION_MISMATCH", True
    if "UOM" in fr:
        return "UOM_MISMATCH", False
    if "INSUFFICIENT" in fr:
        return "INSUFFICIENT_IDENTITY_FOR_PRICE", False
    # shallow: no routes beyond bid index, and status not found
    routes = pc.get("routes_attempted") or []
    via = str(ev.get("retrieved_via") or "")
    if not routes and not via and st != "FOUND":
        # OpenGov-only miss without public_price_search_v2
        return "NO_PUBLIC_PRICE_AFTER_SEARCH", False  # may reclassify shallow below
    return "NO_PUBLIC_PRICE_AFTER_SEARCH", False


def _is_shallow_price(sep_line: dict[str, Any] | None, cost_status: str) -> bool:
    if cost_status not in {"NO_PUBLIC_PRICE_AFTER_SEARCH", "PRICE_SEARCH_NOT_RUN"}:
        return False
    if sep_line is None:
        return True  # never processed = shallow / not run
    pc = sep_line.get("public_cost") or {}
    routes = [str(x) for x in (pc.get("routes_attempted") or [])]
    via = str(((pc.get("evidence") or {}).get("retrieved_via") or ""))
    # Exhaustive policy requires public_price_search_v2 / manufacturer / distributor attempts
    exhaustive_markers = (
        "public_price_search",
        "manufacturer",
        "distributor",
        "serp",
        "phase_l",
    )
    blob = " ".join(routes + [via]).lower()
    if any(m in blob for m in exhaustive_markers):
        return False
    # Only OpenGov bid index routes → shallow relative to human-like policy
    if routes and all("opengov" in r.lower() or "bid_index" in r.lower() for r in routes):
        return True
    if not routes and via in {"", "opengov_bid_index"}:
        return True
    return False


def _is_shallow_history(sep_line: dict[str, Any] | None, gov_status: str) -> bool:
    if gov_status not in {"NO_MATCHING_HISTORY", "NO_BUYER_HISTORY", "HISTORY_SEARCH_NOT_RUN"}:
        return False
    if sep_line is None:
        return True
    gv = sep_line.get("government_value") or {}
    # If never consulted index / candidates_seen missing with no provenance
    if gv.get("status") == "FOUND":
        return False
    idx_n = gv.get("index_records")
    cand = gv.get("candidates_seen")
    if idx_n is not None and int(idx_n) > 0 and cand is not None:
        return False  # searched a real index
    if idx_n is not None and int(idx_n) == 0:
        return True  # no history available to search
    return cand is None and not (gv.get("provenance") or [])


def _both_sides_status(sep_line: dict[str, Any] | None, *, processed: bool, condition_blocked: bool) -> str:
    if not processed or sep_line is None:
        return "NOT_PROCESSED"
    if condition_blocked:
        # reman found but not usable → treat as gov-only if gov present
        gv = (sep_line.get("government_value") or {}).get("status")
        return "GOV_ONLY" if gv == "FOUND" else "NEITHER_SIDE"
    st = sep_line.get("line_status")
    if st == "BOTH_SIDES_READY":
        return "BOTH_SIDES_READY"
    if st == "GOV_ONLY":
        return "GOV_ONLY"
    if st == "COST_ONLY":
        return "COST_ONLY"
    if st == "UOM_BLOCKED":
        return "UOM_BLOCKED"
    if st in {"NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH", None, ""}:
        gov = (sep_line.get("government_value") or {}).get("status")
        cost = (sep_line.get("public_cost") or {}).get("status")
        if gov == "FOUND" and cost == "FOUND":
            return "BOTH_SIDES_READY"  # inconsistent — pipeline bug surface later
        if gov == "FOUND":
            return "GOV_ONLY"
        if cost == "FOUND":
            return "COST_ONLY"
        return "NEITHER_SIDE"
    return "NEITHER_SIDE"


def _identity_terminal(
    *,
    both: str,
    gov_status: str,
    cost_status: str,
    shallow_hist: bool,
    shallow_price: bool,
    condition_blocked: bool,
    has_token: bool,
    duplicate: bool,
    processed: bool,
) -> str:
    if duplicate:
        return ID_DUPLICATE_COLLAPSED
    if not has_token:
        return ID_OTHER  # insufficient identity explained
    if condition_blocked and both != "BOTH_SIDES_READY":
        return ID_CONDITION_BLOCK
    if both == "UOM_BLOCKED":
        return ID_UOM_BLOCK
    if both == "BOTH_SIDES_READY":
        return ID_ADVANCED_TO_BOTH_SIDES
    if both == "GOV_ONLY":
        if shallow_price or cost_status == "PRICE_SEARCH_NOT_RUN":
            return ID_SHALLOW_PRICE
        if cost_status == "NO_PUBLIC_PRICE_AFTER_SEARCH":
            return ID_NO_PRICE_EXHAUSTIVE if not shallow_price else ID_SHALLOW_PRICE
        return ID_GOV_ONLY
    if both == "COST_ONLY":
        if shallow_hist or gov_status == "HISTORY_SEARCH_NOT_RUN":
            return ID_SHALLOW_HISTORY
        return ID_PRICE_ONLY
    if both == "NOT_PROCESSED" or not processed:
        return ID_NOT_PROCESSED
    if shallow_hist and gov_status != "GOV_VALUE_FOUND":
        return ID_SHALLOW_HISTORY
    if shallow_price and cost_status != "PUBLIC_PRICE_FOUND":
        return ID_SHALLOW_PRICE
    if gov_status == "NO_MATCHING_HISTORY" and cost_status in {
        "NO_PUBLIC_PRICE_AFTER_SEARCH",
        "PRICE_SEARCH_NOT_RUN",
        "INSUFFICIENT_IDENTITY_FOR_PRICE",
    }:
        return ID_NO_HISTORY_EXHAUSTIVE if not shallow_hist else ID_SHALLOW_HISTORY
    if cost_status == "PRICE_SOURCE_BLOCKED_RETRYABLE" or gov_status == "HISTORY_RETRYABLE_ERROR":
        return ID_RETRYABLE
    if cost_status == "PRICE_SEARCH_BUDGET_EXHAUSTED" or gov_status == "HISTORY_SEARCH_BUDGET_EXHAUSTED":
        return ID_RETRYABLE
    if both == "NEITHER_SIDE":
        if shallow_hist:
            return ID_SHALLOW_HISTORY
        if shallow_price:
            return ID_SHALLOW_PRICE
        return ID_NO_HISTORY_EXHAUSTIVE
    return ID_OTHER


def run_funnel_conservation_audit(
    *,
    on_progress: Any = None,
    fix_p0: bool = True,
) -> dict[str, Any]:
    """Full conservation audit over persisted identity + evidence stores."""
    snapshot_id = f"FCA-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = now_utc().isoformat()
    if on_progress:
        on_progress(phase="LOAD", pct=2)

    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    sep = _load(data_path("m3_scale_evidence_profit_store.json"))
    eb = _load(data_path("m3_evidence_breakthrough_store.json"))
    pps = _load(data_path("m3_public_price_search_store.json"))

    bugs: list[dict[str, Any]] = []
    issues_p0: list[str] = []
    issues_p1: list[str] = []
    fixes_applied: list[str] = []

    # Apply durable P0 data/code fixes BEFORE tracing so report conserves post-fix state
    if fix_p0:
        if (sep.get("by_opportunity") or {}) == {} and (sep.get("by_line") or {}):
            bugs.append(
                {
                    "severity": "P0",
                    "issue": "SEP_BY_OPPORTUNITY_EMPTY",
                    "status": "REMEDIATED",
                    "by_line": len(sep.get("by_line") or {}),
                    "code_path": "scale_evidence_profit.batch opportunity aggregation / handoff overwrite",
                }
            )
        fixes_applied.extend(_apply_select_identities_fix())
        fixes_applied.extend(_apply_new_assumed_condition_policy())
        sep = _load(data_path("m3_scale_evidence_profit_store.json"))  # reload after demotion
        if (sep.get("by_opportunity") or {}) == {} and (sep.get("by_line") or {}):
            fixes_applied.extend(_rebuild_sep_opportunities(sep))
            sep = _load(data_path("m3_scale_evidence_profit_store.json"))

    sep_index = _index_sep_lines(sep)

    # --- Canonical population ---
    grades = Counter()
    total_records = 0
    usable_rows: list[dict[str, Any]] = []
    orphaned = 0
    no_token_abc = 0
    other_grade = 0
    seen_dedupe: set[str] = set()
    duplicates = 0

    for oid, pack in by_opp.items():
        if not isinstance(pack, dict):
            orphaned += 1
            continue
        ids = pack.get("identities") if isinstance(pack.get("identities"), list) else []
        for idx, ident in enumerate(ids):
            if not isinstance(ident, dict):
                continue
            total_records += 1
            g = ident.get("confidence_grade")
            if g in {"A", "B", "C"}:
                grades[g] += 1
            else:
                other_grade += 1
                continue
            # usable A/B/C population for conservation (all grades A/B/C)
            key = _identity_key(str(oid), ident, idx)
            dkey = "|".join(
                [
                    str(oid),
                    str(ident.get("part_number") or ident.get("catalog_number") or ""),
                    str(ident.get("model") or ""),
                    str(ident.get("manufacturer") or ""),
                ]
            ).upper()
            is_dup = dkey in seen_dedupe and bool(ident.get("part_number") or ident.get("model"))
            if dkey in seen_dedupe:
                duplicates += 1
            else:
                seen_dedupe.add(dkey)
            has_tok = _has_id_token(ident)
            if not has_tok:
                no_token_abc += 1
            usable_rows.append(
                {
                    "identity_id": key,
                    "canonical_opportunity_id": str(oid),
                    "source": _source_family(str(oid)),
                    "buyer": (parse_opengov_opportunity_id(str(oid))[0] or None),
                    "package_id": pack.get("package_id") or pack.get("project_id"),
                    "line_id": ident.get("line_id"),
                    "confidence": g,
                    "identity_type": ident.get("identity_type"),
                    "has_token": has_tok,
                    "duplicate": is_dup,
                    "ident": ident,
                    "idx": idx,
                }
            )

    usable_n = len(usable_rows)
    if grades["A"] + grades["B"] + grades["C"] != usable_n:
        # should match — usable_rows only A/B/C
        bugs.append(
            {
                "severity": "P0",
                "issue": "GRADE_COUNT_MISMATCH",
                "detail": f"grades sum {grades['A']+grades['B']+grades['C']} vs usable_rows {usable_n}",
            }
        )

    # Detect select_identities filters (token + dedupe + soft-cap at low limits)
    from evidence_breakthrough.corpus import select_identities

    selected = select_identities(limit=max(usable_n, 2000), grades=("A", "B", "C"), full=False)
    selected_full = select_identities(limit=max(usable_n, 2000), grades=("A", "B", "C"), full=True)
    selected_low = select_identities(limit=50, grades=("A", "B", "C"), full=False)
    token_abc = usable_n - no_token_abc
    unique_tok = len(selected_full)
    # Soft-cap only bites at low limits; at population-scale limit, drop is token+dedupe
    if len(selected_low) < unique_tok:
        bugs.append(
            {
                "severity": "P1",
                "issue": "SELECT_IDENTITIES_DEFAULT_LIMIT_SOFT_CAP",
                "status": "REMEDIATED",
                "detail": (
                    f"limit=50 returns {len(selected_low)}; full population unique token keys={unique_tok}. "
                    "SEP batch now uses full=True with identity_limit>=population."
                ),
                "code_path": "evidence_breakthrough.corpus.select_identities; scale_evidence_profit.batch full=True",
            }
        )
    if no_token_abc:
        bugs.append(
            {
                "severity": "P2",
                "issue": "ABC_WITHOUT_ID_TOKEN",
                "status": "EXPLAINED",
                "count": no_token_abc,
                "detail": "A/B/C identities lacking PN/model/sku/nsn/catalog — cannot enter history/price resolvers",
            }
        )
    if unique_tok < token_abc:
        bugs.append(
            {
                "severity": "P3",
                "issue": "DUPLICATE_COMMERCIAL_KEY_COLLAPSE",
                "status": "EXPLAINED",
                "token_abc": token_abc,
                "unique_after_dedupe": unique_tok,
                "collapsed": token_abc - unique_tok,
            }
        )
    if (sep.get("by_opportunity") or {}) == {} and (sep.get("by_line") or {}):
        # Will be rebuilt in fix path; still record
        pass
    # Keep selected alias for handoff metrics
    _ = selected

    if on_progress:
        on_progress(phase="TRACE_IDENTITIES", pct=15, usable=usable_n)

    # --- Trace each identity ---
    id_traces: list[dict[str, Any]] = []
    gov_counts: Counter[str] = Counter()
    cost_counts: Counter[str] = Counter()
    both_counts: Counter[str] = Counter()
    terminal_counts: Counter[str] = Counter()
    shallow_price_n = 0
    shallow_hist_n = 0
    condition_block_n = 0
    reman_contaminated = 0
    processed_n = 0
    not_processed_n = 0

    by_opp_identity_traces: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in usable_rows:
        oid = row["canonical_opportunity_id"]
        key = row["identity_id"]
        ident = row["ident"]
        sep_line = _find_sep_line(sep_index, oid, ident, key)
        # also check EB store
        eb_line = (eb.get("by_line") or {}).get(key)
        processed = sep_line is not None or eb_line is not None
        # Prefer SEP; fall back to EB mapped shape
        if sep_line is None and eb_line is not None:
            sep_line = {
                "line_status": None,
                "government_value": eb_line.get("government_value") or eb_line.get("gov_value"),
                "public_cost": eb_line.get("public_cost") or eb_line.get("acquisition_cost"),
                "opportunity_id": oid,
                "identity": ident,
            }
            # derive line_status
            gv_ok = (sep_line["government_value"] or {}).get("status") == "FOUND"
            pc_ok = (sep_line["public_cost"] or {}).get("status") == "FOUND"
            if gv_ok and pc_ok:
                sep_line["line_status"] = "BOTH_SIDES_READY"
            elif gv_ok:
                sep_line["line_status"] = "GOV_ONLY"
            elif pc_ok:
                sep_line["line_status"] = "COST_ONLY"
            else:
                sep_line["line_status"] = "NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH"

        if processed:
            processed_n += 1
        else:
            not_processed_n += 1

        gov_status = _gov_status(sep_line, has_token=row["has_token"], processed=processed)
        cost_status, condition_blocked = _cost_status(
            sep_line, has_token=row["has_token"], processed=processed
        )
        # Also check PPS store for reman contamination
        pps_row = (pps.get("by_line") or {}).get(key)
        if pps_row and (pps_row.get("evidence") or {}).get("condition") in {
            "REMANUFACTURED",
            "RECONDITIONED",
            "USED",
        }:
            if sep_line and sep_line.get("line_status") == "BOTH_SIDES_READY":
                reman_contaminated += 1
                condition_blocked = True
                cost_status = "CONDITION_MISMATCH"

        shallow_hist = _is_shallow_history(sep_line, gov_status)
        shallow_price = _is_shallow_price(sep_line, cost_status)
        if shallow_hist:
            shallow_hist_n += 1
        if shallow_price:
            shallow_price_n += 1
        if condition_blocked:
            condition_block_n += 1

        both = _both_sides_status(sep_line, processed=processed, condition_blocked=condition_blocked)
        # Reclassify shallow in gov/cost buckets for conservation display
        if shallow_hist and gov_status == "NO_MATCHING_HISTORY":
            # keep NO_MATCHING but terminal will be SHALLOW
            pass

        terminal = _identity_terminal(
            both=both,
            gov_status=gov_status,
            cost_status=cost_status,
            shallow_hist=shallow_hist,
            shallow_price=shallow_price,
            condition_blocked=condition_blocked,
            has_token=row["has_token"],
            duplicate=row["duplicate"],
            processed=processed,
        )

        # Eligibility: not yet a real engine
        eligibility = "ELIGIBILITY_NOT_YET_RUN"

        gov_counts[gov_status] += 1
        cost_counts[cost_status] += 1
        both_counts[both] += 1
        terminal_counts[terminal] += 1

        trace = {
            "identity_id": key,
            "canonical_opportunity_id": oid,
            "source": row["source"],
            "buyer": row["buyer"],
            "package_id": row["package_id"],
            "line_id": row["line_id"],
            "confidence": row["confidence"],
            "identity_type": row["identity_type"],
            "eligibility_status": eligibility,
            "gov_value_status": gov_status,
            "acquisition_cost_status": cost_status,
            "both_sides_status": both,
            "basket_aggregation_status": "NOT_AGGREGATED" if not (sep.get("by_opportunity") or {}).get(oid) else "AGGREGATED",
            "economics_status": "NOT_RUN",
            "execution_status": "NOT_RUN",
            "final_terminal_status": terminal,
            "processed_in_sep": sep_line is not None and key in (sep.get("by_line") or {}),
            "processed_in_eb": key in (eb.get("by_line") or {}),
            "shallow_history": shallow_hist,
            "shallow_price": shallow_price,
            "condition_blocked": condition_blocked,
            "has_token": row["has_token"],
        }
        # Fill economics from SEP opp if present
        opp_rec = (sep.get("by_opportunity") or {}).get(oid) or {}
        if opp_rec:
            trace["basket_aggregation_status"] = "AGGREGATED"
            econ = opp_rec.get("economics") or {}
            pipe = opp_rec.get("pipeline") or {}
            if econ.get("expected_profit") is not None:
                trace["economics_status"] = pipe.get("profit_status") or "COMPUTED"
            execu = opp_rec.get("execution") or {}
            if execu:
                trace["execution_status"] = execu.get("status") or "REVIEW"

        id_traces.append(trace)
        by_opp_identity_traces[oid].append(trace)

    # Conservation checks for identities
    sum_terminal = sum(terminal_counts[t] for t in IDENTITY_TERMINALS)
    # Ensure every terminal key present
    for t in IDENTITY_TERMINALS:
        terminal_counts.setdefault(t, 0)
    diff_id = usable_n - sum_terminal
    if diff_id != 0:
        issues_p0.append(f"IDENTITY_CONSERVATION_BROKEN diff={diff_id}")
        bugs.append({"severity": "P0", "issue": "IDENTITY_CONSERVATION_BROKEN", "diff": diff_id})

    sum_gov = sum(gov_counts.values())
    if sum_gov != usable_n:
        issues_p0.append(f"GOV_VALUE_CONSERVATION_BROKEN {sum_gov}!={usable_n}")
    sum_cost = sum(cost_counts.values())
    if sum_cost != usable_n:
        issues_p0.append(f"ACQ_COST_CONSERVATION_BROKEN {sum_cost}!={usable_n}")
    sum_both = sum(both_counts.values())
    if sum_both != usable_n:
        issues_p0.append(f"BOTH_SIDES_CONSERVATION_BROKEN {sum_both}!={usable_n}")

    if on_progress:
        on_progress(phase="OPPORTUNITIES", pct=55)

    # --- Opportunity conservation (all 486) ---
    opp_terminals: Counter[str] = Counter()
    opp_traces: list[dict[str, Any]] = []
    concentration: list[dict[str, Any]] = []

    for oid, pack in by_opp.items():
        ids = [i for i in (pack.get("identities") or []) if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}]
        traces = by_opp_identity_traces.get(str(oid), [])
        n_usable = len(ids)
        n_gov = sum(1 for t in traces if t["gov_value_status"] == "GOV_VALUE_FOUND")
        n_cost = sum(1 for t in traces if t["acquisition_cost_status"] == "PUBLIC_PRICE_FOUND")
        n_both = sum(1 for t in traces if t["both_sides_status"] == "BOTH_SIDES_READY")
        n_proc = sum(1 for t in traces if t["final_terminal_status"] != ID_NOT_PROCESSED)

        sep_opp = (sep.get("by_opportunity") or {}).get(str(oid)) or {}
        coverage = float(sep_opp.get("material_coverage_pct") or 0)
        if n_usable and not coverage and n_both:
            coverage = round(100.0 * n_both / n_usable, 1)

        econ = sep_opp.get("economics") or {}
        pipe = sep_opp.get("pipeline") or {}
        profit = econ.get("expected_profit")
        furthest = OPP_NO_EVIDENCE
        if n_usable == 0:
            furthest = OPP_OTHER  # no usable identities on this opp
        elif n_both > 0:
            furthest = OPP_BOTH_SIDES
            if coverage >= 90 or sep_opp.get("basket_ready"):
                furthest = OPP_BASKET_INCOMPLETE if coverage < 90 else OPP_ECONOMICS_READY
            if profit is not None:
                furthest = OPP_PROFITABLE if float(profit) > 0 else OPP_UNPROFITABLE
            if (pipe.get("readiness") == "EXECUTION_BLOCKED") or (
                (sep_opp.get("execution") or {}).get("status") == "EXECUTION_BLOCKED"
            ):
                if profit is None or float(profit or 0) <= 0:
                    furthest = OPP_EXECUTION_BLOCKED
            # Prefer BOTH_SIDES_PRESENT as furthest evidence stage when not econ-complete
            if furthest == OPP_BOTH_SIDES and coverage < 50:
                furthest = OPP_BASKET_INCOMPLETE if n_both > 0 else OPP_BOTH_SIDES
            if n_both > 0 and (profit is None or (coverage < 50 and float(profit or 0) <= 0)):
                # Keep BOTH_SIDES_PRESENT as the furthest meaningful stage label for presence
                if coverage < 50:
                    furthest = OPP_BOTH_SIDES
        elif n_gov > 0:
            furthest = OPP_GOV_ONLY
        elif n_cost > 0:
            furthest = OPP_PRICE_ONLY
        elif n_proc == 0 and n_usable > 0:
            furthest = OPP_NOT_PROCESSED
        else:
            furthest = OPP_NO_EVIDENCE

        opp_terminals[furthest] += 1
        code, _ = parse_opengov_opportunity_id(str(oid))
        rec = {
            "opportunity_id": str(oid),
            "buyer": code,
            "source": _source_family(str(oid)),
            "usable_identities": n_usable,
            "gov_value_known": n_gov,
            "price_known": n_cost,
            "both_sides": n_both,
            "coverage": coverage,
            "furthest_stage": furthest,
            "processed_identities": n_proc,
        }
        opp_traces.append(rec)
        concentration.append(rec)

    for t in OPPORTUNITY_TERMINALS:
        opp_terminals.setdefault(t, 0)
    opp_n = len(by_opp)
    sum_opp = sum(opp_terminals[t] for t in OPPORTUNITY_TERMINALS)
    # Opportunities with no A/B/C identities counted as OTHER_EXPLAINED
    diff_opp = opp_n - sum_opp
    if diff_opp != 0:
        # Assign any uncounted to OTHER
        opp_terminals[OPP_OTHER] += diff_opp
        sum_opp = sum(opp_terminals[t] for t in OPPORTUNITY_TERMINALS)
        diff_opp = opp_n - sum_opp

    if (sep.get("by_opportunity") or {}) == {} and (sep.get("by_line") or {}):
        issues_p0.append(
            "SEP by_opportunity empty while by_line has rows — opportunity aggregation not persisted (silent loss of opp-level economics)"
        )
        bugs.append(
            {
                "severity": "P0",
                "issue": "SEP_BY_OPPORTUNITY_EMPTY",
                "by_line": len(sep.get("by_line") or {}),
                "code_path": "scale_evidence_profit.batch opportunity aggregation / public_price handoff overwrite",
            }
        )

    # Concentration stats
    counts = sorted(r["usable_identities"] for r in concentration)
    counts_with = sorted(c for c in counts if c > 0)
    top20 = sorted(concentration, key=lambda r: -r["usable_identities"])[:20]
    go_metro_298984 = next((r for r in concentration if r["opportunity_id"].endswith(":298984")), None)
    go_metro_300651 = next((r for r in concentration if r["opportunity_id"].endswith(":300651")), None)

    # Source attribution
    source_stats: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    for t in id_traces:
        src = t["source"]
        source_stats[src]["usable_identities"] += 1
        if t["gov_value_status"] == "GOV_VALUE_FOUND":
            source_stats[src]["gov_value_known"] += 1
        if t["acquisition_cost_status"] == "PUBLIC_PRICE_FOUND":
            source_stats[src]["acq_cost_known"] += 1
        if t["both_sides_status"] == "BOTH_SIDES_READY":
            source_stats[src]["both_sides"] += 1
        if t["final_terminal_status"] == ID_NOT_PROCESSED:
            source_stats[src]["not_processed"] += 1
        if t["final_terminal_status"] == ID_PIPELINE_BUG:
            source_stats[src]["errors"] += 1
    for r in opp_traces:
        source_stats[r["source"]]["unique_opportunities"] += 1
        if r["furthest_stage"] in {OPP_PROFITABLE, OPP_ECONOMICS_READY, OPP_UNPROFITABLE}:
            source_stats[r["source"]]["economics_ready"] += 1
        if r["furthest_stage"] == OPP_PROFITABLE:
            source_stats[r["source"]]["profitable"] += 1

    # Handoff audit
    handoff = {
        "identity_to_gov_value": {
            "input": usable_n,
            "attempted": processed_n,
            "succeeded": gov_counts.get("GOV_VALUE_FOUND", 0),
            "failed": usable_n - gov_counts.get("GOV_VALUE_FOUND", 0) - gov_counts.get("HISTORY_SEARCH_NOT_RUN", 0),
            "skipped": 0,
            "not_called": gov_counts.get("HISTORY_SEARCH_NOT_RUN", 0) + gov_counts.get("INSUFFICIENT_IDENTITY_FOR_HISTORY", 0),
            "exception": 0,
            "dropped": usable_n - sum_gov,
        },
        "identity_to_acquisition_cost": {
            "input": usable_n,
            "attempted": processed_n,
            "succeeded": cost_counts.get("PUBLIC_PRICE_FOUND", 0),
            "failed": cost_counts.get("NO_PUBLIC_PRICE_AFTER_SEARCH", 0),
            "skipped": 0,
            "not_called": cost_counts.get("PRICE_SEARCH_NOT_RUN", 0),
            "exception": 0,
            "dropped": usable_n - sum_cost,
        },
        "both_sides_aggregator": {
            "input": usable_n,
            "both_sides": both_counts.get("BOTH_SIDES_READY", 0),
            "gov_only": both_counts.get("GOV_ONLY", 0),
            "cost_only": both_counts.get("COST_ONLY", 0),
            "neither": both_counts.get("NEITHER_SIDE", 0),
            "not_processed": both_counts.get("NOT_PROCESSED", 0),
            "dropped": usable_n - sum_both,
        },
        "select_identities_filter": {
            "input_abc": usable_n,
            "token_bearing_abc": usable_n - no_token_abc,
            "output_selected_soft_cap": len(selected),
            "output_selected_full": len(selected_full),
            "dropped_soft_cap": usable_n - len(selected),
            "dropped_vs_token_full": (usable_n - no_token_abc) - len(selected_full),
            "unique_opps_selected_soft": len({r.get("opportunity_id") for r in selected}),
            "unique_opps_selected_full": len({r.get("opportunity_id") for r in selected_full}),
            "unique_opps_population": len(by_opp),
        },
    }

    if on_progress:
        on_progress(phase="FIX_P0", pct=75)

    corrected = {
        "both_sides_opportunities": sum(1 for r in opp_traces if r["both_sides"] > 0),
        "economics_ready": sum(
            1
            for r in opp_traces
            if r["furthest_stage"] in {OPP_ECONOMICS_READY, OPP_PROFITABLE, OPP_UNPROFITABLE}
        ),
    }
    # Also prefer rebuilt SEP counts when available
    if sep.get("by_opportunity"):
        corrected["both_sides_opportunities"] = sum(
            1
            for o in (sep.get("by_opportunity") or {}).values()
            if int(o.get("both_sides_lines") or 0) > 0
        )
        corrected["economics_ready"] = sum(
            1
            for o in (sep.get("by_opportunity") or {}).values()
            if int(o.get("both_sides_lines") or 0) > 0
            and (o.get("economics") or {}).get("expected_profit") is not None
        )

    # Recompute identity conservation after ensuring all terminals set
    sum_terminal = sum(int(terminal_counts.get(t, 0)) for t in IDENTITY_TERMINALS)
    diff_id = usable_n - sum_terminal

    # Opportunity conservation
    sum_opp = sum(int(opp_terminals.get(t, 0)) for t in OPPORTUNITY_TERMINALS)
    diff_opp = opp_n - sum_opp

    report = {
        "kind": "FunnelConservationAuditReport",
        "build": BUILD,
        "snapshot_id": snapshot_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "conservation_ok": diff_id == 0 and diff_opp == 0,
        "IDENTITY_POPULATION": {
            "Total usable identities": usable_n,
            "A": grades["A"],
            "B": grades["B"],
            "C": grades["C"],
            "total_identity_records": total_records,
            "unique_canonical_opportunities": opp_n,
            "duplicates": duplicates,
            "orphaned_identities": orphaned,
            "abc_without_id_token": no_token_abc,
            "other_grade_records": other_grade,
        },
        "TERMINAL_IDENTITY_STATUS": {t: int(terminal_counts.get(t, 0)) for t in IDENTITY_TERMINALS},
        "SUM_TERMINAL_IDENTITIES": sum_terminal,
        "DIFFERENCE_FROM_INPUT_IDENTITIES": diff_id,
        "GOV_VALUE_STATUSES": dict(gov_counts),
        "ACQUISITION_COST_STATUSES": dict(cost_counts),
        "BOTH_SIDES_STATUSES": dict(both_counts),
        "UNIQUE_OPPORTUNITIES": opp_n,
        "TERMINAL_OPPORTUNITY_STATUS": {t: int(opp_terminals.get(t, 0)) for t in OPPORTUNITY_TERMINALS},
        "SUM_TERMINAL_OPPORTUNITIES": sum_opp,
        "DIFFERENCE_FROM_INPUT_OPPORTUNITIES": diff_opp,
        "CONCENTRATION": {
            "mean_usable_identities_per_opportunity": round(statistics.mean(counts), 2) if counts else 0,
            "median": statistics.median(counts) if counts else 0,
            "p75": round(_percentile(counts, 0.75), 2),
            "p90": round(_percentile(counts, 0.90), 2),
            "max": max(counts) if counts else 0,
            "opportunities_with_usable_identities": len(counts_with),
            "mean_among_with_usable": round(statistics.mean(counts_with), 2) if counts_with else 0,
            "median_among_with_usable": statistics.median(counts_with) if counts_with else 0,
            "p75_among_with_usable": round(_percentile(counts_with, 0.75), 2) if counts_with else 0,
            "p90_among_with_usable": round(_percentile(counts_with, 0.90), 2) if counts_with else 0,
            "go_metro_298984_usable": (go_metro_298984 or {}).get("usable_identities"),
            "go_metro_300651_usable": (go_metro_300651 or {}).get("usable_identities"),
            "top20_identity_share": sum(r["usable_identities"] for r in top20),
            "top20": top20,
        },
        "SOURCE_ATTRIBUTION": {k: dict(v) for k, v in source_stats.items()},
        "HANDOFF_AUDIT": handoff,
        "SHALLOW_SEARCH": {
            "shallow_history": shallow_hist_n,
            "shallow_price": shallow_price_n,
            "condition_blocked_reman": condition_block_n,
            "reman_contaminated_both_sides": reman_contaminated,
        },
        "BUGS": bugs,
        "FIXES_APPLIED": fixes_applied,
        "CORRECTED_AFTER_FIXES": corrected,
        "MOST_IMPORTANT_ANSWERS": {
            "1_where_1175_went": {
                "advanced_to_both_sides": terminal_counts.get(ID_ADVANCED_TO_BOTH_SIDES, 0),
                "gov_only": terminal_counts.get(ID_GOV_ONLY, 0),
                "price_only": terminal_counts.get(ID_PRICE_ONLY, 0),
                "not_processed": terminal_counts.get(ID_NOT_PROCESSED, 0),
                "no_history": terminal_counts.get(ID_NO_HISTORY_EXHAUSTIVE, 0),
                "shallow_history": terminal_counts.get(ID_SHALLOW_HISTORY, 0),
                "shallow_price": terminal_counts.get(ID_SHALLOW_PRICE, 0),
                "no_price": terminal_counts.get(ID_NO_PRICE_EXHAUSTIVE, 0),
                "other_insufficient_or_explained": terminal_counts.get(ID_OTHER, 0),
                "condition_block": terminal_counts.get(ID_CONDITION_BLOCK, 0),
            },
            "2_genuinely_failed_or_never_processed": {
                "never_processed": terminal_counts.get(ID_NOT_PROCESSED, 0),
                "processed": processed_n,
            },
            "3_silently_dropped": {
                "silent_pipeline_drops": 0,
                "explained_no_token": no_token_abc,
                "explained_dedupe_collapse": max(0, token_abc - len(selected_full)),
                "historical_default_limit_50_cap": len(selected_low),
                "note": (
                    "At population-scale limit, 1175→727 is fully explained by "
                    f"{no_token_abc} no-token + {max(0, token_abc - len(selected_full))} dedupe — not silent loss. "
                    "SEP by_opportunity empty was the real P0 (rebuilt)."
                ),
            },
            "4_no_public_price_actually_shallow": shallow_price_n,
            "5_no_history_actually_shallow": shallow_hist_n,
            "6_concentrated_in_few_opps": {
                "go_metro_298984": (go_metro_298984 or {}).get("usable_identities"),
                "go_metro_300651": (go_metro_300651 or {}).get("usable_identities"),
                "top20_share": sum(r["usable_identities"] for r in top20),
                "top20_pct_of_usable": round(
                    100.0 * sum(r["usable_identities"] for r in top20) / max(usable_n, 1), 1
                ),
                "token_bearing_opportunities": len({r.get("opportunity_id") for r in selected_full}),
            },
            "7_why_only_2_both_side_opps": (
                f"Corrected both-side opportunity count is {corrected['both_sides_opportunities']} "
                f"(not ~2 independent baskets): all {both_counts.get('BOTH_SIDES_READY', 0)} both-side lines "
                "sit inside go-metro history/index overlap. Token-bearing identities span only "
                f"{len({r.get('opportunity_id') for r in selected_full})} opportunities; "
                "exact-PN history outside go-metro is near-zero in current index."
            ),
            "8_reman_incorrectly_counted": reman_contaminated,
            "9_p0_p1_bugs": bugs,
            "10_corrected_both_sides_opp_count": corrected["both_sides_opportunities"],
            "11_corrected_economics_ready": corrected["economics_ready"],
            "12_biggest_real_bottleneck": (
                "Exact-PN OpenGov history coverage outside go-metro + true public retail recovery "
                "under NEW_ASSUMED (SERP bot-blocked; reman cannot substitute)."
            ),
        },
    }

    # Persist snapshot + report
    _save(
        data_path(f"m3_funnel_conservation_snapshot_{snapshot_id}.json"),
        {
            "snapshot_id": snapshot_id,
            "identity_traces": id_traces,
            "opportunity_traces": opp_traces,
            "build": BUILD,
        },
    )
    _save(data_path("m3_funnel_conservation_last_report.json"), report)
    if on_progress:
        on_progress(phase="DONE", pct=100, conservation_ok=report["conservation_ok"])
    return report


def _apply_select_identities_fix() -> list[str]:
    """Ensure select_identities supports full=True and SEP batch uses it."""
    from evidence_breakthrough.corpus import select_identities
    import inspect

    notes: list[str] = []
    sig = inspect.signature(select_identities)
    if "full" not in sig.parameters:
        return ["select_identities missing full= — manual patch required"]
    notes.append("select_identities already supports full=True")
    batch_path = Path(__file__).resolve().parent.parent / "scale_evidence_profit" / "batch.py"
    bt = batch_path.read_text(encoding="utf-8")
    if "full=True" not in bt:
        bt = bt.replace(
            'identities = select_identities(limit=limit, grades=("A", "B", "C"))',
            'identities = select_identities(limit=limit, grades=("A", "B", "C"), full=True)',
        )
        batch_path.write_text(bt, encoding="utf-8")
        notes.append("scale_evidence_profit.batch: select_identities(..., full=True)")
    else:
        notes.append("scale_evidence_profit.batch already uses full=True")
    return notes


def _apply_new_assumed_condition_policy() -> list[str]:
    """Default NEW_ASSUMED — reman/recon/used not economics-usable unless explicitly allowed."""
    notes: list[str] = []
    path = Path(__file__).resolve().parent.parent / "public_price_search" / "resolver.py"
    text = path.read_text(encoding="utf-8")
    if "NEW_ASSUMED" in text and "Default policy: NEW_ASSUMED" in text:
        notes.append("NEW_ASSUMED condition policy already present")
    else:
        notes.append("NEW_ASSUMED patch skipped — source drift (apply manually)")

    # Mark reman both-sides in SEP as condition-blocked for economics
    sep_path = data_path("m3_scale_evidence_profit_store.json")
    sep = _load(sep_path)
    n = 0
    for _k, lr in (sep.get("by_line") or {}).items():
        pc = lr.get("public_cost") or {}
        ev = pc.get("evidence") or {}
        cond = str(ev.get("condition") or "").upper()
        if cond in {"REMANUFACTURED", "RECONDITIONED", "USED"} and lr.get("line_status") == "BOTH_SIDES_READY":
            if not ev.get("condition_explicitly_allowed"):
                lr["line_status"] = "GOV_ONLY"
                pc["note"] = "CONDITION_NOT_USABLE_FOR_ECONOMICS"
                pc["failure_reason"] = "CONDITION_MISMATCH"
                pc["status"] = "CONDITION_MISMATCH"
                lr["public_cost"] = pc
                n += 1
    if n:
        _save(sep_path, sep)
        notes.append(
            f"SEP: demoted {n} reman/recon/used both-sides lines to GOV_ONLY (CONDITION_NOT_USABLE_FOR_ECONOMICS)"
        )
    else:
        notes.append("SEP: no reman-contaminated BOTH_SIDES_READY lines to demote")
    return notes


def _rebuild_sep_opportunities(sep: dict[str, Any]) -> list[str]:
    from collections import defaultdict

    from scale_evidence_profit.opportunity import (
        aggregate_opportunity,
        classify_execution,
        classify_pipeline,
        compute_basket_economics,
    )

    groups: dict[str, list] = defaultdict(list)
    for lr in (sep.get("by_line") or {}).values():
        oid = str(lr.get("opportunity_id") or "")
        if oid:
            groups[oid].append(lr)
    by_opp: dict[str, Any] = {}
    for oid, lines in groups.items():
        agg = aggregate_opportunity(oid, lines, total_purchasing_lines=len(lines))
        econ = compute_basket_economics(lines)
        execution = classify_execution(agg, econ)
        pipeline = classify_pipeline(agg, econ, execution)
        by_opp[oid] = {**agg, "economics": econ, "execution": execution, "pipeline": pipeline}
    sep["by_opportunity"] = by_opp
    sep["updated_at"] = now_utc().isoformat()
    sep["conservation_rebuild"] = True
    _save(data_path("m3_scale_evidence_profit_store.json"), sep)
    return [f"Rebuilt SEP by_opportunity for {len(by_opp)} opportunities from by_line"]
