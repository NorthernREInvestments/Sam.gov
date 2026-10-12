"""Route A — Government selling-price-first commercial research.

Historical unit prices → product identity → live opportunity match → acquisition → economics.
No fabricated prices. SAM prefer 0. AUTO_SEND remains false.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from buyer_intelligence.commercial_budget_control_v1 import dashboard_payload, is_paid_research_paused
from buyer_intelligence.commercial_gap_closure_v1 import (
    _price_evidence_compatible,
    classify_opportunity_completeness,
    research_mpn_public_price,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.first_revenue_validation_sprint_v1 import _days_to_deadline
from buyer_intelligence.product_line_store_v1 import get_entry, get_lines, load_store
from buyer_intelligence.research_novelty_engine_v1 import evidence_version_hash, record_research_attempt, should_revisit
from buyer_intelligence.revenue_strategy_engine_v1 import (
    assign_lane_from_contribution,
    calculate_remaining_contribution,
)
from m3_data_root import data_path

log = logging.getLogger("govtracker.selling_price_first_v1")

BUILD = "20261012-m3-selling-price-first-v1"
OUT = "_og_commercial_research_pipeline_out"
STORE = f"{OUT}/m3_spf_v1_historical_unit_price_store.json"
REPORT = f"{OUT}/m3_spf_v1_report.json"
MATCHES = f"{OUT}/m3_spf_v1_live_matches.json"
CHECKPOINT = f"{OUT}/m3_spf_v1_checkpoint.json"

_DOLLAR = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})")
_RESTRICTED = re.compile(
    r"\b(ammunition|ammo|firearm|weapon|hornady|ekahau|subscription license|software license)\b",
    re.I,
)
_LOW_FRICTION = re.compile(
    r"\b(paper|tissue|towel|janitorial|office|ppe|glove|filter|lamp|led|bolt|screw|battery|oil|brake|"
    r"wiper|toner|soap|cleaner|trash|liner|tape|cable|wire|outlet|switch|tool)\b",
    re.I,
)


def _ckpt(**kwargs: Any) -> None:
    cur = _load(CHECKPOINT) or {"kind": "spf_checkpoint", "build": BUILD}
    cur.update(kwargs)
    cur["at"] = _utc()
    _save(CHECKPOINT, cur)


def _match_level(same_buyer: bool, exact: bool, spec: bool) -> str:
    if exact and same_buyer:
        return "EXACT_PRODUCT_SAME_BUYER"
    if exact:
        return "EXACT_PRODUCT_OTHER_BUYER"
    if spec and same_buyer:
        return "SPEC_MATCH_SAME_BUYER"
    if spec:
        return "SPEC_MATCH_OTHER_BUYER"
    return "CATEGORY_CONTEXT_ONLY"


def _usable(level: str) -> bool:
    return level in {
        "EXACT_PRODUCT_SAME_BUYER",
        "EXACT_PRODUCT_OTHER_BUYER",
        "SPEC_MATCH_SAME_BUYER",
        "SPEC_MATCH_OTHER_BUYER",
        "SPECIFICATION_MATCHED_UNIT_PRICE",
    }


def harvest_historical_unit_prices(*, limit: int = 200) -> dict[str, Any]:
    """Mine cached commercial/historical artifacts for line-level unit prices — no SAM."""
    root = Path(data_path(OUT))
    local_data = Path(data_path("."))
    patterns = (
        "*historical*",
        "*fpd*",
        "*award*",
        "*profit*",
        "*bid*tab*",
        "*gometro*",
        "*hcso*",
        "*multibuyer*",
        "*opengov*profit*",
        "*source_product*",
    )
    files: list[Path] = []
    for base in (root, local_data):
        if not base.exists():
            continue
        for pat in patterns:
            files.extend(base.glob(pat))
            files.extend(base.glob(f"**/{pat}"))
    records: list[dict[str, Any]] = []
    seen = set()
    for path in files:
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        if path.stat().st_size > 60_000_000:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            blob = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        records.extend(_extract_from_blob(blob, path.name))
        if len(records) >= limit * 3:
            break

    # Deduplicate by buyer+mpn+unit_price+source
    uniq = []
    uk = set()
    for r in records:
        k = (
            str(r.get("buyer") or "").lower(),
            str(r.get("mpn") or "").upper(),
            str(r.get("unit_price")),
            str(r.get("source_file")),
        )
        if k in uk:
            continue
        uk.add(k)
        uniq.append(r)

    # Prefer usable exact/spec + low-friction categories
    def score(r: dict[str, Any]) -> int:
        s = 0
        if _usable(str(r.get("match_level") or "")):
            s += 50
        if r.get("mpn"):
            s += 20
        desc = str(r.get("description") or r.get("product") or "")
        if _LOW_FRICTION.search(desc):
            s += 25
        if _RESTRICTED.search(desc):
            s -= 40
        if r.get("quantity") not in (None, "", 0, "0"):
            s += 10
        return s

    uniq.sort(key=score, reverse=True)
    out = {
        "kind": "spf_historical_unit_price_store",
        "build": BUILD,
        "at": _utc(),
        "source_files_scanned": len(seen),
        "raw_extracted": len(records),
        "unique": len(uniq),
        "usable_count": sum(1 for r in uniq if _usable(str(r.get("match_level") or ""))),
        "records": uniq[:limit],
        "NOTE": "Benchmarks only — not guaranteed future award prices; SAM not consumed",
    }
    _save(STORE, out)
    return out


def _extract_from_blob(blob: Any, source_file: str, *, depth: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if depth > 6:
        return out
    if isinstance(blob, dict):
        # Known FPD / HCSO style
        hist = blob.get("HISTORICAL_GOV_PRICING") or blob.get("historical_gov_pricing")
        if isinstance(hist, dict):
            buyer = blob.get("BUYER") or blob.get("buyer") or "UNKNOWN"
            cls = hist.get("CLASS") or hist.get("class") or "SPEC_MATCH_OTHER_BUYER"
            # Nested bid bands
            for key, val in hist.items():
                if not isinstance(val, dict):
                    continue
                for band_key in ("low_bid", "award_band", "mid_range"):
                    band = val.get(band_key)
                    if band is None:
                        continue
                    prices = band if isinstance(band, list) else [band]
                    for p in prices[:3]:
                        try:
                            unit = Decimal(str(p).replace(",", "").replace("$", ""))
                        except Exception:
                            continue
                        if unit < 1 or unit > 100000:
                            continue
                        out.append(
                            {
                                "buyer": buyer,
                                "product": key,
                                "description": key.replace("_", " "),
                                "mpn": None,
                                "unit_price": str(unit),
                                "quantity": None,
                                "uom": "CASE" if "CASE" in key.upper() or "TOWEL" in key.upper() or "TISSUE" in key.upper() else "EA",
                                "match_level": cls if _usable(str(cls)) else "SPEC_MATCH_OTHER_BUYER",
                                "source_file": source_file,
                                "source_url": hist.get("URL") or hist.get("url"),
                                "award_date": hist.get("award_date"),
                                "winning_vendor": hist.get("winning_vendor"),
                                "newly_acquired": True,
                                "route": "CACHED_ARTIFACT_BAND",
                                "low_friction": bool(_LOW_FRICTION.search(key)),
                                "restricted": bool(_RESTRICTED.search(key)),
                            }
                        )
        # Generic line-like dicts
        mpn = blob.get("MPN") or blob.get("mpn") or blob.get("part_number") or blob.get("manufacturer_part_number")
        unit = blob.get("unit_price") or blob.get("awarded_unit_price") or blob.get("bid_unit_price") or blob.get("UNIT_PRICE")
        if unit is not None and (mpn or blob.get("DESCRIPTION") or blob.get("description") or blob.get("product")):
            try:
                up = Decimal(str(unit).replace(",", "").replace("$", ""))
            except Exception:
                up = None
            if up is not None and Decimal("0.5") <= up <= Decimal("100000"):
                desc = str(blob.get("DESCRIPTION") or blob.get("description") or blob.get("product") or "")
                exact = bool(mpn)
                level = _match_level(False, exact, not exact and len(desc) >= 20)
                out.append(
                    {
                        "buyer": blob.get("BUYER") or blob.get("buyer") or blob.get("agency"),
                        "product": desc[:120] or str(mpn),
                        "description": desc[:200],
                        "mpn": str(mpn).strip().upper() if mpn else None,
                        "unit_price": str(up),
                        "quantity": blob.get("QTY") or blob.get("quantity") or blob.get("qty"),
                        "uom": blob.get("UOM") or blob.get("uom"),
                        "match_level": level,
                        "source_file": source_file,
                        "source_url": blob.get("url") or blob.get("URL") or blob.get("source_url"),
                        "award_date": blob.get("award_date") or blob.get("date"),
                        "winning_vendor": blob.get("vendor") or blob.get("awardee"),
                        "newly_acquired": True,
                        "route": "CACHED_LINE_DICT",
                        "low_friction": bool(_LOW_FRICTION.search(desc)),
                        "restricted": bool(_RESTRICTED.search(desc)),
                    }
                )
        for v in blob.values():
            if isinstance(v, (dict, list)):
                out.extend(_extract_from_blob(v, source_file, depth=depth + 1))
    elif isinstance(blob, list):
        for item in blob[:500]:
            out.extend(_extract_from_blob(item, source_file, depth=depth + 1))
    return out


def match_to_live_opportunities(records: list[dict[str, Any]], *, limit: int = 25) -> dict[str, Any]:
    store = load_store(force=True)
    by_mpn: dict[str, list[str]] = {}
    by_token: dict[str, list[str]] = {}
    for oid, e in (store.get("by_opportunity") or {}).items():
        if str(oid).startswith("__"):
            continue
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 1:
            continue
        title = str(e.get("TITLE") or "")
        if re.search(r"\b(service|construction|install|renovation)\b", title, re.I):
            continue
        lines = e.get("LINES") or get_lines(oid)
        for ln in lines:
            mpn = str(ln.get("MPN") or "").strip().upper()
            if mpn:
                by_mpn.setdefault(mpn, []).append(oid)
            desc = str(ln.get("DESCRIPTION") or "").lower()
            for tok in re.findall(r"[a-z]{4,}", desc)[:8]:
                by_token.setdefault(tok, []).append(oid)

    matches = []
    for rec in records[:limit]:
        mpn = str(rec.get("mpn") or "").upper()
        oids = list(dict.fromkeys(by_mpn.get(mpn, []))) if mpn else []
        if not oids:
            desc = str(rec.get("description") or rec.get("product") or "").lower()
            toks = re.findall(r"[a-z]{4,}", desc)
            cand: Counter[str] = Counter()
            for t in toks[:10]:
                for oid in by_token.get(t, [])[:20]:
                    cand[oid] += 1
            oids = [oid for oid, c in cand.most_common(5) if c >= 2]
            level = "SPEC_MATCH_OTHER_BUYER" if oids else "NO_LIVE_MATCH"
        else:
            level = "EXACT_PRODUCT_OTHER_BUYER"
        live = []
        for oid in oids[:5]:
            e = get_entry(oid) or {}
            comp = classify_opportunity_completeness(oid)
            live.append(
                {
                    "opportunity_id": oid,
                    "buyer": e.get("BUYER"),
                    "title": e.get("TITLE"),
                    "deadline": e.get("DEADLINE"),
                    "days_left": _days_to_deadline(e.get("DEADLINE")),
                    "full_mandatory": comp.get("full_mandatory_coverage"),
                    "mandatory_complete": comp.get("mandatory_complete"),
                    "mandatory_count": comp.get("mandatory_count"),
                    "match_basis": level,
                }
            )
        matches.append(
            {
                **{k: rec.get(k) for k in ("buyer", "product", "mpn", "unit_price", "quantity", "uom", "match_level", "source_file", "source_url", "low_friction", "restricted")},
                "live_matches": live,
                "live_match_count": len(live),
            }
        )
    out = {
        "kind": "spf_live_matches",
        "build": BUILD,
        "at": _utc(),
        "researched": len(matches),
        "with_live_match": sum(1 for m in matches if m["live_match_count"]),
        "matches": matches,
    }
    _save(MATCHES, out)
    return out


def research_acquisition_for_matches(
    matches: list[dict[str, Any]],
    *,
    max_prices: int = 25,
    per_item_s: float = 30,
) -> dict[str, Any]:
    """For matched live opps, research acquisition on matching MPNs — free/cached only."""
    import time

    results = []
    priced = 0
    t_all = time.time()
    for m in matches:
        if priced >= max_prices:
            break
        if not m.get("live_matches"):
            continue
        if m.get("restricted"):
            # keep for later; skip first-revenue acquisition burn
            results.append({**m, "acquisition": None, "skip_reason": "RESTRICTED_CATEGORY_DEPRIORITIZED"})
            continue
        mpn = m.get("mpn")
        for live in m["live_matches"][:2]:
            oid = live["opportunity_id"]
            lines = get_lines(oid)
            target_lines = [ln for ln in lines if mpn and str(ln.get("MPN") or "").upper() == str(mpn).upper()]
            if not target_lines:
                # first MPN line for low-friction
                target_lines = [ln for ln in lines if ln.get("MPN")][:1]
            for ln in target_lines[:2]:
                if priced >= max_prices:
                    break
                eh = evidence_version_hash(get_entry(oid) or {})
                gate = should_revisit(
                    oid,
                    task="SPF_ACQUISITION_PRICE",
                    method="public_price_search_v1",
                    sources=["public_html"],
                    current_evidence_hash=eh,
                )
                if not gate.get("allowed"):
                    results.append({"opportunity_id": oid, "mpn": ln.get("MPN"), "acquisition": None, "skip_reason": "NOVELTY_SUPPRESSED", "gate": gate})
                    continue
                pr = research_mpn_public_price(
                    mpn=str(ln.get("MPN")),
                    manufacturer=ln.get("MANUFACTURER") or ln.get("BRAND"),
                    description=ln.get("DESCRIPTION"),
                    opportunity_id=oid,
                    qty=ln.get("QTY"),
                    uom=ln.get("UOM"),
                    deadline_s=per_item_s,
                )
                ok = False
                if pr.get("unit_price"):
                    ok, reason = _price_evidence_compatible(
                        mpn=str(ln.get("MPN")),
                        description=ln.get("DESCRIPTION"),
                        manufacturer=ln.get("MANUFACTURER"),
                        url=pr.get("url"),
                        supplier=str(pr.get("supplier") or ""),
                    )
                    if not ok:
                        pr = {**pr, "unit_price": None, "failure_class": reason, "newly_acquired": False}
                if pr.get("unit_price") and pr.get("newly_acquired"):
                    priced += 1
                gov = m.get("unit_price")
                acq = pr.get("unit_price")
                contrib = None
                lane = None
                if gov and acq:
                    try:
                        # preliminary unit contribution only — qty may unknown
                        selling = Decimal(str(gov))
                        acquisition = Decimal(str(acq))
                        qty = ln.get("QTY") or m.get("quantity") or 1
                        contrib = calculate_remaining_contribution(
                            selling_price=(selling * Decimal(str(qty))).quantize(Decimal("0.01")),
                            acquisition_cost=(acquisition * Decimal(str(qty))).quantize(Decimal("0.01")),
                            freight=None,
                            financing_fees=None,
                        )
                        # Force CONDITIONAL because historical ≠ guaranteed award
                        if contrib.get("CONTRIBUTION_STATE") == "VERIFIED_CONTRIBUTION":
                            contrib["CONTRIBUTION_STATE"] = "CONDITIONAL_CONTRIBUTION"
                            contrib["NOTE"] = "Historical gov unit price is benchmark only"
                        lane = assign_lane_from_contribution(contrib)
                    except Exception:
                        contrib = None
                record_research_attempt(
                    oid,
                    task="SPF_ACQUISITION_PRICE",
                    method="public_price_search_v1",
                    sources=[pr.get("route") or "none"],
                    status=str(pr.get("status") or "NO_NEW_EVIDENCE"),
                    prices_recovered=1 if pr.get("unit_price") and pr.get("newly_acquired") else 0,
                    evidence_hash=eh,
                )
                results.append(
                    {
                        "opportunity_id": oid,
                        "buyer": live.get("buyer"),
                        "title": live.get("title"),
                        "deadline": live.get("deadline"),
                        "historical_buyer": m.get("buyer"),
                        "historical_unit_price": gov,
                        "historical_match_level": m.get("match_level"),
                        "historical_source": m.get("source_file"),
                        "mpn": ln.get("MPN"),
                        "qty": ln.get("QTY"),
                        "acquisition": pr,
                        "economics": contrib,
                        "lane": lane,
                        "low_friction": m.get("low_friction"),
                    }
                )
    out = {
        "kind": "spf_acquisition_bridge",
        "build": BUILD,
        "at": _utc(),
        "new_acquisition_prices": priced,
        "results": results,
        "elapsed_s": round(time.time() - t_all, 2),
        "budget": dashboard_payload(),
    }
    _save(f"{OUT}/m3_spf_v1_acquisition_bridge.json", out)
    return out


def run_selling_price_first(*, harvest_limit: int = 100, cohort: int = 25) -> dict[str, Any]:
    _ckpt(stage="START")
    if is_paid_research_paused():
        return {"status": "PAUSED", "reason": "M3_PAID_RESEARCH_PAUSED"}
    harvested = harvest_historical_unit_prices(limit=harvest_limit)
    usable = [r for r in harvested.get("records") or [] if _usable(str(r.get("match_level") or ""))]
    # Prefer low-friction usable records for cohort
    preferred = [r for r in usable if r.get("low_friction") and not r.get("restricted")]
    cohort_recs = (preferred + [r for r in usable if r not in preferred])[:cohort]
    if len(cohort_recs) < cohort:
        # fill with best remaining even if category-context (but mark non-economic)
        rest = [r for r in harvested.get("records") or [] if r not in cohort_recs]
        cohort_recs.extend(rest[: max(0, cohort - len(cohort_recs))])
    _ckpt(stage="MATCH", cohort=len(cohort_recs))
    matched = match_to_live_opportunities(cohort_recs, limit=cohort)
    _ckpt(stage="ACQUISITION")
    bridge = research_acquisition_for_matches(matched.get("matches") or [], max_prices=cohort)
    conditional_pos = 0
    for r in bridge.get("results") or []:
        econ = r.get("economics") or {}
        if econ.get("CONTRIBUTION_STATE") == "CONDITIONAL_CONTRIBUTION":
            try:
                if float(str(econ.get("REMAINING_CONTRIBUTION") or 0) or 0) > 0:
                    conditional_pos += 1
            except Exception:
                pass
    report = {
        "kind": "m3_selling_price_first_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "cohort": "A_SELLING_PRICE_FIRST",
        "harvested_unique": harvested.get("unique"),
        "usable_historical_unit_prices": harvested.get("usable_count"),
        "researched": len(cohort_recs),
        "live_matches": matched.get("with_live_match"),
        "new_acquisition_prices": bridge.get("new_acquisition_prices"),
        "conditional_positive": conditional_pos,
        "verified_profitable": 0,
        "records_sample": cohort_recs[:15],
        "match_sample": (matched.get("matches") or [])[:15],
        "bridge_sample": (bridge.get("results") or [])[:15],
        "budget": dashboard_payload(),
        "sam_calls": 0,
        "openai_usd": "0",
        "NOTE": "Historical unit prices are benchmarks; contribution marked CONDITIONAL only",
    }
    _save(REPORT, report)
    _ckpt(stage="DONE", report_summary={k: report[k] for k in ("researched", "live_matches", "new_acquisition_prices", "conditional_positive")})
    return report
