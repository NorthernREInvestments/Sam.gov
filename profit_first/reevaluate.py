"""Re-evaluate canonical population through profit-first router."""

from __future__ import annotations

from typing import Any

from profit_first.router import evaluate_opportunity_profit
from profit_first.telemetry import record_disposition, save_evaluation_index


def reevaluate_canonical_population(
    *,
    limit: int | None = None,
    include_dead: bool = False,
    persist: bool = True,
    attach_to_store: bool = False,
) -> dict[str, Any]:
    """Score current L23 opportunities with profit-first economics routing."""
    from phase_l.l23_full_population_funnel import load_store, save_store
    from phase_l.owner_ui_service import DEAD_FUNNEL_STATES, _is_available_rec

    store = load_store()
    evaluations: list[dict[str, Any]] = []
    lie_cache: dict[str, Any] = {}
    try:
        from line_item_economics.engine import load_analysis

        # preload nothing — load per id
        _load_lie = load_analysis
    except Exception:
        _load_lie = lambda _oid: None  # noqa: E731

    count = 0
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        if not include_dead and not _is_available_rec(rec):
            # still allow WATCH / research states; skip only dead
            st = str(rec.get("current_funnel_state") or "")
            if st in DEAD_FUNNEL_STATES:
                continue
        lie = _load_lie(cid)
        signals = {
            "exact_identity": bool(rec.get("solicitation_event_id") or rec.get("authoritative_url")),
            "public_retail_available": bool(
                ((rec.get("row_ref") or {}) if isinstance(rec.get("row_ref"), dict) else {}).get("economics")
            ),
            "commercially_common": str(rec.get("jurisdiction") or "").upper() not in {"", "UNKNOWN"},
        }
        ev = evaluate_opportunity_profit(
            opportunity_id=cid,
            rec=rec,
            title=rec.get("title"),
            buyer=rec.get("buyer"),
            line_item_analysis=lie,
            ranking_signals=signals,
            execution_pass=None,
        )
        ev["platform"] = rec.get("platform")
        evaluations.append(ev)
        if persist:
            record_disposition(cid, ev, disposition=ev.get("route"))
        if attach_to_store:
            rec["profit_first"] = {
                "profit_status": (ev.get("economics") or {}).get("profit_status"),
                "expected_profit": (ev.get("economics") or {}).get("expected_profit"),
                "post_financing_profit": (ev.get("economics") or {}).get("post_financing_profit"),
                "route": ev.get("route"),
                "owner_card": ev.get("owner_card"),
                "ranking_score": (ev.get("ranking") or {}).get("profit_probability_score"),
                "research_priority": (ev.get("research") or {}).get("priority"),
                "evaluated_at": ev.get("evaluated_at"),
            }
        count += 1
        if limit is not None and count >= limit:
            break

    if attach_to_store:
        save_store(store)
    if persist:
        save_evaluation_index(evaluations)

    from profit_first.telemetry import profit_funnel_telemetry

    telem = profit_funnel_telemetry(evaluations)
    return {
        "kind": "ProfitFirstReevaluation",
        "evaluated": len(evaluations),
        "telemetry": telem,
        "sample_owner_candidates": [
            {
                "id": e["opportunity_id"],
                "title": (e.get("title") or "")[:80],
                "status": (e.get("economics") or {}).get("profit_status"),
                "profit": (e.get("economics") or {}).get("expected_profit"),
                "score": (e.get("ranking") or {}).get("profit_probability_score"),
            }
            for e in sorted(
                evaluations,
                key=lambda x: -((x.get("ranking") or {}).get("profit_probability_score") or 0),
            )
            if (e.get("economics") or {}).get("profit_status")
            in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"}
        ][:15],
    }
