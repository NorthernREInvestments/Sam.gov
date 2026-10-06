"""Freeze immutable HARD_MISS_CORPUS_V1 from current 28 misses."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from price_coverage_80.corpus import confirmed_public, load_corpus
from hard_miss_recovery.models import BUILD, FROZEN, HARD_MISS_CORPUS, PRICE_FOUND, PRIOR_SR_CK
from hard_miss_recovery.route import classify_strategy


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_hard_miss_corpus(*, force: bool = False) -> dict[str, Any]:
    out = data_path(HARD_MISS_CORPUS)
    if out.exists() and not force:
        return json.loads(out.read_text(encoding="utf-8"))

    prior = _load(PRIOR_SR_CK)
    # Prefer honest hard-miss checkpoint if present (excludes search-shell inflation)
    hm_ck = _load("m3_hard_miss_recovery_v1_checkpoint.json")
    by = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    from hard_miss_recovery.sweep import _found_is_executable

    priced = set()
    source_items = (hm_ck.get("items") if hm_ck.get("items") else None) or (prior.get("items") or {})
    for bid, row in source_items.items():
        found = row.get("found") or {}
        item = by.get(bid) or {}
        if _found_is_executable(
            found,
            mpn=str(item.get("mpn") or ""),
            pack=int(item.get("expected_pack") or 1),
            bid=bid,
        ):
            priced.add(bid)
    misses: list[dict[str, Any]] = []
    for item in confirmed_public():
        bid = item["benchmark_id"]
        if bid in priced:
            continue
        full = by.get(bid) or item
        prow = (prior.get("items") or {}).get(bid) or {}
        tried = (prow.get("extraction") or {}).get("urls_tried") or []
        domains = sorted({t.get("seller") for t in tried if t.get("seller")})
        urls = [t.get("url") for t in tried if t.get("url")]
        disc = (prow.get("rediscovery") or {}).get("discovered_exact_urls") or []
        for d in disc:
            if d.get("url") and d["url"] not in urls:
                urls.append(d["url"])
            if d.get("domain") and d["domain"] not in domains:
                domains.append(d["domain"])

        row = {
            "benchmark_id": bid,
            "manufacturer": full.get("manufacturer"),
            "brand": full.get("brand") or full.get("manufacturer"),
            "mpn": full.get("mpn") or full.get("part_number"),
            "description": full.get("description") or full.get("raw_description"),
            "required_condition": full.get("expected_condition") or "NEW",
            "required_uom": full.get("expected_uom") or "EA",
            "required_pack": full.get("expected_pack") or 1,
            "known_seller": full.get("known_public_seller"),
            "known_public_price": full.get("known_public_price"),
            "known_public_url": full.get("known_public_url") or full.get("product_url"),
            "benchmark_verified_date": full.get("verified_at")
            or full.get("benchmark_verified_date")
            or "corpus_build",
            "category": full.get("category"),
            "current_failure_class": prow.get("status") or "NO_PRICE",
            "previously_attempted_domains": domains,
            "previously_attempted_urls": urls,
            "primary_strategy": classify_strategy(full, domains),
            "prior_extraction": {
                "status": (prow.get("extraction") or {}).get("status"),
                "n_discovered": (prow.get("rediscovery") or {}).get("n_new_exact_urls"),
            },
        }
        misses.append(row)

    payload = {
        "kind": "HARD_MISS_CORPUS_V1",
        "build": BUILD,
        "immutable": True,
        "created_at": now_utc().isoformat(),
        "n_misses": len(misses),
        "source_checkpoint": PRIOR_SR_CK,
        "prior_priced": len(priced),
        "misses": misses,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_hard_miss_corpus() -> dict[str, Any]:
    p = data_path(HARD_MISS_CORPUS)
    if not p.exists():
        return build_hard_miss_corpus()
    return json.loads(p.read_text(encoding="utf-8"))
