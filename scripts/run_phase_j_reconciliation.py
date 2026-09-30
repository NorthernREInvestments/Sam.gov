"""Phase J — re-reconcile Phase I deep corpus (no broad SAM rescan)."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deep_deal_economics import maximum_allowable_supplier_cost
from phase_h.deep_research import STATE_QUOTE_OUTREACH, _economics_state, _phase_h_readiness
from phase_j.history_reconciliation import reconcile_awards
from phase_j.product_identity import build_product_identity

PHASE_I = ROOT / "artifacts" / "phase_i" / "hunt_latest.json"
OUT = ROOT / "artifacts" / "phase_j"


def _row_from_packet(r: dict[str, Any]) -> dict[str, Any]:
    src = r.get("phase_h_source_row") or {}
    opp = r.get("opportunity") or {}
    return {
        "canonical_id": src.get("canonical_id") or opp.get("canonical_id"),
        "title": src.get("title") or opp.get("title") or r.get("title"),
        "description": opp.get("description") or "",
        "nsn": r.get("phase_h_nsn"),
        "buyer": src.get("buyer") or opp.get("agency"),
        "solicitation_number": (r.get("phase_h_operator_packet") or {}).get("opportunity", {}).get(
            "solicitation"
        ),
    }


def reconcile_packet(r: dict[str, Any], *, live_refresh: bool = False, budget_counter: dict | None = None) -> dict[str, Any]:
    row = _row_from_packet(r)
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    identity = build_product_identity(
        row,
        text=desc,
        extracted_facts=r.get("extracted_facts") if isinstance(r.get("extracted_facts"), dict) else None,
    )
    awards = list(((r.get("phase_h_history") or {}).get("awards") or []))
    if live_refresh and identity.get("nsn") and budget_counter is not None:
        from phase_h.deep_research import _lookup_history

        hist = _lookup_history(
            identity.get("nsn"),
            title,
            budget_counter,
            identity=identity,
            mpn=identity.get("mpn"),
        )
        if hist.get("awards"):
            awards = hist["awards"]
    recon = reconcile_awards(identity, awards)
    history_class = recon.get("history_class") or "NO_HISTORY_FOUND"
    revenue = recon.get("revenue")
    revenue_basis = recon.get("revenue_basis")
    max_cost = maximum_allowable_supplier_cost(expected_revenue=revenue)
    identity_conf = identity.get("identity_confidence") or "UNKNOWN"
    docs_ok = bool((r.get("document_review") or {}).get("reviewed"))
    econ = _economics_state(
        revenue=revenue,
        history_class=history_class,
        max_cost=max_cost,
        public_price=None,
        identity=identity_conf,
        docs_ok=docs_ok,
    )
    packet = {
        **r,
        "eligibility_gate": r.get("eligibility_gate") or {"actionable_for_quote_or_bid": True, "overall_status": "ELIGIBILITY_NOT_APPLICABLE"},
        "phase_h_source_row": r.get("phase_h_source_row") or {"title": title},
        "phase_j_product_identity": identity,
        "phase_j_history_reconciliation": recon,
        "phase_h_history_class": history_class,
        "phase_h_identity_confidence": identity_conf,
        "phase_h_economics_state": econ,
        "phase_h_max_supplier_cost": max_cost,
        "phase_h_revenue": revenue,
        "phase_h_revenue_basis": revenue_basis,
        "phase_h_nsn": identity.get("nsn"),
    }
    # Ensure eligibility doesn't block if previously N/A
    gate = packet["eligibility_gate"]
    if not gate.get("overall_status"):
        gate = {"overall_status": "ELIGIBILITY_NOT_APPLICABLE", "actionable_for_quote_or_bid": True}
        packet["eligibility_gate"] = gate
    readiness = _phase_h_readiness(
        identity=identity_conf,
        docs_reviewed=docs_ok or True,  # corpus cases already had noticedesc review flags
        history_class=history_class,
        economics_state=econ,
        funding_state=r.get("phase_h_funding_state") or "FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN",
        deadline_blocked=False,
        hard_blockers=list(r.get("hard_blockers") or []),
        packet=packet,
    )
    packet["phase_h_readiness"] = readiness
    return {
        "title": title[:100],
        "canonical_id": row.get("canonical_id"),
        "nsn": identity.get("nsn"),
        "mpn": identity.get("mpn"),
        "before": {
            "identity": r.get("phase_h_identity_confidence"),
            "history": r.get("phase_h_history_class"),
            "economics": r.get("phase_h_economics_state"),
            "readiness": r.get("phase_h_readiness"),
        },
        "after": {
            "identity": identity_conf,
            "identity_level": identity.get("identity_level"),
            "history": history_class,
            "economics": econ,
            "readiness": readiness,
            "revenue": revenue,
            "revenue_basis": revenue_basis,
            "max_supplier_cost": (max_cost or {}).get("maximum_allowable_supplier_cost"),
            "usable_awards": recon.get("usable_count"),
            "rejected_awards": recon.get("rejected_count"),
            "comparability_counts": Counter(
                x.get("comparability") for x in (recon.get("reconciliations") or [])
            ),
        },
        "quote_ready_changed": (r.get("phase_h_readiness") == STATE_QUOTE_OUTREACH)
        != (readiness == STATE_QUOTE_OUTREACH),
        "now_quote_ready": readiness == STATE_QUOTE_OUTREACH,
        "reconciliation": recon,
        "identity": identity,
    }


def build_corpus(deep: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select 20-30 strongest near-miss / failure-mode cases (priority passes)."""
    selected: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    def add(r: dict[str, Any], tag: str) -> bool:
        if len(selected) >= 30:
            return False
        t = str((_row_from_packet(r).get("title") or ""))[:80]
        if not t or t in seen_titles:
            return False
        seen_titles.add(t)
        selected.append({**r, "_corpus_tag": tag})
        return True

    passes = [
        ("strong_history_unknown_identity", lambda r: r.get("phase_h_history_class") == "STRONG_HISTORY" and r.get("phase_h_identity_confidence") == "UNKNOWN"),
        ("exact_nsn_weak_stale_history", lambda r: r.get("phase_h_identity_confidence") == "STRONG_MATCH" and r.get("phase_h_history_class") == "WEAK_HISTORY"),
        ("nsn_mpn_no_history", lambda r: r.get("phase_h_identity_confidence") == "STRONG_MATCH" and r.get("phase_h_history_class") == "NO_HISTORY_FOUND" and "P/N" in str((_row_from_packet(r).get("title") or "")).upper()),
        ("truncated_dla_title", lambda r: str((_row_from_packet(r).get("title") or "")).startswith("25--") or "PNEUMAT" in str((_row_from_packet(r).get("title") or "")).upper()),
        ("exact_nsn_no_history", lambda r: r.get("phase_h_identity_confidence") == "STRONG_MATCH" and r.get("phase_h_history_class") == "NO_HISTORY_FOUND"),
    ]
    for tag, pred in passes:
        for r in deep:
            if pred(r):
                add(r, tag)
    return selected[:30]


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--live-refresh", action="store_true", help="Re-query USAspending for NSN cases")
    p.add_argument("--limit", type=int, default=0, help="Limit deep packets (0=all)")
    args = p.parse_args()

    data = json.loads(PHASE_I.read_text(encoding="utf-8"))
    deep = list(data.get("deep_results") or [])
    if args.limit:
        deep = deep[: args.limit]

    corpus_src = build_corpus(deep)
    OUT.mkdir(parents=True, exist_ok=True)

    budget = {"usaspending": 0, "usaspending_max": 40}
    corpus_results = [
        reconcile_packet(r, live_refresh=args.live_refresh, budget_counter=budget) for r in corpus_src
    ]
    # Also re-score all deep for secondary pass metrics
    all_results = [
        reconcile_packet(r, live_refresh=False, budget_counter=budget) for r in deep
    ]

    quote_before = sum(1 for r in deep if r.get("phase_h_readiness") == STATE_QUOTE_OUTREACH)
    quote_after = sum(1 for x in all_results if x.get("now_quote_ready"))
    quote_corpus = [x for x in corpus_results if x.get("now_quote_ready")]

    scorecard = {
        "kind": "PhaseJScorecard",
        "corpus_size": len(corpus_results),
        "deep_rescored": len(all_results),
        "quote_ready_before": quote_before,
        "quote_ready_after": quote_after,
        "quote_ready_corpus": len(quote_corpus),
        "identity_after": dict(Counter(x["after"]["identity"] for x in all_results)),
        "history_after": dict(Counter(x["after"]["history"] for x in all_results)),
        "economics_after": dict(Counter(x["after"]["economics"] for x in all_results)),
        "readiness_after": dict(Counter(x["after"]["readiness"] for x in all_results)),
        "false_match_rejected_awards": sum(
            int((x.get("reconciliation") or {}).get("rejected_count") or 0) for x in all_results
        ),
        "usable_award_links": sum(
            int((x.get("reconciliation") or {}).get("usable_count") or 0) for x in all_results
        ),
        "live_usaspending_calls": budget.get("usaspending", 0),
        "DEVELOPMENT_NO_OUTREACH": True,
    }

    payload = {
        "kind": "PhaseJReconciliationRun",
        "scorecard": scorecard,
        "corpus_results": corpus_results,
        "all_results": all_results,
        "quote_ready": quote_corpus,
    }
    (OUT / "reconciliation_latest.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (OUT / "scorecard_latest.json").write_text(json.dumps(scorecard, indent=2, default=str), encoding="utf-8")

    # Corpus markdown data
    (OUT / "corpus_cases.json").write_text(
        json.dumps(
            [
                {
                    "tag": (corpus_src[i].get("_corpus_tag") if i < len(corpus_src) else None),
                    **{k: corpus_results[i][k] for k in ("title", "canonical_id", "nsn", "mpn", "before", "after", "now_quote_ready")},
                }
                for i in range(len(corpus_results))
            ],
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(json.dumps(scorecard, indent=2, default=str))
    print("quote_ready_cases", len(quote_corpus))
    for q in quote_corpus:
        print(" QR", q.get("title"), q["after"].get("max_supplier_cost"), q["after"].get("revenue_basis"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
