"""Freeze baseline + build immutable contradiction corpus."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from live_seller_benchmark_reverify.models import (
    BASELINE,
    BUILD,
    CONTRADICTION_CORPUS,
    EASY25_BASELINE_PRICED,
    EASY25_DENOM,
    HONEST_BASELINE,
    ORIGINAL_DENOM,
    PRIOR_CONTRADICTIONS,
    PRIOR_HM_CK,
    PRIOR_KPE_CORPUS,
    PRIOR_LEP_CK,
    PRIOR_OSE_CK,
    PRIOR_P14_CK,
)
from m3_data_root import data_path
from price_coverage_80.corpus import load_corpus


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def collect_priced_ids() -> set[str]:
    priced: set[str] = set()
    for src in (PRIOR_HM_CK, PRIOR_P14_CK, PRIOR_OSE_CK, PRIOR_LEP_CK):
        data = _load(src)
        if src == PRIOR_HM_CK:
            for bid, row in (data.get("items") or {}).items():
                if (row.get("found") or {}).get("usable"):
                    priced.add(bid)
        elif src == PRIOR_P14_CK:
            for stage in ("stage_a", "stage_b"):
                for bid, row in (data.get(stage) or {}).items():
                    if row.get("status") in {"PASS", "EXECUTABLE_PRICE"}:
                        priced.add(bid)
        else:
            for bid, row in (data.get("items") or {}).items():
                if row.get("status") == "EXECUTABLE_PRICE":
                    priced.add(bid)
    return priced


def freeze_baseline() -> dict[str, Any]:
    priced = collect_priced_ids()
    payload = {
        "name": "FULL100_REVERIFY_BASELINE_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "original_denominator": ORIGINAL_DENOM,
        "current_valid_prices": HONEST_BASELINE,
        "priced_ids_count": len(priced),
        "coverage": round(HONEST_BASELINE / ORIGINAL_DENOM, 4),
        "coverage_pct": round(100.0 * HONEST_BASELINE / ORIGINAL_DENOM, 1),
        "accuracy": 1.0,
        "easy25": {
            "priced": EASY25_BASELINE_PRICED,
            "denominator": EASY25_DENOM,
            "coverage": round(EASY25_BASELINE_PRICED / EASY25_DENOM, 4),
            "accuracy": 1.0,
        },
        "note": "Frozen after live exact priced PDP v1 (50/82). Soft URLs excluded.",
    }
    _save(BASELINE, payload)
    return payload


def _candidate_urls_for(bid: str, full_item: dict[str, Any]) -> list[dict[str, Any]]:
    urls: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(url: str, domain: str | None = None, soft: bool = False, method: str = "") -> None:
        if not url or not str(url).startswith("http") or url in seen:
            return
        seen.add(url)
        urls.append(
            {
                "url": url,
                "domain": domain or "",
                "soft_identity": soft,
                "discovery_method": method,
            }
        )

    kpe = _load(PRIOR_KPE_CORPUS)
    for row in kpe.get("items") or []:
        if row.get("benchmark_id") != bid:
            continue
        for p in row.get("pdps") or []:
            _add(p.get("url") or "", p.get("domain"), bool(p.get("soft_identity")), "KPE")

    lep = _load(PRIOR_LEP_CK)
    for a in ((lep.get("items") or {}).get(bid) or {}).get("audits") or []:
        _add(a.get("url") or "", a.get("domain"), False, "LEP_AUDIT")

    ose = _load(PRIOR_OSE_CK)
    for p in ((ose.get("items") or {}).get(bid) or {}).get("exact_pdps") or []:
        _add(p.get("url") or "", p.get("domain"), bool(p.get("soft_identity")), "OSE")

    hint = full_item.get("known_url_hint")
    if hint:
        _add(str(hint), None, True, "BENCHMARK_HINT")
    return urls


def _attempted_domains(bid: str, candidates: list[dict[str, Any]]) -> list[str]:
    doms: set[str] = set()
    for c in candidates:
        d = (c.get("domain") or "").replace("www.", "")
        if d:
            doms.add(d)
    lep = _load(PRIOR_LEP_CK)
    for a in ((lep.get("items") or {}).get(bid) or {}).get("audits") or []:
        d = (a.get("domain") or "").replace("www.", "")
        if d:
            doms.add(d)
    return sorted(doms)


def build_contradiction_corpus() -> dict[str, Any]:
    """Immutable BENCHMARK_CONTRADICTION_CORPUS_V1 from prior LEP flags."""
    prior = _load(PRIOR_CONTRADICTIONS)
    full = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    lep = _load(PRIOR_LEP_CK)
    items: list[dict[str, Any]] = []

    for row in prior.get("items") or []:
        bid = row.get("benchmark_id")
        if not bid:
            continue
        base = dict(full.get(bid) or {"benchmark_id": bid})
        candidates = _candidate_urls_for(bid, base)
        lep_row = (lep.get("items") or {}).get(bid) or {}
        items.append(
            {
                "benchmark_id": bid,
                "manufacturer": base.get("manufacturer"),
                "mpn": base.get("mpn") or base.get("model"),
                "model": base.get("model"),
                "description": base.get("description"),
                "category": base.get("category"),
                "expected_condition": base.get("expected_condition") or "NEW",
                "expected_pack": base.get("expected_pack") or 1,
                "expected_uom": base.get("expected_uom") or "EA",
                "original_benchmark_seller": base.get("known_public_seller"),
                "original_benchmark_price": base.get("known_public_price"),
                "original_benchmark_url": base.get("known_url_hint"),
                "original_verification_date": base.get("date_verified"),
                "truth_class": base.get("truth_class"),
                "candidate_urls": candidates,
                "current_failure_classification": list(row.get("reasons") or []),
                "seller_domains_attempted": _attempted_domains(bid, candidates),
                "prior_lep_status": lep_row.get("status"),
                "product": row.get("product") or f"{base.get('manufacturer') or ''} {base.get('mpn') or bid}".strip(),
            }
        )

    payload = {
        "name": "BENCHMARK_CONTRADICTION_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "count": len(items),
        "immutable": True,
        "items": items,
        "note": "Do not remove from denominator without Phase-4/5 evidence.",
    }
    _save(CONTRADICTION_CORPUS, payload)
    return payload


def load_contradiction_corpus() -> list[dict[str, Any]]:
    data = _load(CONTRADICTION_CORPUS)
    if data.get("items"):
        return list(data["items"])
    return list(build_contradiction_corpus().get("items") or [])


def load_unresolved_for_expansion(priced: set[str] | None = None) -> list[dict[str, Any]]:
    """Unresolved Full-100 items still believed publicly priceable (not yet removed)."""
    priced = priced or collect_priced_ids()
    full = [
        i
        for i in (load_corpus().get("items") or [])
        if i.get("truth_class") == "PUBLIC_NEW_PRICE_CONFIRMED"
    ]
    contrad_ids = {r["benchmark_id"] for r in load_contradiction_corpus()}
    out: list[dict[str, Any]] = []
    for item in full:
        bid = item["benchmark_id"]
        if bid in priced:
            continue
        # Include contradictions + other unresolved
        row = dict(item)
        row["is_contradiction"] = bid in contrad_ids
        row["pdps"] = _candidate_urls_for(bid, item)
        out.append(row)
    return sorted(out, key=lambda x: (0 if x.get("is_contradiction") else 1, x.get("benchmark_id") or ""))


def load_easy25_items() -> list[dict[str, Any]]:
    full = load_corpus().get("items") or []
    return [dict(i) for i in full if str(i.get("benchmark_id") or "").startswith("easy-")]
