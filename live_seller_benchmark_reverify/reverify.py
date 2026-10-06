"""Independent contradiction reclassification + human-style verification notes."""

from __future__ import annotations

from collections import Counter
from typing import Any

from live_exact_priced_pdp.models import (
    DEAD_404,
    LIVE_EXACT_PDP_ACCESS_BLOCKED,
    LIVE_EXACT_PDP_LOGIN_REQUIRED,
    LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
    LIVE_EXACT_PDP_PRICE_HIDDEN,
    LIVE_EXACT_PDP_QUOTE_ONLY,
    LIVE_EXACT_PRICED_PDP,
    MARKETING_PAGE,
    WRONG_MANUFACTURER,
    WRONG_MPN,
    WRONG_PRODUCT,
)
from live_seller_benchmark_reverify.models import (
    AMBIGUOUS_REQUIRES_REVIEW,
    BENCHMARK_IDENTITY_WRONG,
    BENCHMARK_SOURCE_STALE,
    NO_CURRENT_PUBLIC_PRICE_CONFIRMED,
    PRODUCT_DISCONTINUED,
    PUBLIC_NEW_PRICE_REVERIFIED,
    PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT,
    QUOTE_ONLY_CONFIRMED,
    REMOVABLE_FROM_DENOM,
    RETAIN_IN_PUBLIC_DENOM,
)


_DISCONTINUED_MARKERS = (
    "discontinued",
    "no longer available",
    "no longer sold",
    "product retired",
    "obsolete",
)


def classify_contradiction_outcome(
    *,
    audits: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    item: dict[str, Any],
) -> dict[str, Any]:
    """Map live audit + extraction into Phase-4 contradiction outcome."""
    if prices:
        best = min(prices, key=lambda x: float(x.get("price") or 1e18))
        return {
            "outcome": PUBLIC_NEW_PRICE_REVERIFIED,
            "retain_in_denom": True,
            "remove_from_denom": False,
            "reason": "live_exact_public_price_extracted",
            "evidence": {
                "seller": best.get("domain"),
                "url": best.get("url"),
                "price": best.get("price"),
                "route": best.get("extraction_route"),
            },
        }

    states = [a.get("state") for a in audits if a.get("state")]
    counts = Counter(states)
    extractable = [a for a in audits if a.get("extractable")]
    quotes = [a for a in audits if a.get("state") == LIVE_EXACT_PDP_QUOTE_ONLY]
    login = [a for a in audits if a.get("state") == LIVE_EXACT_PDP_LOGIN_REQUIRED]
    blocked = [a for a in audits if a.get("state") == LIVE_EXACT_PDP_ACCESS_BLOCKED]
    wrong = [a for a in audits if a.get("state") in {WRONG_MPN, WRONG_PRODUCT, WRONG_MANUFACTURER}]
    dead = [a for a in audits if a.get("state") == DEAD_404]
    no_offer = [a for a in audits if a.get("state") == LIVE_EXACT_PDP_NO_PUBLIC_PRICE]
    marketing = [a for a in audits if a.get("state") == MARKETING_PAGE]

    # Access-difficult: extractable offer found but price not recovered, OR
    # blocked primary with structured offer evidence elsewhere without extract.
    if extractable:
        a = extractable[0]
        return {
            "outcome": PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT,
            "retain_in_denom": True,
            "remove_from_denom": False,
            "reason": "live_exact_offer_exists_extraction_failed",
            "evidence": {
                "seller": a.get("domain"),
                "url": a.get("url"),
                "state": a.get("state"),
                "offer_status": a.get("offer_status"),
            },
        }

    # Discontinued markers in titles/evidence
    for a in audits:
        title = str(((a.get("evidence") or {}).get("live") or {}).get("title") or "").lower()
        blob = title
        if any(m in blob for m in _DISCONTINUED_MARKERS):
            return {
                "outcome": PRODUCT_DISCONTINUED,
                "retain_in_denom": False,
                "remove_from_denom": True,
                "reason": "discontinued_marker_on_live_page",
                "evidence": {"url": a.get("url"), "title": title},
            }

    if quotes and not extractable:
        return {
            "outcome": QUOTE_ONLY_CONFIRMED,
            "retain_in_denom": False,
            "remove_from_denom": True,
            "reason": "live_exact_identity_quote_only",
            "evidence": {"urls": [q.get("url") for q in quotes[:3]], "domains": [q.get("domain") for q in quotes[:3]]},
        }

    if login and not extractable and not blocked:
        return {
            "outcome": PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT,
            "retain_in_denom": True,
            "remove_from_denom": False,
            "reason": "login_required_for_public_offer",
            "evidence": {"urls": [x.get("url") for x in login[:2]]},
        }

    # Wrong identity dominating + no live exact
    if wrong and len(wrong) >= max(2, len(audits) // 2) and not any(
        s in {LIVE_EXACT_PRICED_PDP, LIVE_EXACT_PDP_PRICE_HIDDEN, LIVE_EXACT_PDP_NO_PUBLIC_PRICE, LIVE_EXACT_PDP_QUOTE_ONLY}
        for s in states
    ):
        # Benchmark identity may be wrong if original seller also fails identity and no public alternate
        orig = str(item.get("original_benchmark_seller") or item.get("known_public_seller") or "")
        return {
            "outcome": BENCHMARK_IDENTITY_WRONG if orig and all(
                (a.get("domain") or "") == orig.replace("www.", "") or a.get("state") in {WRONG_MPN, WRONG_PRODUCT, WRONG_MANUFACTURER, DEAD_404}
                for a in audits[:3]
            )
            else NO_CURRENT_PUBLIC_PRICE_CONFIRMED,
            "retain_in_denom": False,
            "remove_from_denom": True,
            "reason": "no_exact_identity_public_pdp_found",
            "evidence": {
                "wrong_states": dict(counts),
                "sample_urls": [a.get("url") for a in wrong[:3]],
            },
        }

    # Stale benchmark: original URL dead + only soft/404/blocked
    orig_url = item.get("original_benchmark_url") or item.get("known_url_hint")
    if orig_url and dead and (len(dead) + len(blocked) + len(marketing)) >= max(2, len(audits) * 0.6) and not extractable:
        return {
            "outcome": BENCHMARK_SOURCE_STALE,
            "retain_in_denom": False,
            "remove_from_denom": True,
            "reason": "original_benchmark_url_dead_no_current_public_alternate",
            "evidence": {
                "original_url": orig_url,
                "dead": len(dead),
                "blocked": len(blocked),
                "states": dict(counts),
            },
        }

    if no_offer and not extractable:
        return {
            "outcome": NO_CURRENT_PUBLIC_PRICE_CONFIRMED,
            "retain_in_denom": False,
            "remove_from_denom": True,
            "reason": "exact_pdp_no_public_offer",
            "evidence": {"urls": [a.get("url") for a in no_offer[:3]]},
        }

    if (len(dead) + len(blocked) + len(wrong)) >= max(3, len(audits)) and not extractable:
        return {
            "outcome": NO_CURRENT_PUBLIC_PRICE_CONFIRMED,
            "retain_in_denom": False,
            "remove_from_denom": True,
            "reason": "exhaustive_live_search_no_public_priceable_pdp",
            "evidence": {"states": dict(counts), "n_audits": len(audits)},
        }

    if blocked and len(blocked) == len(audits):
        return {
            "outcome": PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT,
            "retain_in_denom": True,
            "remove_from_denom": False,
            "reason": "all_candidates_access_blocked_no_independent_proof",
            "evidence": {"domains": [a.get("domain") for a in blocked[:5]]},
        }

    return {
        "outcome": AMBIGUOUS_REQUIRES_REVIEW,
        "retain_in_denom": True,  # do not remove without positive evidence
        "remove_from_denom": False,
        "reason": "insufficient_positive_evidence_for_removal",
        "evidence": {"states": dict(counts), "n_audits": len(audits)},
    }


def human_check_record(
    item: dict[str, Any],
    *,
    queries: list[str],
    audits: list[dict[str, Any]],
    classification: dict[str, Any],
    price: dict[str, Any] | None,
) -> dict[str, Any]:
    best = None
    for a in audits:
        if a.get("extractable"):
            best = a
            break
        best = a
    return {
        "benchmark_id": item.get("benchmark_id"),
        "product": f"{item.get('manufacturer') or ''} {item.get('mpn') or item.get('benchmark_id')}".strip(),
        "queries_used": queries[:6],
        "seller_found": (best or {}).get("domain") or (price or {}).get("domain"),
        "live_pdp": (best or {}).get("url") or (price or {}).get("url"),
        "human_visible_price": (best or {}).get("human_visibility"),
        "structured_price": (best or {}).get("offer_status") in {
            "PUBLIC_PRICE_STRUCTURED",
            "PUBLIC_PRICE_JS_HIDDEN",
            "PUBLIC_PRICE_VISIBLE",
        },
        "public_offer": (best or {}).get("offer_status"),
        "final_benchmark_classification": classification.get("outcome"),
        "price": (price or {}).get("price"),
    }


def removal_allowed(outcome: str) -> bool:
    return outcome in REMOVABLE_FROM_DENOM


def retain_in_public(outcome: str) -> bool:
    return outcome in RETAIN_IN_PUBLIC_DENOM
