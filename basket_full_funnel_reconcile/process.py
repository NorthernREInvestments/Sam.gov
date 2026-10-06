"""Per-opportunity basket completion → terminal economics."""

from __future__ import annotations

from typing import Any

from basket_full_funnel_reconcile.economics import classify_basket_ready, compute_economics
from basket_full_funnel_reconcile.lines import complete_opportunity_lines
from basket_full_funnel_reconcile.models import ITEM_DEADLINE_S
from basket_full_funnel_reconcile.owner_ui import (
    derive_canonical_stage,
    owner_funnel_view,
    owner_next_action,
    what_can_hurt_us,
)


def process_opportunity(
    corpus_row: dict[str, Any],
    *,
    stats: dict[str, Any] | None = None,
    deadline_s: float = ITEM_DEADLINE_S,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    oid = corpus_row["opportunity_id"]
    print(f"[basket] lines {oid}", flush=True)
    lines = complete_opportunity_lines(oid, corpus_row, stats=stats, deadline_s=deadline_s)
    basket = classify_basket_ready(lines)
    print(
        f"[basket] coverage {oid} line={lines.get('line_coverage')} "
        f"priced={lines.get('PRICED_LINES')}/{lines.get('TOTAL_LINES')} class={basket.get('basket_class')}",
        flush=True,
    )
    economics = compute_economics(oid, corpus_row, lines, basket)
    print(
        f"[basket] econ {oid} terminal={economics.get('economic_terminal')} "
        f"profit={economics.get('net_expected_profit')} margin={economics.get('margin_pct')}%",
        flush=True,
    )
    result = {
        "opportunity_id": oid,
        "corpus": corpus_row,
        "lines": lines,
        "basket": basket,
        "economics": economics,
        "buyer": corpus_row.get("buyer"),
        "source": corpus_row.get("source"),
    }
    result["canonical_stage"] = derive_canonical_stage(result)
    result["next_action"] = owner_next_action(result)
    result["what_can_hurt_us"] = what_can_hurt_us(result, corpus_row)
    result["owner_view"] = owner_funnel_view(result)
    return result
