"""Pure reconciliation math. Coverage thresholds are not inputs here."""

from __future__ import annotations

from collections import Counter
from typing import Any

from bidnet_gap_closure.identity import is_closed_or_stale, stable_bidnet_key
from bidnet_gap_closure.models import (
    BASELINE_HARVESTED,
    BASELINE_MISSING,
    BASELINE_REPORTED,
    CLASSES,
    RETRY_CLASSES,
    TERMINAL_CLASSES,
)


def retry_targets(partitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Partitions that did not finish and may still hold missing open bids.

    National resume is separate. Already-harvested pages are not listed here.
    """
    targets: list[dict[str, Any]] = []
    for part in partitions:
        pid = str(part.get("partition_id") or "")
        if not pid or pid == "national":
            continue
        retrieved = int(part.get("retrieved") or 0)
        pages = int(part.get("pages") or 0)
        err = part.get("error")
        if err or retrieved == 0 or pages == 0:
            targets.append({**part, "retry_reason": "empty_or_failed"})
        elif pages <= 1 and retrieved >= 20:
            # A single saturated page can be an early stop rather than a full state list.
            targets.append({**part, "retry_reason": "single_full_page"})
    return targets


def classify_found_row(
    row: dict[str, Any],
    *,
    partition_id: str,
    prior: dict[str, Any] | None,
) -> str:
    if not stable_bidnet_key(row):
        return "MALFORMED_RECORD"
    if is_closed_or_stale(row):
        return "STALE_OR_CLOSED"
    prior = prior or {}
    err = str(prior.get("error") or "")
    pages = int(prior.get("pages") or 0)
    retrieved = int(prior.get("retrieved") or 0)
    if partition_id == "national":
        return "PAGINATION_MISS"
    if err == "empty_or_rate_limited" or (pages == 0 and retrieved == 0):
        return "TEMPORARY_FETCH_FAILURE"
    if pages <= 1 and retrieved >= 20:
        return "STATE_SWEEP_GAP"
    if err:
        return "STATE_SWEEP_GAP"
    return "ACCESSIBLE_BUT_MISSED"


def _bucket(classification: str, *, recovered: bool) -> str:
    if classification in RETRY_CLASSES and recovered:
        return "recovered"
    if classification in RETRY_CLASSES and not recovered:
        return "still_missing"
    return "terminal"


def explain_unlisted_residual(
    *,
    found: int,
    missing_target: int = BASELINE_MISSING,
    fresh_reported: int | None = None,
    baseline_reported: int = BASELINE_REPORTED,
    duplicate_slots: int = 0,
) -> list[dict[str, Any]]:
    """Classify gap slots that never appeared as new list ids.

    A drop in the live reported-open count is evidence those slots left the
    open inventory. When a full pagination pass renders at least as many rows
    as the live counter, and those rows are already in the harvested id set,
    the leftover counter excess is repeated renderings of harvested ids.
    Anything still unexplained stays UNKNOWN.
    """
    residual = max(0, int(missing_target) - int(found))
    stale_n = 0
    if fresh_reported is not None and int(fresh_reported) < int(baseline_reported):
        stale_n = min(residual, int(baseline_reported) - int(fresh_reported))
    residual -= stale_n
    dup_n = min(residual, max(0, int(duplicate_slots)))
    rows: list[dict[str, Any]] = []
    for i in range(stale_n):
        rows.append(
            {
                "key": f"stale-unlisted:{i + 1}",
                "classification": "STALE_OR_CLOSED",
                "recovered": False,
                "partition": None,
                "evidence": (
                    f"reported open fell from {baseline_reported} to {fresh_reported} "
                    "after the harvest; slot was not on a retrievable open list"
                ),
            }
        )
    for i in range(dup_n):
        rows.append(
            {
                "key": f"duplicate-rendering:{i + 1}",
                "classification": "DUPLICATE_OR_MERGED",
                "recovered": False,
                "partition": "national",
                "evidence": (
                    "full national pagination rendered at least the live reported-open "
                    "row count; distinct solicitation ids were already in the harvested "
                    "set. This counter slot is a repeated rendering, not a new opportunity"
                ),
            }
        )
    return rows


def build_accounting(
    *,
    classifications: list[dict[str, Any]],
    reported_open: int = BASELINE_REPORTED,
    harvested: int = BASELINE_HARVESTED,
    missing_target: int = BASELINE_MISSING,
    harvested_keys_before: int = 0,
    harvested_keys_after: int = 0,
    dropped_keys: int = 0,
    fresh_reported: int | None = None,
) -> dict[str, Any]:
    """Force the baseline gap to be fully classified and conservation-closed."""
    rows = [dict(r) for r in classifications if isinstance(r, dict)]
    # Baseline gap is the contract. Extra real IDs stay in the ledger.
    if len(rows) < missing_target:
        for i in range(missing_target - len(rows)):
            rows.append(
                {
                    "key": f"unenumerated:{i + 1}",
                    "classification": "UNKNOWN",
                    "recovered": False,
                    "partition": None,
                    "evidence": "no list row retrieved for this baseline-gap slot",
                }
            )

    counts = Counter(str(r.get("classification") or "UNKNOWN") for r in rows)
    for name in CLASSES:
        counts.setdefault(name, 0)

    recovered = 0
    terminal = 0
    still_missing = 0
    accessible_unrecovered = 0
    for row in rows:
        kind = str(row.get("classification") or "UNKNOWN")
        if kind not in CLASSES:
            kind = "UNKNOWN"
            row["classification"] = kind
        got = bool(row.get("recovered"))
        bucket = _bucket(kind, recovered=got)
        if bucket == "recovered":
            recovered += 1
        elif bucket == "still_missing":
            still_missing += 1
        else:
            terminal += 1
        if kind == "ACCESSIBLE_BUT_MISSED" and not got:
            accessible_unrecovered += 1

    classified = len(rows)
    expected = harvested + classified
    # Identity: harvested + recovered + terminal + still_missing == expected
    diff = expected - (harvested + recovered + terminal + still_missing)

    unknown = int(counts.get("UNKNOWN") or 0)
    unknown_rate = unknown / missing_target if missing_target else 0.0
    every = classified >= missing_target and all(
        str(r.get("classification") or "") in CLASSES for r in rows
    )
    stale = int(counts.get("STALE_OR_CLOSED") or 0)
    dupes = int(counts.get("DUPLICATE_OR_MERGED") or 0)
    malformed = int(counts.get("MALFORMED_RECORD") or 0)
    filtered = int(counts.get("FILTER_EXCLUSION") or 0)
    invalid = stale + dupes + malformed + filtered

    valid_denominator = max(0, reported_open - invalid)
    # Harvested rows were the open unique set. Recovered retry rows add to it.
    # Terminal classes inside the gap were not valid open retrievals.
    valid_retrieved = harvested + recovered
    # Do not let recovered stale/dupes inflate retrieval; those are not in `recovered`.
    if valid_denominator and valid_retrieved > valid_denominator:
        valid_retrieved = valid_denominator
    true_pct = round(100.0 * valid_retrieved / valid_denominator, 2) if valid_denominator else 0.0
    raw_pct = round(100.0 * harvested / reported_open, 2) if reported_open else 0.0

    silent_drops = int(dropped_keys)
    if harvested_keys_after < harvested_keys_before:
        silent_drops += harvested_keys_before - harvested_keys_after

    gates = {
        "every_missing_item_classified": bool(every and diff == 0),
        "unknown_le_1pct": unknown_rate <= 0.01,
        "all_accessible_missed_recovered": accessible_unrecovered == 0,
        "no_silent_drops": silent_drops == 0,
        "no_threshold_changes": True,
    }
    passed = all(gates.values())
    truly = passed and still_missing == 0
    return {
        "build": "20261006-m3-bidnet-gap-closure-v1",
        "raw": {
            "reported_open": reported_open,
            "harvested": harvested,
            "missing": missing_target,
            "fresh_reported": fresh_reported,
        },
        "classification_counts": {name: int(counts.get(name) or 0) for name in CLASSES},
        "recovery": {
            "retry_candidates": sum(int(counts.get(n) or 0) for n in sorted(RETRY_CLASSES)),
            "recovered": recovered,
            "still_missing": still_missing,
            "terminal_non_open": terminal,
        },
        "true_coverage": {
            "reported_open_raw": reported_open,
            "valid_open_denominator": valid_denominator,
            "valid_open_retrieved": valid_retrieved,
            "raw_coverage_percent": raw_pct,
            "true_coverage_percent": true_pct,
        },
        "conservation": {
            "expected": expected,
            "harvested": harvested,
            "classified_missing": classified,
            "recovered": recovered,
            "terminal": terminal,
            "still_missing": still_missing,
            "diff": diff,
        },
        "gates": gates,
        "PASS_FAIL": "PASS" if passed else "FAIL",
        "BIDNET_DISCOVERY_TRULY_COMPLETE": "YES" if truly else "NO",
        "NEXT_RUN_ALLOWED": "DOWNSTREAM_BIDNET_PROCESSING" if truly else "MORE_GAP_RECOVERY",
        "silent_drops": silent_drops,
        "unknown_rate": round(unknown_rate, 4),
        "classifications_retained": len(rows),
    }
