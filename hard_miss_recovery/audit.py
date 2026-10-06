"""Independent revalidation of Full-100 denominator (82 items)."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from hard_miss_recovery.models import (
    AMBIGUOUS,
    BENCHMARK_SOURCE_STALE,
    BUILD,
    CURRENT_PUBLIC_NEW_PRICE_VERIFIED,
    CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT,
    DEAD_PRIMARY,
    DENOM_AUDIT,
    FROZEN,
    KNOWN_PAGE_DEAD,
    LEAVE_DENOMINATOR,
    NO_LONGER_PUBLICLY_PRICED,
    PRICE_FOUND,
    PRIOR_SR_CK,
    PRODUCT_DISCONTINUED,
    QUOTE_ONLY_NOW,
)
from m3_data_root import data_path
from price_adapters.validate import mpn_in_blob, seller_of
from price_coverage_80.corpus import confirmed_public, load_corpus
from public_price_search.search import fetch_page, search_web


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _call_for_price(html: str) -> bool:
    return bool(
        re.search(
            r"call\s*(for|/)\s*price|login\s*to\s*(see|view)\s*price|price\s*upon\s*request|"
            r"request\s*a\s*quote|sign\s*in\s*for\s*price|discontinued",
            (html or "")[:25000],
            re.I,
        )
    )


def _discontinued(html: str) -> bool:
    return bool(re.search(r"\bdiscontinued\b|no longer available|product not found", (html or "")[:20000], re.I))


def audit_one(item: dict[str, Any], *, prior_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify current public priceability. Fail-closed on removals."""
    bid = item["benchmark_id"]
    mpn = str(item.get("mpn") or "")
    mfr = str(item.get("manufacturer") or "")
    known_seller = str(item.get("known_public_seller") or "").lower().replace("www.", "")
    prior = prior_row or {}
    found = prior.get("found") or {}

    base = {
        "benchmark_id": bid,
        "mpn": mpn,
        "manufacturer": mfr,
        "known_seller": known_seller,
        "revalidated_at": now_utc().isoformat(),
        "evidence": [],
        "leave_denominator": False,
    }

    # Already recovered valid NEW price → verified current public
    if prior.get("status") in {PRICE_FOUND, FROZEN} and found.get("usable") and found.get("unit_price"):
        return {
            **base,
            "class": CURRENT_PUBLIC_NEW_PRICE_VERIFIED,
            "source": found.get("source_url") or found.get("seller"),
            "reason": "m3_valid_recovery_or_frozen",
            "evidence": [
                {
                    "seller": found.get("seller"),
                    "url": found.get("source_url"),
                    "price": found.get("unit_price"),
                }
            ],
        }

    evidence: list[dict[str, Any]] = []
    hard_to_extract = False
    quote_only = False
    dead_known = False
    discontinued = False
    alt_public_signal = False

    # Probe known seller URL patterns / search — identity only on dead primaries
    queries = [
        f'{mfr} "{mpn}" price',
        f'"{mpn}" buy',
    ]
    for q in queries[:2]:
        try:
            res = search_web(q, limit=6, use_budget=True)
        except Exception:
            res = {"results": []}
        for r in res.get("results") or []:
            url = r.get("url") or ""
            host = seller_of(url)
            title = r.get("title") or ""
            snip = r.get("snippet") or ""
            blob = f"{title} {snip}"
            if not mpn_in_blob(mpn, blob, url) and mfr.lower().split()[0] not in blob.lower():
                continue
            # Price signal in snippet is discovery only
            price_hint = re.search(r"\$\s*([0-9]+(?:\.[0-9]{2})?)", blob)
            row = {
                "url": url,
                "host": host,
                "title": title[:120],
                "price_hint": float(price_hint.group(1)) if price_hint else None,
                "dead_primary": host in DEAD_PRIMARY,
            }
            evidence.append(row)
            if host in DEAD_PRIMARY:
                hard_to_extract = True
            elif price_hint and float(price_hint.group(1)) >= 1.51:
                alt_public_signal = True
            elif host not in DEAD_PRIMARY:
                alt_public_signal = True

    # Fetch 1–2 non-dead candidate pages for stronger evidence
    open_urls = [e["url"] for e in evidence if not e.get("dead_primary")][:2]
    for url in open_urls:
        fr = fetch_page(url)
        html = fr.get("text") or ""
        if fr.get("blocked"):
            hard_to_extract = True
            evidence.append({"url": url, "status": "BOT_WALL"})
            continue
        if fr.get("status_code") in {404, 410} or (fr.get("ok") and _discontinued(html)):
            if known_seller and known_seller in seller_of(url):
                dead_known = True
            if _discontinued(html):
                discontinued = True
            evidence.append({"url": url, "status": "DEAD_OR_DISCONTINUED", "code": fr.get("status_code")})
            continue
        if fr.get("ok") and html:
            if _call_for_price(html) and not re.search(r'"price"\s*:\s*"?\d', html[:50000]):
                quote_only = True
                evidence.append({"url": url, "status": "QUOTE_ONLY"})
            elif mpn_in_blob(mpn, html[:20000], url):
                # Product page exists publicly
                if re.search(r'"price"\s*:\s*"?\d+\.?\d*', html[:80000]) or re.search(
                    r"product:price:amount", html[:20000], re.I
                ):
                    alt_public_signal = True
                    evidence.append({"url": url, "status": "PUBLIC_PRICE_MARKUP"})
                else:
                    hard_to_extract = True
                    evidence.append({"url": url, "status": "PRODUCT_PAGE_NO_CLEAR_PRICE"})

    # Known seller alone is dead-primary → hard to extract unless alt signal
    if known_seller in DEAD_PRIMARY and not alt_public_signal:
        hard_to_extract = True

    # Classification — removals require positive evidence
    if discontinued and not alt_public_signal:
        cls = PRODUCT_DISCONTINUED
        reason = "discontinued_marker_no_alt_public"
    elif dead_known and not alt_public_signal and not evidence:
        cls = KNOWN_PAGE_DEAD
        reason = "known_page_dead_no_alternates"
    elif quote_only and not alt_public_signal and not hard_to_extract:
        cls = QUOTE_ONLY_NOW
        reason = "quote_only_no_public_price_found"
    elif alt_public_signal:
        cls = CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT if not found.get("usable") else CURRENT_PUBLIC_NEW_PRICE_VERIFIED
        reason = "alternate_public_seller_or_markup_signal"
    elif hard_to_extract:
        # Still in denominator — human may still get price on dead-primary sites
        cls = CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT
        reason = "known_or_search_signal_but_extraction_hard"
    elif evidence:
        cls = AMBIGUOUS
        reason = "search_hits_without_clear_public_price"
    else:
        # No evidence to remove — stay ambiguous, remain in denominator
        cls = AMBIGUOUS
        reason = "insufficient_evidence_to_remove_from_denominator"

    leave = cls in LEAVE_DENOMINATOR
    # Extra safety: never leave solely on AMBIGUOUS
    if cls == AMBIGUOUS:
        leave = False

    return {
        **base,
        "class": cls,
        "source": (evidence[0].get("url") if evidence else None),
        "reason": reason,
        "evidence": evidence[:8],
        "leave_denominator": leave,
    }


def run_denominator_audit(*, force: bool = False) -> dict[str, Any]:
    outp = data_path(DENOM_AUDIT)
    if outp.exists() and not force:
        try:
            return json.loads(outp.read_text(encoding="utf-8"))
        except Exception:
            pass

    prior = _load(PRIOR_SR_CK)
    items = confirmed_public()
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    print(f"[denom_audit] start n={len(items)} build={BUILD}", flush=True)
    for idx, item in enumerate(items, 1):
        bid = item["benchmark_id"]
        prow = (prior.get("items") or {}).get(bid) or {}
        row = audit_one(item, prior_row=prow)
        rows.append(row)
        counts[row["class"]] = counts.get(row["class"], 0) + 1
        if idx % 10 == 0:
            print(f"[denom_audit] progress {idx}/{len(items)} leave={sum(1 for r in rows if r.get('leave_denominator'))}", flush=True)

    removed = [r for r in rows if r.get("leave_denominator")]
    audited_denom = len(items) - len(removed)
    payload = {
        "build": BUILD,
        "created_at": now_utc().isoformat(),
        "original_denominator": len(items),
        "audited_denominator": audited_denom,
        "denominator_changed": bool(removed),
        "counts": counts,
        "removed_or_reclassified": removed,
        "items": {r["benchmark_id"]: r for r in rows},
        "still_currently_publicly_priceable": audited_denom,
        "public_but_difficult": counts.get(CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT, 0),
        "quote_only_now": counts.get(QUOTE_ONLY_NOW, 0),
        "no_longer_publicly_priced": counts.get(NO_LONGER_PUBLICLY_PRICED, 0),
        "discontinued": counts.get(PRODUCT_DISCONTINUED, 0),
        "dead_stale": counts.get(KNOWN_PAGE_DEAD, 0) + counts.get(BENCHMARK_SOURCE_STALE, 0),
        "ambiguous": counts.get(AMBIGUOUS, 0),
        "current_public_new_verified": counts.get(CURRENT_PUBLIC_NEW_PRICE_VERIFIED, 0),
    }
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(
        f"[denom_audit] done original={len(items)} audited={audited_denom} "
        f"removed={len(removed)} verified={payload['current_public_new_verified']}",
        flush=True,
    )
    return payload


def load_denominator_audit() -> dict[str, Any]:
    p = data_path(DENOM_AUDIT)
    if not p.exists():
        return run_denominator_audit()
    return json.loads(p.read_text(encoding="utf-8"))
