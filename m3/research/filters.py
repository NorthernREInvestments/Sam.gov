"""Combinable multi-select filters for contract discovery results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

VALUE_BANDS = {
    "under_5k": (0, 5_000),
    "5k_15k": (5_000, 15_000),
    "15k_50k": (15_000, 50_000),
    "50k_150k": (50_000, 150_000),
    "150k_500k": (150_000, 500_000),
    "500k_1m": (500_000, 1_000_000),
    "over_1m": (1_000_000, None),
    "unknown": None,
}

SET_ASIDE_OPTIONS = {
    "total_small_business",
    "partial_small_business",
    "8a",
    "hubzone",
    "sdvosb",
    "vosb",
    "wosb",
    "edwosb",
    "unrestricted",
    "state_local_preference",
    "other",
    "unknown",
}


def _mid_value(item: dict[str, Any]) -> float | None:
    if item.get("estimated_value_unknown") and item.get("estimated_value_min") is None:
        return None
    lo = item.get("estimated_value_min")
    hi = item.get("estimated_value_max")
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2.0
    return float(lo if lo is not None else hi)


def _in_band(val: float | None, band: str) -> bool:
    if band == "unknown":
        return val is None
    bounds = VALUE_BANDS.get(band)
    if bounds is None:
        return val is None
    lo, hi = bounds
    if val is None:
        return False
    if hi is None:
        return val >= lo
    return lo <= val < hi


def _normalize_set_aside(raw: str | None) -> str:
    s = (raw or "unknown").strip().lower()
    if not s or s in {"n/a", "none", "null"}:
        return "unknown"
    if "unrestricted" in s or s in {"open", "full and open"}:
        return "unrestricted"
    if "8(a)" in s or s == "8a" or "8(a)" in s:
        return "8a"
    if "hubzone" in s:
        return "hubzone"
    if "sdvosb" in s or "service-disabled" in s:
        return "sdvosb"
    if s == "vosb" or "veteran" in s and "disabled" not in s:
        return "vosb"
    if "edwosb" in s:
        return "edwosb"
    if "wosb" in s or "women" in s:
        return "wosb"
    if "small business" in s and "partial" in s:
        return "partial_small_business"
    if "small business" in s or s in {"sb", "total sb"}:
        return "total_small_business"
    if "preference" in s or "resident" in s:
        return "state_local_preference"
    if s == "unknown":
        return "unknown"
    return "other"


def apply_contract_filters(items: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Within-category OR, across-category AND."""
    from m3.research.vocabulary import parse_search_terms

    value_bands = list(filters.get("value_bands") or [])
    custom_min = filters.get("custom_value_min")
    custom_max = filters.get("custom_value_max")
    include_unknown_value = filters.get("include_unknown_value", True)
    set_asides = list(filters.get("set_asides") or [])
    states = [s.upper() for s in (filters.get("states") or [])]
    categories = [c.lower() for c in (filters.get("product_categories") or [])]
    buyer_types = [b.lower() for b in (filters.get("buyer_types") or [])]
    days_remaining_max = filters.get("days_remaining_max")
    search_mode = (filters.get("search_mode") or "broad").strip().lower()
    parsed = parse_search_terms(
        filters.get("keyword") or "",
        mode=search_mode,
        exclude=filters.get("exclude_keywords") or "",
    )
    include_terms = parsed["include_terms"]
    exclude_terms = parsed["exclude_terms"]
    # Display filter only — does not change underlying discovery population on the run
    product_resale_only = filters.get("product_resale_only", True)
    default_product_classes = [
        "product_supply",
        "product_with_incidental_services",
        "product_with_significant_installation",
        "product_rental_leasing",
        "mixed_product_service",
        "unknown_mixed",
    ]
    include_classes = list(filters.get("requirement_classes") or default_product_classes)
    non_product = {
        "services",
        "professional_services",
        "maintenance_repair_services",
        "construction",
        "software_only",
    }

    out = []
    now = datetime.now(timezone.utc)
    for item in items:
        req = (item.get("requirement_class") or "unknown_mixed").lower()
        if product_resale_only and req in non_product:
            continue
        if filters.get("requirement_classes") and req not in include_classes:
            continue
        if product_resale_only and not filters.get("requirement_classes") and req not in include_classes:
            continue
        # Exclude clearly non-active from default product view (status evidence)
        st = (item.get("solicitation_status") or "unknown").lower()
        if product_resale_only and st in {
            "closed",
            "closed_by_deadline",
            "cancelled",
            "historical_award",
            "forecast",
        }:
            continue

        val = _mid_value(item)
        # Value filter
        if value_bands or custom_min is not None or custom_max is not None:
            ok_val = False
            if value_bands:
                for b in value_bands:
                    if _in_band(val, b):
                        ok_val = True
                        break
            if custom_min is not None or custom_max is not None:
                if val is None:
                    ok_val = ok_val or bool(include_unknown_value)
                else:
                    if custom_min is not None and val < float(custom_min):
                        pass
                    elif custom_max is not None and val > float(custom_max):
                        pass
                    else:
                        ok_val = True
            if val is None and include_unknown_value and "unknown" in value_bands:
                ok_val = True
            if val is None and not include_unknown_value and "unknown" not in value_bands:
                ok_val = False
            if not ok_val and value_bands:
                continue
            if (custom_min is not None or custom_max is not None) and not ok_val and not value_bands:
                continue

        # Set-aside OR
        if set_asides:
            sa = _normalize_set_aside(item.get("set_aside"))
            if sa not in set_asides and not ("unknown" in set_asides and sa == "unknown"):
                # also allow 'other' bucket
                if "other" in set_asides and sa == "other":
                    pass
                else:
                    continue

        if states:
            st = (item.get("geo_state") or "").upper()
            if st not in states:
                continue

        if categories:
            cat = (item.get("product_category") or "").lower()
            if cat not in categories and "unknown" not in categories:
                continue

        title_l = str(item.get("title") or "").lower()
        desc_l = str(item.get("description") or "").lower()
        cat_l = str(item.get("product_category") or "").lower()
        line_l = str(item.get("line_items_text") or "").lower()
        mfr_l = str(item.get("manufacturer") or "").lower()
        mpn_l = str(item.get("part_number") or "").lower()
        doc_l = str(item.get("document_text") or "").lower()
        # document_text only counts when actually retrieved
        doc_searched = bool(item.get("document_text"))
        item["_document_searchable"] = doc_searched
        blob = " ".join(
            [
                title_l,
                desc_l,
                str(item.get("buyer_name") or "").lower(),
                str(item.get("solicitation_number") or "").lower(),
                cat_l,
                str(item.get("quantity") or "").lower(),
                line_l,
                mfr_l,
                mpn_l,
                doc_l if doc_searched else "",
            ]
        )

        match_reasons: list[str] = []
        if include_terms:
            if search_mode == "exact_mpn":
                needle = include_terms[0]
                if needle in mpn_l:
                    match_reasons.append("exact_part_number")
                elif needle in title_l or needle in blob:
                    match_reasons.append("exact_part_number_in_text")
                else:
                    continue
            elif search_mode == "exact_phrase":
                phrase = (filters.get("keyword") or "").strip().lower()
                if phrase and phrase not in blob:
                    continue
                if phrase in title_l:
                    match_reasons.append("title_match")
                elif phrase in desc_l:
                    match_reasons.append("description_match")
                elif phrase in cat_l:
                    match_reasons.append("category_match")
                elif phrase in line_l:
                    match_reasons.append("line_item_match")
                elif doc_searched and phrase in doc_l:
                    match_reasons.append("document_content_match")
                else:
                    match_reasons.append("text_match")
            else:
                # broad / multi-term: any include term matches (OR within keywords)
                hit_term = next((t for t in include_terms if t in blob), None)
                if not hit_term:
                    continue
                if hit_term in title_l:
                    match_reasons.append("title_match")
                elif hit_term in desc_l:
                    match_reasons.append("description_match")
                elif hit_term in cat_l:
                    match_reasons.append("category_match")
                elif hit_term in line_l:
                    match_reasons.append("line_item_match")
                elif doc_searched and hit_term in doc_l:
                    match_reasons.append("document_content_match")
                else:
                    match_reasons.append("text_match")
                match_reasons.append(f"term:{hit_term}")
        if exclude_terms and any(t in blob for t in exclude_terms):
            continue

        if days_remaining_max is not None and item.get("deadline_at"):
            try:
                raw = str(item["deadline_at"]).replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw[:19])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (dt - now).days
                if days < 0 or days > int(days_remaining_max):
                    continue
            except Exception:
                pass

        if buyer_types:
            # soft match on buyer name
            buyer = (item.get("buyer_name") or "").lower()
            if not any(b in buyer for b in buyer_types):
                continue

        if match_reasons:
            item = dict(item)
            item["match_reasons"] = match_reasons
        out.append(item)
    return sort_contracts_newest_first(out)


def _newest_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """ISO-ish strings for reverse chronological sort (posted → deadline → title)."""
    posted = (
        item.get("posted_at")
        or item.get("posted_date")
        or item.get("first_seen_at")
        or item.get("discovered_at")
        or ""
    )
    deadline = item.get("deadline_at") or item.get("response_deadline") or ""
    title = str(item.get("title") or item.get("solicitation_number") or "")
    return (str(posted)[:32], str(deadline)[:32], title.lower())


def sort_contracts_newest_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest published contracts first; unknown posted dates sink below dated ones."""
    dated: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    for it in items:
        posted = it.get("posted_at") or it.get("posted_date") or it.get("first_seen_at") or it.get("discovered_at")
        (dated if posted else undated).append(it)
    dated_sorted = sorted(dated, key=_newest_sort_key, reverse=True)
    # Undated: still prefer nearer/farther deadlines then title for stable UI
    undated_sorted = sorted(undated, key=_newest_sort_key, reverse=True)
    return dated_sorted + undated_sorted
