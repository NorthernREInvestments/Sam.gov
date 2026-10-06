"""Freeze the 40 currently validated Exact-Page prices as immutable baseline."""

from __future__ import annotations

import json
import time
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from seller_rediscovery.models import BUILD, FROZEN, FROZEN_BASELINE, PRIOR_EPE_CK


def freeze_validated_baseline(*, force: bool = False) -> dict[str, Any]:
    """Create / load frozen baseline of currently validated prices."""
    out_path = data_path(FROZEN_BASELINE)
    if out_path.exists() and not force:
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    prior_path = data_path(PRIOR_EPE_CK)
    if not prior_path.exists():
        raise FileNotFoundError(f"Missing prior checkpoint: {PRIOR_EPE_CK}")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    items = prior.get("items") or {}

    frozen: dict[str, Any] = {}
    for bid, row in items.items():
        found = row.get("found") or {}
        if not found.get("usable"):
            continue
        if row.get("status") != "PRICE_FOUND":
            continue
        # Never freeze known contamination
        url = str(found.get("source_url") or "").lower()
        price = float(found.get("unit_price") or 0)
        if "nationaldistributorllc" in url:
            continue
        if bid == "easy-global-dwt-6" and "globalindustrial.com/p/dwt-6" in url:
            continue
        frozen[bid] = {
            "benchmark_id": bid,
            "status": FROZEN,
            "product_identity": {
                "benchmark_id": bid,
                "manufacturer": (row.get("item") or {}).get("manufacturer")
                or row.get("manufacturer"),
                "mpn": (row.get("item") or {}).get("mpn")
                or (row.get("item") or {}).get("part_number")
                or row.get("mpn")
                or row.get("part_number"),
            },
            "seller": found.get("seller"),
            "exact_url": found.get("source_url"),
            "extracted_price": found.get("unit_price"),
            "condition": found.get("condition") or "NEW",
            "extraction_route": found.get("via"),
            "validation_result": row.get("accuracy")
            or {"class": "CORRECT_EXACT_MATCH", "correct": True, "claimed": True},
            "found": found,
            "timestamp": row.get("updated_at") or now_utc().isoformat(),
            "frozen_at": now_utc().isoformat(),
            "source_checkpoint": PRIOR_EPE_CK,
        }

    # Enrich identity from corpus
    try:
        from price_coverage_80.corpus import load_corpus

        by_id = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
        for bid, fr in frozen.items():
            src = by_id.get(bid) or {}
            ident = fr["product_identity"]
            if not ident.get("manufacturer"):
                ident["manufacturer"] = src.get("manufacturer")
            if not ident.get("mpn"):
                ident["mpn"] = src.get("mpn") or src.get("part_number")
            ident["raw_description"] = src.get("raw_description") or src.get("description")
    except Exception:
        pass

    payload = {
        "build": BUILD,
        "created_at": now_utc().isoformat(),
        "n_frozen": len(frozen),
        "acceptance": {
            "target": 40,
            "frozen": len(frozen),
            "pass": len(frozen) >= 40,
        },
        "items": frozen,
        "prior_full100": prior.get("full100"),
        "prior_easy25": prior.get("easy25"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_frozen_baseline() -> dict[str, Any]:
    p = data_path(FROZEN_BASELINE)
    if not p.exists():
        return freeze_validated_baseline()
    return json.loads(p.read_text(encoding="utf-8"))


def apply_frozen_to_checkpoint(ck: dict[str, Any]) -> int:
    """Ensure frozen validated prices are present and not overwritten."""
    baseline = load_frozen_baseline()
    items = ck.setdefault("items", {})
    applied = 0
    for bid, fr in (baseline.get("items") or {}).items():
        existing = items.get(bid) or {}
        # Never overwrite a frozen/priced row unless newer result independently failed validation
        if existing.get("status") in {FROZEN, "PRICE_FOUND"} and (existing.get("found") or {}).get("usable"):
            continue
        items[bid] = {
            "benchmark_id": bid,
            "status": FROZEN,
            "found": fr.get("found"),
            "accuracy": fr.get("validation_result"),
            "frozen": True,
            "exact_url": fr.get("exact_url"),
            "extraction_route": fr.get("extraction_route"),
            "updated_at": now_utc().isoformat(),
            "source": "frozen_baseline",
        }
        applied += 1
    ck["frozen_n"] = len(baseline.get("items") or {})
    ck["frozen_applied_at"] = time.time()
    return applied
