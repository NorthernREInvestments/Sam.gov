"""Opportunity-first diversity prioritization + early economic sampling."""

from __future__ import annotations

from typing import Any

from eligibility_and_recovery.models import (
    ELIGIBLE_TO_RESEARCH,
    PER_OPP_EXPAND_LINES,
    PER_OPP_SAMPLE_LINES,
)


def _has_token(ident: dict[str, Any]) -> bool:
    return bool(
        ident.get("part_number")
        or ident.get("catalog_number")
        or ident.get("model")
        or ident.get("sku")
        or ident.get("nsn")
        or ident.get("_recovered_token")
    )


def score_opportunity(
    oid: str,
    *,
    eligibility_status: str,
    identities: list[dict[str, Any]],
    gov_history_available: bool = False,
    package_chars: int = 0,
) -> tuple:
    """Lower tuple sorts first (higher priority)."""
    elig_rank = 0 if eligibility_status in ELIGIBLE_TO_RESEARCH else (
        1 if eligibility_status == "ELIGIBILITY_UNKNOWN" else 9
    )
    n = len(identities)
    tok = sum(1 for i in identities if _has_token(i))
    exact_ratio = tok / n if n else 0.0
    # Prefer manageable mid-size baskets over 500-line giants for diversity
    size_pen = 0 if 3 <= n <= 40 else (1 if n <= 80 else 2)
    hist = 0 if gov_history_available else 1
    pkg = 0 if package_chars >= 200 else 1
    # Slightly deprioritize already-huge go-metro so other opps get turns
    mega = 1 if n >= 200 else 0
    return (elig_rank, mega, size_pen, hist, pkg, -exact_ratio, -min(n, 60), oid)


def prioritize_opportunities(
    opp_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scored = sorted(opp_rows, key=lambda r: score_opportunity(
        r["opportunity_id"],
        eligibility_status=r.get("eligibility_status") or "ELIGIBILITY_UNKNOWN",
        identities=r.get("identities") or [],
        gov_history_available=bool(r.get("gov_history_available")),
        package_chars=int(r.get("package_chars") or 0),
    ))
    return scored


def select_lines_for_pass(
    identities: list[dict[str, Any]],
    *,
    pass_kind: str = "sample",
    already_done: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Pick representative lines: token-bearing + high-grade first, then recovered."""
    already_done = already_done or set()
    cap = PER_OPP_SAMPLE_LINES if pass_kind == "sample" else PER_OPP_EXPAND_LINES

    def _key(i: dict[str, Any]) -> tuple:
        g = {"A": 0, "B": 1, "C": 2}.get(str(i.get("confidence_grade") or ""), 3)
        tok = 0 if _has_token(i) else 1
        rec = 0 if i.get("_recovered_token") else 1
        return (tok, g, rec)

    ranked = sorted(identities, key=_key)
    out: list[dict[str, Any]] = []
    for ident in ranked:
        lid = str(ident.get("line_id") or ident.get("part_number") or ident.get("raw_description") or "")[:120]
        if lid in already_done:
            continue
        out.append(ident)
        if len(out) >= cap:
            break
    return out


def sample_economics_verdict(line_results: list[dict[str, Any]]) -> str:
    """Early sample classification — not final profit proof."""
    both = [r for r in line_results if r.get("line_status") == "BOTH_SIDES_READY"]
    if len(both) < 2:
        return "SAMPLE_INCONCLUSIVE"
    profits = []
    for r in both:
        gov = ((r.get("government_value") or {}).get("evidence") or {})
        cost = ((r.get("public_cost") or {}).get("evidence") or {})
        try:
            gv = float(gov.get("unit_price") or gov.get("awarded_unit_price") or 0)
            cv = float(cost.get("unit_price") or 0)
            qty = float(((r.get("identity") or {}).get("quantity")) or 1)
        except (TypeError, ValueError):
            continue
        if gv > 0 and cv > 0:
            profits.append((gv - cv) * qty)
    if not profits:
        return "SAMPLE_INCONCLUSIVE"
    total = sum(profits)
    if total < 0 and all(p < 0 for p in profits):
        return "ECONOMICALLY_UNPROMISING_SAMPLE"
    if total > 0:
        return "SAMPLE_PROMISING"
    return "SAMPLE_INCONCLUSIVE"
