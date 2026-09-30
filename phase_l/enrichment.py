"""Phase L.2 enrichment orchestrator — adapters over existing M3 research.

Does NOT reimplement identity/history/market/economics engines.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.competition import annotate_competition, offer_bucket
from phase_l.economics import build_phase_l_economics, profit_tier
from phase_l.normalize import READY_FOR_OWNER_REVIEW, normalize_opportunity

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "phase_l2_enrichment_cache.json"
OUT = ROOT / "artifacts" / "phase_l"

# Identity research states (L.2 vocabulary mapped onto Phase J levels)
IDENTITY_EXACT = "IDENTITY_EXACT"
IDENTITY_STRONG = "IDENTITY_STRONG"
OR_EQUAL_RESEARCHABLE = "OR_EQUAL_RESEARCHABLE"
IDENTITY_PARTIAL = "IDENTITY_PARTIAL"
IDENTITY_INSUFFICIENT = "IDENTITY_INSUFFICIENT"

ENRICH_NOW = "ENRICH_NOW"
ENRICH_NEXT = "ENRICH_NEXT"
ENRICH_IF_CAPACITY = "ENRICH_IF_CAPACITY"
IDENTITY_NEEDED = "IDENTITY_NEEDED"

_OR_EQUAL_RE = re.compile(
    r"\bor[\s\-]?equal\b|\bequivalent\b|\bapproved\s+equal\b|\bcomparable\b|"
    r"\bbrand[\s\-]?name\s+or\s+equal\b",
    re.I,
)
_EXACT_ONLY_RE = re.compile(
    r"\bno\s+substitut|\bexact\s+(?:item|match|part)\s+required|"
    r"\bmust\s+be\s+(?:the\s+)?(?:exact|identical)|brand[\s\-]?name\s+only(?!\s+or\s+equal)",
    re.I,
)

CACHE_TTL_DAYS = 14


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_cache(path: Path | None = None) -> dict[str, Any]:
    path = path or CACHE_PATH
    if not path.exists():
        return {"kind": "PhaseL2EnrichmentCache", "by_key": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("by_key"), dict):
            return data
    except Exception:
        pass
    return {"kind": "PhaseL2EnrichmentCache", "by_key": {}, "updated_at": None}


def save_cache(cache: dict[str, Any], path: Path | None = None) -> Path:
    path = path or CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = dict(cache)
    cache["kind"] = "PhaseL2EnrichmentCache"
    cache["updated_at"] = _utc()
    path.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    return path


def cache_key(identity: dict[str, Any]) -> str | None:
    nsn = identity.get("nsn") or identity.get("normalized_nsn")
    if nsn:
        return f"nsn:{str(nsn).upper()}"
    mpn = identity.get("normalized_mpn") or identity.get("mpn")
    if mpn:
        return f"mpn:{str(mpn).upper()}"
    sku = identity.get("sku")
    if sku and str(sku).upper() not in {"UNKNOWN", ""}:
        return f"sku:{str(sku).upper()}"
    return None


def _cache_fresh(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict) or not entry.get("cached_at"):
        return False
    try:
        from datetime import datetime, timezone

        ts = datetime.fromisoformat(str(entry["cached_at"]).replace("Z", "+00:00"))
        age = (now_utc() - ts.astimezone(timezone.utc)).total_seconds()
        return age < CACHE_TTL_DAYS * 86400
    except Exception:
        return False


def _blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("title") or ""),
        str(row.get("description") or ""),
        str(row.get("solicitation_text") or ""),
    ]
    pi = row.get("product_identity")
    if isinstance(pi, dict):
        parts.append(str(pi.get("nsn") or ""))
        parts.append(str(pi.get("mpn") or ""))
    return "\n".join(parts)


def detect_or_equal(row: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    blob = text or _blob(row)
    exact_only = bool(_EXACT_ONLY_RE.search(blob))
    or_equal = bool(_OR_EQUAL_RE.search(blob)) and not exact_only
    return {
        "or_equal_allowed": or_equal,
        "exact_item_required": exact_only or (not or_equal and bool(re.search(r"\bNSN\b|\bP/?N\b", blob))),
        "substitution_policy": (
            "EXACT_ONLY" if exact_only else "OR_EQUAL" if or_equal else "UNSPECIFIED"
        ),
    }


def resolve_quantity_uom(row: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    """Reuse sam_live_fallback + phase_j parsers — never invent qty."""
    if row.get("quantity") is not None:
        try:
            qty = float(row["quantity"])
            uom = str(row.get("uom") or "EA").upper()
            if uom == "EACH":
                uom = "EA"
            return {
                "quantity": qty,
                "uom": uom,
                "quantity_source": "row",
                "quantity_known": True,
            }
        except (TypeError, ValueError):
            pass

    blob = text or _blob(row)
    try:
        from sam_live_fallback import parse_qty_uom_from_text

        parsed = parse_qty_uom_from_text(blob)
        if parsed.get("quantity") is not None:
            return {
                "quantity": float(parsed["quantity"]),
                "uom": str(parsed.get("uom") or "EA").upper(),
                "quantity_source": "sam_live_fallback",
                "quantity_known": True,
                "provenance": parsed.get("provenance"),
            }
    except Exception:
        pass

    try:
        from phase_j.history_reconciliation import parse_qty_uom

        parsed = parse_qty_uom(blob)
        if parsed.get("quantity") is not None:
            return {
                "quantity": float(parsed["quantity"]),
                "uom": str(parsed.get("uom") or "EA").upper(),
                "quantity_source": "phase_j.parse_qty_uom",
                "quantity_known": True,
            }
    except Exception:
        pass

    return {
        "quantity": None,
        "uom": row.get("uom"),
        "quantity_source": None,
        "quantity_known": False,
        "quantity_state": "UNKNOWN",
    }


def stage_a_identity(row: dict[str, Any], *, text: str | None = None) -> dict[str, Any]:
    """Cheap identity screen — Phase J canonical extractor."""
    from phase_j.product_identity import (
        LEVEL_EXACT,
        LEVEL_PARTIAL,
        LEVEL_STRONG,
        LEVEL_UNKNOWN,
        build_product_identity,
    )

    blob = text or _blob(row)
    identity = build_product_identity(row, text=blob)
    sub = detect_or_equal(row, blob)
    qty = resolve_quantity_uom(row, blob)

    level = identity.get("identity_level")
    if level == LEVEL_EXACT:
        research_state = IDENTITY_EXACT
    elif level == LEVEL_STRONG:
        research_state = IDENTITY_STRONG
    elif level == LEVEL_PARTIAL and sub.get("or_equal_allowed"):
        research_state = OR_EQUAL_RESEARCHABLE
    elif level == LEVEL_PARTIAL:
        research_state = IDENTITY_PARTIAL
    else:
        research_state = IDENTITY_INSUFFICIENT

    # Map to Phase L identity_match_type vocabulary
    match_type = {
        LEVEL_EXACT: "EXACT",
        LEVEL_STRONG: "EXACT" if identity.get("nsn") else "PARTIAL",
        LEVEL_PARTIAL: "OR_EQUAL_MATCH" if sub.get("or_equal_allowed") else "PARTIAL",
        LEVEL_UNKNOWN: "UNKNOWN",
    }.get(level, "UNKNOWN")
    if identity.get("nsn") and row.get("approved_source"):
        match_type = "APPROVED_SOURCE_EXACT"

    return {
        "kind": "PhaseL2IdentityScreen",
        "identity": identity,
        "identity_research_state": research_state,
        "identity_match_type": match_type,
        "identity_confidence": identity.get("identity_confidence"),
        "identity_source": (identity.get("provenance") or [{"source": "phase_j"}])[0].get("source")
        if identity.get("provenance")
        else "phase_j.product_identity",
        "or_equal_allowed": sub.get("or_equal_allowed"),
        "exact_item_required": sub.get("exact_item_required"),
        "substitution_policy": sub.get("substitution_policy"),
        "quantity": qty.get("quantity"),
        "uom": qty.get("uom"),
        "quantity_known": qty.get("quantity_known"),
        "quantity_source": qty.get("quantity_source"),
        "researchable": research_state
        in {IDENTITY_EXACT, IDENTITY_STRONG, OR_EQUAL_RESEARCHABLE},
        "screened_at": _utc(),
    }


def enrichment_priority(row: dict[str, Any], screen: dict[str, Any]) -> dict[str, Any]:
    """Cheap priority — not final opportunity score."""
    score = 0
    reasons: list[str] = []
    state = screen.get("identity_research_state")
    if state == IDENTITY_EXACT:
        score += 40
        reasons.append("exact_identity")
    elif state == IDENTITY_STRONG:
        score += 30
        reasons.append("strong_identity")
    elif state == OR_EQUAL_RESEARCHABLE:
        score += 20
        reasons.append("or_equal_researchable")
    elif state == IDENTITY_PARTIAL:
        score += 8
        reasons.append("partial_identity")
    else:
        return {
            "enrichment_priority": IDENTITY_NEEDED,
            "priority_score": 0,
            "priority_reasons": ["identity_insufficient"],
        }

    if screen.get("quantity_known"):
        score += 15
        reasons.append("qty_known")
        qty = screen.get("quantity") or 0
        if qty >= 50:
            score += 10
            reasons.append("qty_ge_50")
        elif qty >= 10:
            score += 5
            reasons.append("qty_ge_10")

    access_type = str(row.get("competition_access_type") or "")
    if access_type in {"OPEN_MARKET", "UNRESTRICTED", "TOTAL_SMALL_BUSINESS"}:
        score += 15
        reasons.append("open_competition")

    runway = row.get("runway_days")
    try:
        if runway is not None and float(runway) >= 7:
            score += 5
            reasons.append("runway_ok")
    except (TypeError, ValueError):
        pass

    level = str(row.get("source_level") or "")
    if level == "FEDERAL" and screen.get("identity", {}).get("nsn"):
        score += 10
        reasons.append("federal_nsn")

    if score >= 55:
        bucket = ENRICH_NOW
    elif score >= 35:
        bucket = ENRICH_NEXT
    else:
        bucket = ENRICH_IF_CAPACITY

    return {
        "enrichment_priority": bucket,
        "priority_score": score,
        "priority_reasons": reasons,
    }


def lookup_government_history(
    row: dict[str, Any],
    identity: dict[str, Any],
    *,
    budget: dict[str, int],
    cache: dict[str, Any],
    authorize_live: bool = True,
) -> dict[str, Any]:
    """USAspending + Phase J reconciliation. State/local may mark UNAVAILABLE."""
    key = cache_key(identity)
    by_key = cache.setdefault("by_key", {})
    if key and _cache_fresh(by_key.get(key, {}).get("history")):
        hit = dict(by_key[key]["history"])
        hit["cache_hit"] = True
        return hit

    source_level = str(row.get("source_level") or "").upper()
    out: dict[str, Any] = {
        "kind": "PhaseL2HistoryResearch",
        "history_research_state": "HISTORY_PENDING",
        "attempted": False,
        "historical_award_unit_price": None,
        "historical_award_price": None,
        "historical_offers_received": None,
        "historical_competition_type": row.get("competition_access_type"),
        "historical_identity_match_type": None,
        "history_confidence": "NO_USABLE_HISTORY",
        "source_type": None,
        "source_url": "https://api.usaspending.gov/",
        "retrieved_at": _utc(),
        "cache_hit": False,
    }

    if not authorize_live:
        out["history_research_state"] = "HISTORY_NOT_FOUND"
        out["skipped"] = "live_disabled"
        return out

    from phase_j.history_reconciliation import reconcile_awards
    from phase_l.convergence import (
        expanded_history_keywords,
        history_query_approaches,
        map_history_confidence,
    )

    keywords = expanded_history_keywords(identity, row)
    if not keywords:
        out["history_research_state"] = "HISTORY_NOT_FOUND"
        out["reason"] = "no_nsn_or_mpn_identity_key"
        if source_level in {"STATE", "LOCAL", "COOPERATIVE", "NETWORK"}:
            out["history_research_state"] = "HISTORY_SOURCE_UNAVAILABLE"
            out["reason"] = "no_federal_identity_key_for_usaspending"
        return out

    if budget.get("usaspending", 0) >= budget.get("usaspending_max", 80):
        out["history_research_state"] = "HISTORY_PENDING"
        out["skipped"] = "budget"
        return out

    approaches = history_query_approaches(keywords)
    out["query_approaches"] = approaches
    out["keywords"] = keywords[:8]
    awards: list[dict[str, Any]] = []
    approach_logs: list[dict[str, Any]] = []

    try:
        from usaspending_client import fetch_awards_by_keywords

        for i, batch in enumerate(approaches[:3]):
            if budget.get("usaspending", 0) >= budget.get("usaspending_max", 80):
                break
            if not batch:
                continue
            try:
                batch_awards = fetch_awards_by_keywords(batch[:2], limit=10) or []
                budget["usaspending"] = budget.get("usaspending", 0) + 1
                out["attempted"] = True
                approach_logs.append(
                    {"approach": i + 1, "keywords": batch[:2], "award_count": len(batch_awards)}
                )
                for a in batch_awards:
                    # dedupe by award id
                    aid = a.get("generated_internal_id") or a.get("Award ID") or a.get("award_id")
                    if aid and any(
                        (x.get("generated_internal_id") or x.get("Award ID") or x.get("award_id")) == aid
                        for x in awards
                    ):
                        continue
                    awards.append(a)
                if awards and i >= 2:
                    break
            except Exception as exc:
                budget["usaspending"] = budget.get("usaspending", 0) + 1
                approach_logs.append({"approach": i + 1, "keywords": batch[:2], "error": str(exc)[:120]})
        out["LIVE_API_REQUESTS"] = len(approach_logs)
        out["approach_logs"] = approach_logs
        out["raw_award_count"] = len(awards)
    except Exception as exc:
        budget["usaspending"] = budget.get("usaspending", 0) + 1
        out["attempted"] = True
        out["error"] = str(exc)[:200]
        out["history_research_state"] = "HISTORY_NOT_FOUND"
        return out

    recon = reconcile_awards(
        {
            "nsn": identity.get("nsn"),
            "mpn": identity.get("mpn"),
            "normalized_nsn": identity.get("normalized_nsn"),
            "normalized_mpn": identity.get("normalized_mpn"),
            "title": row.get("title"),
            "manufacturer": identity.get("manufacturer"),
            "model": identity.get("model"),
        },
        awards or [],
        solicitation_uom=row.get("uom"),
    )
    out["reconciliation"] = {
        "history_class": recon.get("history_class"),
        "usable_count": recon.get("usable_count"),
        "rejected_count": recon.get("rejected_count"),
        "price_stats": recon.get("price_stats"),
        "revenue": recon.get("revenue"),
        "revenue_basis": recon.get("revenue_basis"),
    }
    out["source_type"] = "USASPENDING"

    stats = recon.get("price_stats") or {}
    unit_prices = stats.get("unit_prices") or []
    usable = [
        r
        for r in (recon.get("reconciliations") or [])
        if r.get("drives_economics")
    ]

    if unit_prices:
        out["historical_award_unit_price"] = float(stats.get("unit_price_most_recent") or unit_prices[0])
        out["historical_unit_price_median"] = float(stats.get("unit_price_median") or statistics.median(unit_prices))
        out["historical_unit_price_low"] = float(stats.get("unit_price_low") or min(unit_prices))
        out["historical_unit_price_high"] = float(stats.get("unit_price_high") or max(unit_prices))
        out["history_research_state"] = "HISTORY_FOUND"
        cls = recon.get("history_class")
        out["history_confidence"] = (
            "EXACT_RECENT"
            if cls == "STRONG_HISTORY"
            else "EXACT_OLDER"
            if cls == "MODERATE_HISTORY"
            else "STRONG_COMPARABLE"
            if cls == "WEAK_HISTORY"
            else "WEAK_COMPARABLE"
        )
        out["history_confidence_l24"] = map_history_confidence(out)
    elif recon.get("revenue") is not None and row.get("quantity"):
        # Lot total only — derive unit if qty known
        try:
            out["historical_award_price"] = float(recon["revenue"])
            out["historical_award_unit_price"] = round(
                float(recon["revenue"]) / float(row["quantity"]), 4
            )
            out["history_research_state"] = "HISTORY_FOUND"
            out["history_confidence"] = "WEAK_COMPARABLE"
            out["expected_bid_basis"] = recon.get("revenue_basis")
        except (TypeError, ValueError, ZeroDivisionError):
            out["history_research_state"] = "HISTORY_NOT_FOUND"
    else:
        out["history_research_state"] = (
            "HISTORY_NOT_FOUND" if out.get("raw_award_count", 0) == 0 else "HISTORY_NOT_FOUND"
        )
        if source_level in {"STATE", "LOCAL"} and not awards:
            out["history_research_state"] = "HISTORY_SOURCE_UNAVAILABLE"

    # Offer count from first usable award if present
    for r in usable:
        award = r.get("award") or {}
        offers = (
            award.get("number_of_offers_received")
            or award.get("offers_received")
            or r.get("offers_received")
        )
        if offers is not None:
            try:
                out["historical_offers_received"] = int(offers)
                break
            except (TypeError, ValueError):
                continue
    if out.get("historical_offers_received") is None:
        for a in awards or []:
            offers = a.get("number_of_offers_received") or a.get("offers_received")
            if offers is not None:
                try:
                    out["historical_offers_received"] = int(offers)
                    break
                except (TypeError, ValueError):
                    continue

    if out.get("historical_offers_received") is None and awards:
        # Light detail backfill for offer count (existing usaspending helper)
        try:
            from usaspending_client import backfill_predecessor_offer_count, fetch_award_detail

            for a in (awards or [])[:3]:
                gid = a.get("generated_internal_id") or a.get("internal_id")
                if not gid:
                    continue
                detail = fetch_award_detail(gid)
                if not detail:
                    continue
                offers = detail.get("number_of_offers_received") or detail.get("latest_transaction", {}).get(
                    "number_of_offers_received"
                )
                if offers is None and callable(backfill_predecessor_offer_count):
                    try:
                        summary = {"generated_internal_id": gid}
                        backfill_predecessor_offer_count(summary)
                        offers = summary.get("number_of_offers_received")
                    except Exception:
                        offers = None
                if offers is not None:
                    try:
                        out["historical_offers_received"] = int(offers)
                        out["offer_count_source"] = "usaspending_award_detail"
                        break
                    except (TypeError, ValueError):
                        continue
        except Exception as exc:
            out["offer_backfill_error"] = str(exc)[:160]

    if usable:
        out["historical_identity_match_type"] = usable[0].get("final_reconciliation_result")
        out["historical_award_id"] = (usable[0].get("award") or {}).get("award_id") or (
            usable[0].get("award") or {}
        ).get("Award ID")
        out["historical_awardee"] = (usable[0].get("award") or {}).get("recipient_name") or (
            usable[0].get("award") or {}
        ).get("Recipient Name")

    if key and out.get("attempted") and out.get("history_research_state") == "HISTORY_FOUND":
        by_key.setdefault(key, {})["history"] = {**out, "cached_at": _utc()}

    return out


def select_market_price(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer exact + reputable; do not blindly pick lowest suspicious listing."""
    if not observations:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for o in observations:
        price = o.get("unit_price") if o.get("unit_price") is not None else o.get("Observed_price")
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        score = 0.0
        conf = str(o.get("confidence") or o.get("Evidence_confidence") or o.get("Match_confidence") or "").upper()
        if conf in {"HIGH", "STRONG", "EXACT"}:
            score += 50
        elif conf in {"MEDIUM", "MODERATE"}:
            score += 25
        seller = str(o.get("seller") or o.get("Seller") or o.get("Seller_name") or "").lower()
        # Deprioritize marketplace scrapes that look sketchy
        if any(x in seller for x in ("ebay", "craigslist", "facebook", "alibaba")):
            score -= 40
        if o.get("exact_match") in {"EXACT", "STRONG", True} or str(o.get("exact_match") or "").upper() in {
            "HIGH",
            "STRONG",
            "EXACT",
            "MATCH_STRONG",
        }:
            score += 30
        url = str(o.get("url") or o.get("URL") or o.get("source_url") or "")
        if any(d in url.lower() for d in (".gov", "grainger", "mscdirect", "digikey", "mouser", "mcmaster")):
            score += 20
        # Mild preference for mid-pack over extreme low outliers
        score -= abs(p) * 0.00001
        scored.append((score, {**o, "unit_price": p}))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _bing_result_urls(query: str, *, limit: int = 8) -> list[str]:
    """Fallback when DuckDuckGo HTML is 403. Free GET — no outreach."""
    try:
        import httpx
        from urllib.parse import quote_plus

        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        r = httpx.get(
            url,
            timeout=25.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        if r.status_code >= 400:
            return []
        text = r.text or ""
        found: list[str] = []
        # Prefer visible cite hosts; reconstruct https URLs
        for m in re.finditer(r"<cite[^>]*>([^<]+)</cite>", text, re.I):
            raw = re.sub(r"\s+", "", m.group(1))
            raw = raw.replace("›", "/").strip()
            if not raw or "bing." in raw or "microsoft." in raw:
                continue
            if not raw.startswith("http"):
                raw = "https://" + raw.lstrip("/")
            # cite often truncates with …
            raw = raw.split("…")[0].split("...")[0]
            if raw.startswith("http") and raw not in found:
                found.append(raw)
            if len(found) >= limit:
                break
        return found
    except Exception:
        return []


def _fetch_nsn_catalog_prices(nsn: str) -> list[dict[str, Any]]:
    """Direct public NSN dealer pages when search engines are blocked.

    Reuses m3_public_pricing_evidence.fetch_public_text + extract_price_observations.
    Also tries Bing cite → page fetch for NSN buy/price queries.
    """
    from m3_public_pricing_evidence import ACCESS_OK, CostLedger, extract_price_observations, fetch_public_text

    nsn = str(nsn or "").strip()
    if not nsn:
        return []
    compact = nsn.replace("-", "")
    urls = [
        f"https://www.iso-group.com/NSN/{nsn}",
        f"https://www.iso-group.com/NSN/{compact}",
    ]
    # Bing fallback URLs (DDG often AUTH_REQUIRED)
    for u in _bing_result_urls(f"NSN {nsn} price distributor buy", limit=6):
        if u not in urls:
            urls.append(u)

    ledger = CostLedger()
    access_log: list[dict[str, Any]] = []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for u in urls:
        text, state = fetch_public_text(u, ledger, access_log, seen=seen)
        if state != ACCESS_OK or not text:
            continue
        for o in extract_price_observations(text, source_url=u, product_hint=nsn):
            amt = o.get("Observed_price")
            try:
                price = float(amt)
            except (TypeError, ValueError):
                continue
            if price <= 0 or price > 5_000_000:
                continue
            # Skip page-level aggregate award totals that aren't unit prices
            if price > 100_000 and "iso-group.com" in u:
                continue
            seller = "catalog"
            if "iso-group" in u:
                seller = "ISO Group"
            elif "amazon." in u:
                seller = "Amazon"
            elif "grainger" in u:
                seller = "Grainger"
            out.append(
                {
                    "unit_price": price,
                    "seller": seller,
                    "url": u,
                    "confidence": "MEDIUM",
                    "exact_match": "NSN",
                    "price_type": "RETAIL",
                    "source_type": "nsn_catalog_or_search",
                    "checked_at": _utc(),
                }
            )
        if out:
            break
    return out


def lookup_current_market(
    row: dict[str, Any],
    identity: dict[str, Any],
    screen: dict[str, Any],
    *,
    budget: dict[str, int],
    cache: dict[str, Any],
    authorize_live: bool = True,
) -> dict[str, Any]:
    """L.2.1: MPN-first public market research via phase_l.market_price."""
    key = cache_key(identity)
    by_key = cache.setdefault("by_key", {})
    if key and _cache_fresh(by_key.get(key, {}).get("market")):
        hit = dict(by_key[key]["market"])
        hit["cache_hit"] = True
        return hit

    from phase_l.market_price import build_search_identity, research_public_market_price

    search_probe = build_search_identity(identity, row)
    has_commercial_key = bool(
        search_probe.get("primary_mpn")
        or search_probe.get("sku")
        or search_probe.get("nsn")
        or (search_probe.get("model") and search_probe.get("manufacturer"))
    )

    if screen.get("exact_item_required") and screen.get("identity_research_state") == IDENTITY_INSUFFICIENT:
        return {
            "kind": "PhaseL2MarketResearch",
            "market_research_state": "MARKET_PRICE_PENDING",
            "market_price_confidence": "IDENTITY_INSUFFICIENT",
            "attempted": False,
            "reason": "exact_item_required_identity_insufficient",
            "public_retail_unit_price": None,
            "observations": [],
            "cache_hit": False,
        }

    if not screen.get("researchable") and not screen.get("or_equal_allowed") and not has_commercial_key:
        # L.2.3: allow when commercial overlay marked market_research_eligible
        if not screen.get("market_research_eligible") and not screen.get("l23_researchable"):
            return {
                "kind": "PhaseL2MarketResearch",
                "market_research_state": "MARKET_PRICE_PENDING",
                "market_price_confidence": "IDENTITY_INSUFFICIENT",
                "attempted": False,
                "reason": "identity_not_researchable",
                "public_retail_unit_price": None,
                "observations": [],
                "cache_hit": False,
            }

    result = research_public_market_price(
        row,
        identity,
        budget=budget,
        max_pages=12,
        authorize_live=authorize_live,
    )
    out = {
        "kind": "PhaseL2MarketResearch",
        "attempted": bool(result.get("attempted")),
        "cache_hit": False,
        "market_research_state": result.get("market_research_state"),
        "market_price_confidence": result.get("market_price_confidence"),
        "public_retail_unit_price": result.get("public_retail_unit_price"),
        "public_retail_source": result.get("public_retail_source"),
        "market_price_low": result.get("market_price_low"),
        "market_price_median": result.get("market_price_median"),
        "market_price_high": result.get("market_price_high"),
        "market_price_selected": result.get("market_price_selected"),
        "selection_reason": result.get("selection_reason"),
        "observations": result.get("observations") or [],
        "selected_observation": result.get("selected_observation"),
        "mpn_query_attempted": result.get("mpn_query_attempted"),
        "provider_attempts": result.get("provider_attempts"),
        "rfq_only_pages": result.get("rfq_only_pages"),
        "retrieved_at": result.get("retrieved_at"),
        "l21": True,
        "l22": True,
        "l23": True,
        "economics_eligible": result.get("economics_eligible"),
        "l22_confidence": result.get("l22_confidence"),
        "evidence": result.get("evidence") or [],
        "verified_evidence": result.get("verified_evidence") or [],
        "pages_fetched": result.get("pages_fetched"),
        "product_pages_fetched": result.get("product_pages_fetched"),
        "search_pages_seen": result.get("search_pages_seen"),
        "search_results_found": result.get("search_results_found"),
        "candidate_links_extracted": result.get("candidate_links_extracted"),
        "exact_identity_pages": result.get("exact_identity_pages"),
        "strong_identity_pages": result.get("strong_identity_pages"),
        "rejected_mismatches": result.get("rejected_mismatches"),
        "drop_reason": result.get("drop_reason"),
        "drop_reason_counts": result.get("drop_reason_counts"),
    }
    if key and out.get("market_research_state") == "MARKET_PRICE_FOUND":
        by_key.setdefault(key, {})["market"] = {**out, "cached_at": _utc()}
    return out



def apply_enrichment_to_row(
    row: dict[str, Any],
    *,
    screen: dict[str, Any],
    priority: dict[str, Any],
    history: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge enrichment into row fields Phase L economics already understands."""
    enriched = dict(row)
    identity = screen.get("identity") or {}

    if screen.get("quantity") is not None:
        enriched["quantity"] = screen["quantity"]
    if screen.get("uom"):
        enriched["uom"] = screen["uom"]
    if identity.get("nsn"):
        enriched["nsn"] = identity["nsn"]
    if identity.get("mpn"):
        enriched["mpn"] = identity["mpn"]

    enriched["identity_match_type"] = screen.get("identity_match_type")
    enriched["identity_confidence"] = screen.get("identity_confidence")
    enriched["or_equal_allowed"] = screen.get("or_equal_allowed")
    enriched["enrichment_priority"] = priority.get("enrichment_priority")
    enriched["identity_research_state"] = screen.get("identity_research_state")
    enriched["product_identity"] = {
        **(enriched.get("product_identity") or {}),
        "nsn": identity.get("nsn"),
        "mpn": identity.get("mpn"),
        "manufacturer": identity.get("manufacturer"),
        "model": identity.get("model"),
        "title": enriched.get("title"),
    }

    history = history or {}
    market = market or {}

    if history.get("historical_award_unit_price") is not None:
        enriched["historical_award_unit_price"] = history["historical_award_unit_price"]
    if history.get("historical_award_price") is not None:
        enriched["historical_award_price"] = history["historical_award_price"]
    if history.get("historical_offers_received") is not None:
        enriched["historical_offers_received"] = history["historical_offers_received"]
        enriched["offer_count"] = history["historical_offers_received"]
    if history.get("historical_competition_type"):
        enriched["historical_competition_type"] = history["historical_competition_type"]

    if market.get("public_retail_unit_price") is not None and market.get("economics_eligible", True):
        enriched["public_retail_unit_price"] = market["public_retail_unit_price"]
        enriched["public_retail_price"] = market["public_retail_unit_price"]
        enriched["public_retail_source"] = market.get("public_retail_source")
        enriched["l22_confidence"] = market.get("l22_confidence")
        enriched["market_price_evidence"] = market.get("verified_evidence") or market.get("evidence")
    elif market.get("public_retail_unit_price") is not None and market.get("economics_eligible") is False:
        # Research-only price — do not feed economics
        enriched["market_price_research_only"] = market.get("public_retail_unit_price")
        enriched["l22_confidence"] = market.get("l22_confidence")
        enriched["market_drop_reason"] = market.get("drop_reason") or "NOT_ECONOMICS_ELIGIBLE"

    enriched["history_research_state"] = history.get("history_research_state")
    enriched["market_research_state"] = market.get("market_research_state")
    enriched["history_confidence"] = history.get("history_confidence")
    enriched["market_price_confidence"] = market.get("market_price_confidence")
    enriched["enrichment"] = {
        "screen": {
            k: screen.get(k)
            for k in (
                "identity_research_state",
                "identity_match_type",
                "quantity_known",
                "or_equal_allowed",
                "researchable",
            )
        },
        "priority": priority,
        "history": {
            k: history.get(k)
            for k in (
                "history_research_state",
                "history_confidence",
                "historical_award_unit_price",
                "historical_offers_received",
                "attempted",
                "cache_hit",
                "reason",
            )
        },
        "market": {
            k: market.get(k)
            for k in (
                "market_research_state",
                "market_price_confidence",
                "public_retail_unit_price",
                "market_price_low",
                "market_price_median",
                "market_price_high",
                "selection_reason",
                "attempted",
                "cache_hit",
                "reason",
            )
        },
    }
    return enriched


def finalize_economics(row: dict[str, Any]) -> dict[str, Any]:
    """Run Phase L economics when qty + revenue + retail exist. Always attach unit-spread research."""
    from phase_l.commercial_identity import (
        PROMISING_UNIT_ECONOMICS,
        QUANTITY_REQUIRED,
        TOTAL_PROFIT_UNKNOWN_QUANTITY,
        compute_unit_spread,
    )

    qty = row.get("quantity")
    hist_u = row.get("historical_award_unit_price")
    retail_u = row.get("public_retail_unit_price") or row.get("public_retail_price")

    unit = compute_unit_spread(
        historical_unit_price=hist_u if isinstance(hist_u, (int, float)) else None,
        public_retail_unit_price=retail_u if isinstance(retail_u, (int, float)) else None,
        quantity=qty if isinstance(qty, (int, float)) else None,
    )

    if qty is None:
        out = {
            "economics_completed": False,
            "blocker": "UNKNOWN_QUANTITY",
            "expected_net_profit": None,
            "profit_tier": None,
            "meets_floor": False,
            "ready_to_bid": False,
            "unit_economics": unit,
            "total_status": unit.get("total_status") or TOTAL_PROFIT_UNKNOWN_QUANTITY,
            "research_signal": unit.get("research_signal")
            or (f"{PROMISING_UNIT_ECONOMICS} / {QUANTITY_REQUIRED}" if unit.get("unit_raw_spread") and unit["unit_raw_spread"] > 0 else QUANTITY_REQUIRED if retail_u and hist_u else None),
        }
        return out
    if hist_u is None and row.get("expected_bid_unit_price") is None:
        return {
            "economics_completed": False,
            "blocker": "UNKNOWN_HISTORICAL_PRICE",
            "expected_net_profit": None,
            "historical_award_unit_price": None,
            "profit_tier": None,
            "meets_floor": False,
            "ready_to_bid": False,
            "unit_economics": unit,
        }
    if retail_u is None:
        return {
            "economics_completed": False,
            "blocker": "UNKNOWN_MARKET_PRICE",
            "expected_net_profit": None,
            "public_retail_unit_price": None,
            "profit_tier": None,
            "meets_floor": False,
            "ready_to_bid": False,
            "unit_economics": unit,
        }

    econ = build_phase_l_economics(
        quantity=qty,
        uom=row.get("uom"),
        historical_unit_price=hist_u,
        expected_bid_unit_price=row.get("expected_bid_unit_price"),
        public_retail_unit_price=retail_u,
        public_retail_source=row.get("public_retail_source"),
        freight=row.get("freight"),
    )
    econ["economics_completed"] = econ.get("expected_net_profit") is not None and not (
        econ.get("blocker") in {"MISSING_REVENUE_BASIS", "MISSING_PUBLIC_RETAIL"}
    )
    econ["ready_to_bid"] = False
    econ["unit_economics"] = unit
    if econ.get("expected_net_profit") is not None and float(econ["expected_net_profit"]) > 0:
        from phase_l.commercial_identity import EXPECTED_NET_GE_10K, EXPECTED_NET_POSITIVE

        signals = list(unit.get("signals") or [])
        signals.append(EXPECTED_NET_POSITIVE)
        if float(econ["expected_net_profit"]) >= 10000:
            signals.append(EXPECTED_NET_GE_10K)
        econ["profit_signals"] = signals
    return econ


def enrich_opportunity(
    row: dict[str, Any],
    *,
    budget: dict[str, int],
    cache: dict[str, Any],
    authorize_live: bool = True,
    deep: bool = True,
) -> dict[str, Any]:
    """Full enrichment path for one accessible opportunity."""
    screen = stage_a_identity(row)
    priority = enrichment_priority(row, screen)
    history = None
    market = None

    do_deep = deep and priority.get("enrichment_priority") in {
        ENRICH_NOW,
        ENRICH_NEXT,
        ENRICH_IF_CAPACITY,
    }
    if do_deep and screen.get("researchable"):
        history = lookup_government_history(
            row,
            screen.get("identity") or {},
            budget=budget,
            cache=cache,
            authorize_live=authorize_live,
        )
        market = lookup_current_market(
            row,
            screen.get("identity") or {},
            screen,
            budget=budget,
            cache=cache,
            authorize_live=authorize_live,
        )

    enriched = apply_enrichment_to_row(
        row, screen=screen, priority=priority, history=history, market=market
    )
    economics = finalize_economics(enriched) if do_deep else {
        "economics_completed": False,
        "blocker": "DEEP_NOT_RUN",
        "expected_net_profit": None,
        "meets_floor": False,
    }

    comp = annotate_competition(
        historical_offers_received=enriched.get("historical_offers_received")
        or enriched.get("offer_count"),
        historical_competition_type=enriched.get("historical_competition_type")
        or enriched.get("competition_access_type"),
        competition_access_type=enriched.get("competition_access_type"),
    )
    enriched["competition"] = comp
    enriched["effective_competition_signal"] = comp.get("effective_competition_signal")

    # Preserve access from L.1
    access = enriched.get("access") if isinstance(enriched.get("access"), dict) else {
        "our_bid_access": enriched.get("our_bid_access"),
        "competition_access_type": enriched.get("competition_access_type"),
        "registration_action": enriched.get("registration_action"),
        "is_easy_registration": enriched.get("is_easy_registration"),
        "vendor_registration_required": (
            (enriched.get("access") or {}).get("vendor_registration_required")
            if isinstance(enriched.get("access"), dict)
            else None
        ),
    }

    norm = normalize_opportunity(
        enriched,
        access=access if access.get("our_bid_access") else None,
        economics=economics if economics.get("economics_completed") else None,
        competition=comp,
        runway_days=enriched.get("runway_days"),
    )
    # Attach L.2 fields onto normalized
    for k in (
        "enrichment_priority",
        "identity_research_state",
        "history_research_state",
        "market_research_state",
        "history_confidence",
        "market_price_confidence",
        "or_equal_allowed",
        "enrichment",
        "registration_action",
        "is_easy_registration",
        "can_compete",
    ):
        if enriched.get(k) is not None:
            norm[k] = enriched.get(k)
    norm["economics_completed"] = bool(economics.get("economics_completed"))
    if economics.get("blocker") and not economics.get("economics_completed"):
        norm["enrichment_blocker"] = economics.get("blocker")
    return norm


def run_phase_l2_enrichment(
    rows: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    usaspending_max: int = 80,
    market_max: int = 60,
    deep_limit: int | None = 120,
) -> dict[str, Any]:
    """Stage-A all rows; deep enrich prioritized researchable subset."""
    cache = load_cache()
    budget = {
        "usaspending": 0,
        "usaspending_max": usaspending_max,
        "market": 0,
        "market_max": market_max,
    }

    funnel = {
        "FEDERAL": Counter(),
        "STATE": Counter(),
        "LOCAL": Counter(),
        "COOPERATIVE": Counter(),
        "TOTAL": Counter(),
    }
    failure_reasons = Counter()
    open_offer_dist = Counter()
    prefiltered_offer_dist = Counter()

    # Stage A all
    screened: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if str(row.get("our_bid_access") or "") != "YES":
            continue
        level = str(row.get("source_level") or "LOCAL").upper()
        if level not in funnel:
            level = "LOCAL"
        for bucket in (level, "TOTAL"):
            funnel[bucket]["access_yes_input"] += 1

        screen = stage_a_identity(row)
        priority = enrichment_priority(row, screen)
        for bucket in (level, "TOTAL"):
            funnel[bucket]["identity_screened"] += 1
            state = screen.get("identity_research_state")
            if state == IDENTITY_EXACT or state == IDENTITY_STRONG:
                funnel[bucket]["exact_strong_identity"] += 1
            elif state == OR_EQUAL_RESEARCHABLE:
                funnel[bucket]["or_equal_researchable"] += 1
            elif state == IDENTITY_INSUFFICIENT:
                funnel[bucket]["identity_insufficient"] += 1
                failure_reasons["identity_insufficient"] += 1
            else:
                funnel[bucket]["identity_partial"] += 1

        screened.append((row, screen, priority))

    # Order for deep enrichment
    order = {ENRICH_NOW: 0, ENRICH_NEXT: 1, ENRICH_IF_CAPACITY: 2, IDENTITY_NEEDED: 9}
    screened.sort(
        key=lambda t: (
            order.get(t[2].get("enrichment_priority"), 5),
            -int(t[2].get("priority_score") or 0),
        )
    )

    deep_candidates = [
        t
        for t in screened
        if t[1].get("researchable")
        and t[2].get("enrichment_priority") != IDENTITY_NEEDED
    ]
    if deep_limit is not None:
        deep_candidates = deep_candidates[:deep_limit]
    deep_ids = {
        id(t[0]) for t in deep_candidates
    }

    enriched_rows: list[dict[str, Any]] = []
    owner_queue: list[dict[str, Any]] = []
    internal_fails: list[dict[str, Any]] = []

    for i, (row, screen, priority) in enumerate(screened):
        level = str(row.get("source_level") or "LOCAL").upper()
        if level not in funnel:
            level = "LOCAL"
        deep = id(row) in deep_ids
        if deep:
            print(
                f"[phase_l2] deep {budget['usaspending']}/{budget['usaspending_max']} "
                f"mkt {budget['market']}/{budget['market_max']} "
                f"{(row.get('title') or '')[:60]}",
                flush=True,
            )
            norm = enrich_opportunity(
                row,
                budget=budget,
                cache=cache,
                authorize_live=authorize_live,
                deep=True,
            )
        else:
            # Stage-A only normalize
            enriched = apply_enrichment_to_row(row, screen=screen, priority=priority)
            norm = normalize_opportunity(enriched)
            for k in (
                "enrichment_priority",
                "identity_research_state",
                "or_equal_allowed",
                "registration_action",
                "is_easy_registration",
            ):
                if enriched.get(k) is not None:
                    norm[k] = enriched.get(k)
            norm["economics_completed"] = False
            norm["enrichment_blocker"] = "DEEP_NOT_SELECTED"

        # Funnel tallies for deep
        if deep:
            hist = (norm.get("enrichment") or {}).get("history") or {}
            mkt = (norm.get("enrichment") or {}).get("market") or {}
            # Re-read from norm fields
            for bucket in (level, "TOTAL"):
                if norm.get("history_research_state") or hist:
                    funnel[bucket]["historical_research_attempted"] += 1
                if norm.get("history_research_state") == "HISTORY_FOUND" or (
                    norm.get("historical_award_unit_price") is not None
                ):
                    funnel[bucket]["historical_price_found"] += 1
                if norm.get("historical_offers_received") is not None:
                    funnel[bucket]["offer_count_found"] += 1
                if norm.get("market_research_state") or mkt:
                    if norm.get("market_research_state") != "MARKET_PRICE_PENDING" or mkt.get("attempted"):
                        funnel[bucket]["market_research_attempted"] += 1
                if norm.get("public_retail_price") is not None or norm.get("public_retail_unit_price") is not None:
                    # public_retail may come from economics merge
                    pass
                if (norm.get("enrichment") or {}).get("market", {}).get(
                    "market_research_state"
                ) == "MARKET_PRICE_FOUND" or (
                    (norm.get("economics") or {}).get("public_retail_unit_price") is not None
                    and norm.get("economics_completed")
                ):
                    funnel[bucket]["exact_market_price_found"] += 1
                elif norm.get("public_retail_price") is not None:
                    funnel[bucket]["exact_market_price_found"] += 1

            # Fix funnel from enrichment block
            enr = norm.get("enrichment") or {}
            if enr.get("history", {}).get("attempted") or norm.get("history_research_state") not in {
                None,
                "HISTORY_PENDING",
            }:
                pass  # already counted
            if enr.get("market", {}).get("attempted"):
                for bucket in (level, "TOTAL"):
                    # ensure attempted counted once — use dedicated flags on norm
                    pass

            if norm.get("economics_completed"):
                for bucket in (level, "TOTAL"):
                    funnel[bucket]["economics_completed"] += 1
                net = norm.get("expected_net_profit")
                if isinstance(net, (int, float)):
                    for thr, key in (
                        (10000, "net_ge_10k"),
                        (25000, "net_ge_25k"),
                        (50000, "net_ge_50k"),
                        (75000, "net_ge_75k"),
                        (100000, "net_ge_100k"),
                    ):
                        if net >= thr:
                            for bucket in (level, "TOTAL"):
                                funnel[bucket][key] += 1

            blocker = norm.get("enrichment_blocker")
            if blocker:
                failure_reasons[str(blocker)] += 1
            elif norm.get("meets_floor") is False and norm.get("economics_completed"):
                failure_reasons["profit_below_10k"] += 1

            comp = norm.get("competition") or {}
            if comp.get("historical_offers_received") is not None:
                b = offer_bucket(comp["historical_offers_received"])
                if comp.get("effective_competition_signal") == "PRE_FILTERED_COMPETITION":
                    prefiltered_offer_dist[b] += 1
                elif comp.get("is_open_comparable"):
                    open_offer_dist[b] += 1

        enriched_rows.append(norm)
        if norm.get("meets_floor") and norm.get("expected_net_profit") is not None:
            owner_queue.append(norm)
        elif norm.get("economics_completed"):
            internal_fails.append(
                {
                    "title": (norm.get("title") or "")[:80],
                    "source_level": norm.get("source_level"),
                    "expected_net_profit": norm.get("expected_net_profit"),
                    "blocker": norm.get("enrichment_blocker") or "below_floor",
                }
            )

        if (i + 1) % 50 == 0:
            print(f"[phase_l2] screened {i+1}/{len(screened)}", flush=True)

    save_cache(cache)

    # Recount attempted accurately from enrichment payloads
    for level in list(funnel.keys()):
        for k in (
            "historical_research_attempted",
            "market_research_attempted",
            "exact_market_price_found",
            "historical_price_found",
            "offer_count_found",
            "economics_completed",
            "net_ge_10k",
            "net_ge_25k",
            "net_ge_50k",
            "net_ge_75k",
            "net_ge_100k",
        ):
            funnel[level][k] = 0

    for norm in enriched_rows:
        level = str(norm.get("source_level") or "LOCAL").upper()
        if level not in funnel:
            level = "LOCAL"
        enr = norm.get("enrichment") or {}
        hist = enr.get("history") or {}
        mkt = enr.get("market") or {}
        for bucket in (level, "TOTAL"):
            if hist.get("attempted") or hist.get("cache_hit"):
                funnel[bucket]["historical_research_attempted"] += 1
            if hist.get("historical_award_unit_price") is not None or hist.get(
                "history_research_state"
            ) == "HISTORY_FOUND":
                funnel[bucket]["historical_price_found"] += 1
            if hist.get("historical_offers_received") is not None or norm.get(
                "historical_offers_received"
            ) is not None:
                funnel[bucket]["offer_count_found"] += 1
            if mkt.get("attempted") or mkt.get("cache_hit"):
                funnel[bucket]["market_research_attempted"] += 1
            if mkt.get("public_retail_unit_price") is not None or mkt.get(
                "market_research_state"
            ) == "MARKET_PRICE_FOUND":
                funnel[bucket]["exact_market_price_found"] += 1
            if norm.get("economics_completed"):
                funnel[bucket]["economics_completed"] += 1
                net = norm.get("expected_net_profit")
                if isinstance(net, (int, float)):
                    if net >= 10000:
                        funnel[bucket]["net_ge_10k"] += 1
                    if net >= 25000:
                        funnel[bucket]["net_ge_25k"] += 1
                    if net >= 50000:
                        funnel[bucket]["net_ge_50k"] += 1
                    if net >= 75000:
                        funnel[bucket]["net_ge_75k"] += 1
                    if net >= 100000:
                        funnel[bucket]["net_ge_100k"] += 1

    owner_queue.sort(
        key=lambda r: (
            float(r.get("expected_net_profit") or 0),
            int((r.get("competition") or {}).get("competition_rank_score") or 0),
            float(r.get("runway_days") or 0),
        ),
        reverse=True,
    )

    def funnel_row(c: Counter) -> dict[str, int]:
        keys = [
            "access_yes_input",
            "identity_screened",
            "exact_strong_identity",
            "or_equal_researchable",
            "identity_partial",
            "identity_insufficient",
            "historical_research_attempted",
            "historical_price_found",
            "offer_count_found",
            "market_research_attempted",
            "exact_market_price_found",
            "economics_completed",
            "net_ge_10k",
            "net_ge_25k",
            "net_ge_50k",
            "net_ge_75k",
            "net_ge_100k",
        ]
        return {k: int(c.get(k, 0)) for k in keys}

    # Open-market offer stats
    open_offers = []
    for norm in enriched_rows:
        comp = norm.get("competition") or {}
        if comp.get("is_open_comparable") and comp.get("historical_offers_received") is not None:
            open_offers.append(int(comp["historical_offers_received"]))

    open_stats = {
        "median_offers": float(statistics.median(open_offers)) if open_offers else None,
        "count_with_offers": len(open_offers),
        "le_2": sum(1 for n in open_offers if n <= 2),
        "le_3": sum(1 for n in open_offers if n <= 3),
        "le_5": sum(1 for n in open_offers if n <= 5),
        "le_10": sum(1 for n in open_offers if n <= 10),
        "distribution": dict(open_offer_dist),
        "prefiltered_distribution": dict(prefiltered_offer_dist),
    }

    payload = {
        "kind": "PhaseL2EnrichmentResult",
        "phase": "L.2",
        "generated_at": _utc(),
        "budget_used": {
            "usaspending": budget["usaspending"],
            "market": budget["market"],
        },
        "funnel": {k: funnel_row(funnel[k]) for k in funnel},
        "open_competition_stats": open_stats,
        "failure_reasons": failure_reasons.most_common(25),
        "counts": {
            "access_yes_input": int(funnel["TOTAL"]["access_yes_input"]),
            "identity_screened": int(funnel["TOTAL"]["identity_screened"]),
            "deep_selected": len(deep_candidates),
            "owner_queue": len(owner_queue),
            "economics_completed": int(funnel["TOTAL"]["economics_completed"]),
            "net_ge_10k": int(funnel["TOTAL"]["net_ge_10k"]),
        },
        "owner_queue": owner_queue[:50],
        "top20": owner_queue[:20],
        "internal_economic_fails_sample": internal_fails[:30],
        "enriched_sample": enriched_rows[:40],
        "all_enriched_count": len(enriched_rows),
    }
    _write("enrichment_latest.json", payload)
    _write(
        "owner_profit_queue.json",
        {
            "kind": "PhaseL2OwnerProfitQueue",
            "count": len(owner_queue),
            "rows": owner_queue[:100],
        },
    )
    return payload
