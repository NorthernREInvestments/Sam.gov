"""Phase 1–3 — deterministic stratified LARGE_TEST_CORPUS_V1."""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from typing import Any

from application_clock import now_utc
from large_production_test.models import BUILD, CORPUS, MIN_SAMPLE, SEED, TARGET_SAMPLE
from m3_data_root import data_path


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    data_path(name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _stable_hash(s: str) -> int:
    return int(hashlib.sha256(f"{SEED}:{s}".encode()).hexdigest()[:12], 16)


def _jurisdiction_bucket(row: dict[str, Any]) -> str:
    j = str(row.get("jurisdiction") or "").upper()
    if row.get("is_federal") or j == "FEDERAL" or "sam" in str(row.get("platform") or "").lower():
        return "FEDERAL"
    if j in {"STATE", "STATE_LOCAL"}:
        return "STATE"
    if j in {"LOCAL", "CITY", "COUNTY"}:
        return "LOCAL"
    title = f"{row.get('title') or ''} {row.get('buyer') or ''}".lower()
    if any(k in title for k in ("school", "university", "college", "usd ", "isd ", "education")):
        return "EDUCATION"
    if any(k in title for k in ("utility", "water", "electric", "power authority", "municipal light")):
        return "UTILITIES"
    if j == "COOPERATIVE":
        return "COOPERATIVE"
    return "OTHER"


def _platform_bucket(row: dict[str, Any], oid: str) -> str:
    plat = str(row.get("platform") or "").lower()
    if oid.startswith("opengov:") or "opengov" in plat or "live_structured" in plat:
        return "OpenGov"
    if "bidnet" in plat:
        return "BidNet"
    if "sam" in plat or row.get("is_federal"):
        return "SAM"
    return "Other"


def _category_guess(title: str, identities: list[dict[str, Any]] | None = None) -> str:
    blob = (title or "").lower()
    for i in identities or []:
        blob += " " + str(i.get("raw_description") or "").lower()
        blob += " " + str(i.get("category") or "").lower()
    rules = [
        ("PPE", ("glove", "mask", "ppe", "respirator", "safety vest", "goggle")),
        ("Tools", ("tool", "wrench", "drill", "saw", "hammer", "pliers")),
        ("MRO", ("filter", "bearing", "belt", "mro", "maintenance", "lubricant")),
        ("Office", ("paper", "toner", "printer", "office", "stapler", "pen ")),
        ("Furniture", ("chair", "desk", "table", "furniture", "cabinet")),
        ("Lighting", ("led", "lamp", "lighting", "fixture", "bulb")),
        ("Plumbing", ("valve", "pipe", "plumbing", "faucet", "fitting")),
        ("HVAC", ("hvac", "air handler", "thermostat", "condenser", "furnace")),
        ("Parts", ("part", "oem", "mpn", "replacement", "spare")),
    ]
    for cat, keys in rules:
        if any(k in blob for k in keys):
            return cat
    return "Other"


def build_population() -> list[dict[str, Any]]:
    """Load usable product population from current stores (no new discovery)."""
    rows: dict[str, dict[str, Any]] = {}

    # 1) OpenGov package-backed product opportunities (deep-funnel capable)
    try:
        from evidence_breakthrough.corpus import load_identity_store

        id_store = load_identity_store().get("by_opportunity") or {}
    except Exception:
        id_store = {}
    elig = (_load("m3_eligibility_file_mine_store.json").get("by_opportunity") or {})
    for oid, pack in id_store.items():
        buyer = oid.split(":")[1] if oid.count(":") >= 2 else None
        rows[oid] = {
            "opportunity_id": oid,
            "source_population": "opengov_package_identity_store",
            "buyer": buyer,
            "title": pack.get("title") or pack.get("solicitation_title") or "",
            "jurisdiction": "LOCAL",
            "platform": "live_opengov",
            "is_federal": False,
            "package_backed": True,
            "identity_count": len(pack.get("identities") or []),
            "identities": pack.get("identities") or [],
            "eligibility_pre": (elig.get(oid) or {}).get("status") or (elig.get(oid) or {}).get("eligibility_status"),
            "deadline": pack.get("deadline"),
        }

    # 2) L23 tangible products (volume universe — mostly BidNet)
    l23 = _load("l23_canonical_population_store.json")
    for cid, row in (l23.get("opportunities") or {}).items():
        uc = str(row.get("universe_class") or row.get("universe_classification") or "")
        if uc != "TANGIBLE_PRODUCT" and not row.get("eligible_for_profit_research"):
            continue
        if uc in {"PURE_SERVICE", "CONSTRUCTION"}:
            continue
        # Skip non-product service classifiers
        if uc == "UNKNOWN" and not row.get("eligible_for_profit_research"):
            continue
        oid = f"l23:{cid}"
        if oid in rows:
            continue
        # Avoid duplicating OpenGov if solicitation matches packaged set by buyer+title later
        rows[oid] = {
            "opportunity_id": oid,
            "canonical_opportunity_id": cid,
            "source_population": "l23_tangible_product",
            "buyer": row.get("buyer"),
            "title": row.get("title") or "",
            "jurisdiction": row.get("jurisdiction"),
            "platform": row.get("platform"),
            "is_federal": bool(row.get("is_federal")),
            "package_backed": False,
            "identity_count": 0,
            "identities": [],
            "deadline": row.get("deadline"),
            "current_funnel_state": row.get("current_funnel_state"),
            "authoritative_url": row.get("authoritative_url"),
            "universe_class": uc or "TANGIBLE_PRODUCT",
        }

    # 3) Line economics extras
    econ = _load("m3_line_item_economics_store.json").get("by_opportunity") or {}
    for oid, erow in econ.items():
        if oid not in rows:
            rows[oid] = {
                "opportunity_id": oid,
                "source_population": "line_item_economics_store",
                "buyer": erow.get("buyer"),
                "title": erow.get("title") or "",
                "jurisdiction": "LOCAL",
                "platform": "live_opengov" if oid.startswith("opengov:") else "other",
                "is_federal": False,
                "package_backed": oid.startswith("opengov:"),
                "identity_count": int((erow.get("extraction") or {}).get("line_count") or 0),
                "identities": [],
                "deadline": erow.get("deadline"),
                "line_count_hint": int((erow.get("extraction") or {}).get("line_count") or 0),
            }
        else:
            rows[oid]["line_count_hint"] = int((erow.get("extraction") or {}).get("line_count") or 0)
            rows[oid]["has_line_economics"] = True

    out = list(rows.values())
    for r in out:
        r["source_bucket"] = _platform_bucket(r, r["opportunity_id"])
        r["jurisdiction_bucket"] = _jurisdiction_bucket(r)
        r["category_bucket"] = _category_guess(r.get("title") or "", r.get("identities"))
        n = int(r.get("line_count_hint") or r.get("identity_count") or 0)
        if n <= 1:
            r["line_bucket"] = "1"
        elif n <= 10:
            r["line_bucket"] = "2-10"
        elif n <= 50:
            r["line_bucket"] = "11-50"
        elif n <= 100:
            r["line_bucket"] = "51-100"
        else:
            r["line_bucket"] = "100+"
    return out


def select_corpus(*, target: int = TARGET_SAMPLE) -> dict[str, Any]:
    """Deterministic stratified sample. Prefer representative mix; report strata."""
    population = build_population()
    pop_ids = [r["opportunity_id"] for r in population]
    by_id = {r["opportunity_id"]: r for r in population}

    # Explicit strata (reported) — not easiest-only
    # A: package-backed deep funnel capacity (cap 200 so BidNet volume still dominates)
    # B–F: jurisdiction mix from remaining population
    rng = random.Random(SEED)

    package_backed = [r for r in population if r.get("package_backed")]
    non_package = [r for r in population if not r.get("package_backed")]

    strata_targets = {
        "package_backed_deep": min(200, len(package_backed)),
        "FEDERAL": 75,
        "STATE": 100,
        "LOCAL": 100,
        "EDUCATION": 50,
        "UTILITIES": 25,
        "OTHER_FILL": 0,  # remainder
    }

    selected: list[str] = []

    def _take(pool: list[dict[str, Any]], n: int) -> list[str]:
        if n <= 0 or not pool:
            return []
        ordered = sorted(pool, key=lambda r: (_stable_hash(r["opportunity_id"]), r["opportunity_id"]))
        # shuffle deterministically within hash bands
        rng2 = random.Random(SEED ^ len(ordered) ^ n)
        ordered = list(ordered)
        rng2.shuffle(ordered)
        picks = []
        for r in ordered:
            if r["opportunity_id"] in selected:
                continue
            picks.append(r["opportunity_id"])
            if len(picks) >= n:
                break
        return picks

    selected.extend(_take(package_backed, strata_targets["package_backed_deep"]))

    for jb, n in [
        ("FEDERAL", strata_targets["FEDERAL"]),
        ("STATE", strata_targets["STATE"]),
        ("LOCAL", strata_targets["LOCAL"]),
        ("EDUCATION", strata_targets["EDUCATION"]),
        ("UTILITIES", strata_targets["UTILITIES"]),
    ]:
        pool = [r for r in non_package if r.get("jurisdiction_bucket") == jb]
        # also allow package-backed leftovers into jurisdiction if still short later
        selected.extend(_take(pool, n))

    # Fill to target from remaining population (deterministic)
    remaining = [r for r in population if r["opportunity_id"] not in selected]
    need = max(0, target - len(selected))
    selected.extend(_take(remaining, need))

    # If still short, take remaining package-backed
    if len(selected) < target:
        selected.extend(_take(package_backed, target - len(selected)))

    selected = list(dict.fromkeys(selected))[:target]
    items = [by_id[i] for i in selected if i in by_id]

    why_short = None
    if len(items) < target:
        why_short = (
            f"Only {len(items)} unique usable product opportunities available from current stores "
            f"(population={len(population)}); did not duplicate IDs or force new discovery."
        )

    assert len(items) == len({i["opportunity_id"] for i in items})

    corpus = {
        "name": "LARGE_TEST_CORPUS_V1",
        "immutable": True,
        "build": BUILD,
        "selection_rule": (
            "deterministic_stratified_v1: seed + sha256 order; "
            "strata=package_backed_deep<=200 + jurisdiction FEDERAL/STATE/LOCAL/EDUCATION/UTILITIES + fill; "
            "no preferential profitable/easy-price selection"
        ),
        "seed": SEED,
        "selection_timestamp": now_utc().isoformat(),
        "target": target,
        "minimum_acceptable": MIN_SAMPLE,
        "source_population_count": len(population),
        "source_population_breakdown": {
            "package_backed": len(package_backed),
            "non_package": len(non_package),
            "jurisdiction": dict(Counter(r["jurisdiction_bucket"] for r in population)),
            "source": dict(Counter(r["source_bucket"] for r in population)),
        },
        "strata_targets": strata_targets,
        "strata_actual": {
            "package_backed": sum(1 for i in items if i.get("package_backed")),
            "jurisdiction": dict(Counter(i["jurisdiction_bucket"] for i in items)),
            "source": dict(Counter(i["source_bucket"] for i in items)),
            "line_bucket": dict(Counter(i["line_bucket"] for i in items)),
            "category": dict(Counter(i["category_bucket"] for i in items)),
        },
        "count": len(items),
        "opportunity_ids": [i["opportunity_id"] for i in items],
        "items": items,
        "sample_count_equals_unique_ids": len(items) == len(set(i["opportunity_id"] for i in items)),
        "why_short_of_500": why_short,
    }
    _save(CORPUS, corpus)
    # Slim items for checkpoint size — keep ids + key meta
    return corpus


def load_corpus() -> dict[str, Any]:
    existing = _load(CORPUS)
    if existing.get("immutable") and existing.get("opportunity_ids"):
        if existing.get("count") == len(existing.get("opportunity_ids") or []):
            return existing
    return select_corpus()
