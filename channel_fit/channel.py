"""Channel competition class + dominance score from historical bidder evidence."""

from __future__ import annotations

from collections import Counter
from typing import Any

from channel_fit.registry import (
    classify_bidder_type,
    detect_vertical,
    normalize_name,
    role_for_company,
    owner_override_lookup,
    DISTRIBUTOR_REGISTRY,
)

CHANNEL_CLASSES = (
    "A_RESELLER_FRIENDLY",
    "B_MIXED_CHANNEL",
    "C_DISTRIBUTOR_ADVANTAGED",
    "D_CHANNEL_DOMINATED",
    "UNKNOWN",
)


def _bidder_rows(opportunity: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (
        opportunity.get("historical_bidders")
        or opportunity.get("bid_tab")
        or opportunity.get("bidders")
        or []
    )
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        if isinstance(row, str):
            out.append({"name": row})
        elif isinstance(row, dict):
            out.append(row)
    return out


def classify_historical_bidders(
    opportunity: dict[str, Any],
    *,
    vertical: str | None,
) -> dict[str, Any]:
    rows = _bidder_rows(opportunity)
    typed = []
    counts = Counter()
    winners_dist = 0
    winners_total = 0
    for row in rows:
        name = str(row.get("name") or row.get("bidder") or row.get("vendor") or "")
        btype = str(row.get("bidder_type") or "").upper()
        if btype not in {"DISTRIBUTOR", "RESELLER", "MANUFACTURER", "UNKNOWN"}:
            btype = classify_bidder_type(name, vertical=vertical)
        amount = row.get("bid_amount") or row.get("amount")
        winner = bool(row.get("winner") or row.get("awarded") or row.get("is_winner"))
        typed.append(
            {
                "bidder_name": name,
                "bidder_type": btype,
                "bid_amount": amount,
                "winner": winner,
                "role": role_for_company(name, vertical=vertical, channel_class=None),
            }
        )
        counts[btype] += 1
        if winner:
            winners_total += 1
            if btype == "DISTRIBUTOR":
                winners_dist += 1

    total = sum(counts.values()) or 0

    def pct(key: str) -> float:
        return round(100.0 * counts.get(key, 0) / total, 1) if total else 0.0

    return {
        "historical_bidders": typed,
        "DISTRIBUTOR_BIDDER_PERCENT": pct("DISTRIBUTOR"),
        "RESELLER_BIDDER_PERCENT": pct("RESELLER"),
        "MANUFACTURER_BIDDER_PERCENT": pct("MANUFACTURER"),
        "UNKNOWN_BIDDER_PERCENT": pct("UNKNOWN"),
        "distributor_win_percent": round(100.0 * winners_dist / winners_total, 1) if winners_total else None,
        "bidder_count": total,
        "provenance": {
            "source": "opportunity.historical_bidders|bid_tab|bidders",
            "classified_via": "channel_fit.registry",
        },
    }


def channel_dominance_score(
    *,
    bidder_stats: dict[str, Any],
    vertical: str | None,
    title: str,
    public_vs_gov_gap_pct: float | None,
    single_brand_concentration: float | None = None,
    basket_in_vertical_catalog: bool = False,
) -> tuple[int, list[str]]:
    """0–100 CHANNEL_DOMINANCE_SCORE with explicit reasons."""
    score = 0
    reasons: list[str] = []
    dist_pct = float(bidder_stats.get("DISTRIBUTOR_BIDDER_PERCENT") or 0)
    win_pct = bidder_stats.get("distributor_win_percent")

    if bidder_stats.get("bidder_count", 0) == 0:
        # No historical bidders — use vertical catalog concentration only
        if basket_in_vertical_catalog and vertical:
            score += 45
            reasons.append(f"basket_in_{vertical}_catalog_without_bid_history")
        else:
            return 0, ["no_historical_bidder_evidence"]
    else:
        # Distributor bidder share
        if dist_pct >= 80:
            score += 45
            reasons.append(f"distributor_bidders_{dist_pct}%")
        elif dist_pct >= 60:
            score += 35
            reasons.append(f"distributor_bidders_{dist_pct}%")
        elif dist_pct >= 40:
            score += 20
            reasons.append(f"distributor_bidders_{dist_pct}%")
        elif dist_pct >= 20:
            score += 10
            reasons.append(f"distributor_bidders_{dist_pct}%")

        if win_pct is not None:
            if win_pct >= 80:
                score += 25
                reasons.append(f"distributor_wins_{win_pct}%")
            elif win_pct >= 50:
                score += 15
                reasons.append(f"distributor_wins_{win_pct}%")

    if basket_in_vertical_catalog and vertical:
        score += 15
        reasons.append(f"core_{vertical}_catalog_basket")

    if public_vs_gov_gap_pct is not None:
        # Public/list far above historical gov price → channel economics we may lack
        if public_vs_gov_gap_pct >= 40:
            score += 20
            reasons.append(f"public_above_gov_{public_vs_gov_gap_pct}%")
        elif public_vs_gov_gap_pct >= 25:
            score += 12
            reasons.append(f"public_above_gov_{public_vs_gov_gap_pct}%")
        elif public_vs_gov_gap_pct >= 15:
            score += 6
            reasons.append(f"public_above_gov_{public_vs_gov_gap_pct}%")

    if single_brand_concentration is not None and single_brand_concentration >= 0.7:
        score += 10
        reasons.append("single_brand_concentration")

    # Known vertical + known distributors named in title/buyer notes
    blob = normalize_name(title)
    if vertical:
        hits = sum(1 for n in DISTRIBUTOR_REGISTRY.get(vertical, ()) if n in blob)
        if hits:
            score += 5
            reasons.append("distributor_named_in_title")

    return min(100, max(0, int(score))), reasons


def channel_competition_class(score: int, *, has_bidder_evidence: bool) -> str:
    if not has_bidder_evidence and score < 20:
        return "UNKNOWN"
    if score <= 20:
        return "A_RESELLER_FRIENDLY"
    if score <= 40:
        return "B_MIXED_CHANNEL"
    if score <= 60:
        return "C_DISTRIBUTOR_ADVANTAGED"
    if score <= 100:
        return "D_CHANNEL_DOMINATED"
    return "UNKNOWN"


def score_channel(opportunity: dict[str, Any], *, public_basket: dict[str, Any] | None = None) -> dict[str, Any]:
    oid = str(opportunity.get("stable_key") or opportunity.get("opportunity_id") or opportunity.get("canonical_opportunity_id") or "")
    override = owner_override_lookup(oid)
    title = str(opportunity.get("title") or "")
    vertical = detect_vertical(title, opportunity.get("category"))
    bidder_stats = classify_historical_bidders(opportunity, vertical=vertical)

    gov = (public_basket or {}).get("government_value")
    pub = (public_basket or {}).get("PUBLIC_BASKET_VALUE")
    gap_pct = None
    if gov and pub and float(gov) > 0 and float(pub) > float(gov):
        gap_pct = round(100.0 * (float(pub) - float(gov)) / float(gov), 1)

    # Basket in vertical catalog: title strongly matches a distributor vertical
    basket_in_catalog = vertical is not None and bool(
        __import__("re").search(
            r"\b(water\s+materials|waterworks|pipe\s+and\s+fittings|hydrant)\b",
            title,
            __import__("re").I,
        )
    )

    score, reasons = channel_dominance_score(
        bidder_stats=bidder_stats,
        vertical=vertical,
        title=title,
        public_vs_gov_gap_pct=gap_pct,
        single_brand_concentration=opportunity.get("single_brand_concentration"),
        basket_in_vertical_catalog=basket_in_catalog,
    )
    has_evidence = int(bidder_stats.get("bidder_count") or 0) > 0
    klass = channel_competition_class(score, has_bidder_evidence=has_evidence)

    if override:
        if override.get("CHANNEL_COMPETITION_CLASS") in CHANNEL_CLASSES:
            klass = override["CHANNEL_COMPETITION_CLASS"]
            reasons.append("owner_override_class")
        if override.get("CHANNEL_DOMINANCE_SCORE") is not None:
            score = int(override["CHANNEL_DOMINANCE_SCORE"])
            reasons.append("owner_override_score")

    # Refresh roles with final class
    for row in bidder_stats.get("historical_bidders") or []:
        row["role"] = role_for_company(
            row.get("bidder_name") or "",
            vertical=vertical,
            channel_class=klass,
        )

    return {
        "opportunity_id": oid,
        "vertical": vertical,
        "CHANNEL_COMPETITION_CLASS": klass,
        "CHANNEL_DOMINANCE_SCORE": score,
        "channel_reasons": reasons,
        "bidder_structure": bidder_stats,
        "public_vs_gov_gap_pct": gap_pct,
        "owner_override_applied": bool(override),
        "provenance": bidder_stats.get("provenance"),
    }
