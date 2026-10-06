"""OpenGovBidPriceIndex — persistent cross-buyer bid tabulation index."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from evidence_breakthrough.opengov_history import (
    build_or_load_buyer_history,
    parse_opengov_opportunity_id,
)
from m3_data_root import data_path

log = logging.getLogger("govtracker.scale_evidence_profit.bid_index")

_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9\-/\.]{2,}", re.I)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def index_path() -> Path:
    return data_path("opengov_bid_price_index.json")


def load_index() -> dict[str, Any]:
    p = index_path()
    if not p.exists():
        return {
            "kind": "OpenGovBidPriceIndex",
            "build": "20261003-m3-scale-evidence-to-profit-v1",
            "records": [],
            "by_pn": {},
            "by_model": {},
            "by_desc": {},
            "buyers_indexed": [],
            "updated_at": None,
        }
    return json.loads(p.read_text(encoding="utf-8"))


def save_index(idx: dict[str, Any]) -> None:
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    idx["updated_at"] = now_utc().isoformat()
    p.write_text(json.dumps(idx, indent=2, default=str), encoding="utf-8")


def _record_from_priced_line(ln: dict[str, Any], government_code: str) -> dict[str, Any]:
    pn = ln.get("part_number")
    desc = ln.get("description") or ""
    return {
        "buyer": ln.get("buyer") or government_code,
        "government_code": government_code,
        "project_id": ln.get("project_id"),
        "project_title": ln.get("project_title"),
        "solicitation_number": ln.get("financial_id"),
        "award_date": ln.get("closed_at"),
        "vendor": ln.get("winning_vendor"),
        "line_description": desc,
        "part_number": pn,
        "quantity": ln.get("quantity"),
        "uom": ln.get("uom"),
        "bid_unit_price": ln.get("unit_price"),
        "extended": ln.get("extended"),
        "bidder_count": ln.get("bidder_count"),
        "all_priced_vendors": ln.get("all_priced_vendors") or [],
        "source_url": ln.get("source_url"),
        "source": "opengov_public_bid_tabulation",
        "pn_norm": _norm(pn),
        "desc_tokens": [t for t in (_norm(x) for x in _TOKEN.findall(desc)) if len(t) >= 4 and re.search(r"\d", t)][:8],
    }


def rebuild_index_from_buyer_caches(*, force: bool = False) -> dict[str, Any]:
    """Merge all OpenGovBuyerHistoryProfile caches into OpenGovBidPriceIndex."""
    idx = load_index() if not force else {
        "kind": "OpenGovBidPriceIndex",
        "build": "20261003-m3-scale-evidence-to-profit-v1",
        "records": [],
        "by_pn": {},
        "by_model": {},
        "by_desc": {},
        "buyers_indexed": [],
        "updated_at": None,
    }
    root = data_path("opengov_buyer_history")
    if not root.exists():
        save_index(idx)
        return idx

    # Dedup key
    seen: set[str] = set()
    for rec in idx.get("records") or []:
        key = f"{rec.get('government_code')}|{rec.get('project_id')}|{rec.get('pn_norm')}|{rec.get('bid_unit_price')}|{rec.get('line_description')}"
        seen.add(key)

    buyers = set(idx.get("buyers_indexed") or [])
    for path in sorted(root.glob("*.json")):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        code = str(profile.get("government_code") or path.stem).lower()
        for ln in profile.get("priced_lines") or []:
            if not isinstance(ln, dict) or not ln.get("unit_price"):
                continue
            rec = _record_from_priced_line(ln, code)
            key = f"{code}|{rec.get('project_id')}|{rec.get('pn_norm')}|{rec.get('bid_unit_price')}|{rec.get('line_description')}"
            if key in seen:
                continue
            seen.add(key)
            ri = len(idx["records"])
            idx["records"].append(rec)
            pn = rec.get("pn_norm")
            if pn and len(pn) >= 3:
                idx.setdefault("by_pn", {}).setdefault(pn, []).append(ri)
            for tok in rec.get("desc_tokens") or []:
                idx.setdefault("by_model", {}).setdefault(tok, []).append(ri)
        buyers.add(code)

    idx["buyers_indexed"] = sorted(buyers)
    idx["record_count"] = len(idx["records"])
    save_index(idx)
    return idx


def mine_missing_buyers(
    government_codes: list[str],
    *,
    max_buyers: int = 40,
    max_awarded_detail: int = 12,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Build buyer history for codes not yet indexed; refresh global index."""
    idx = rebuild_index_from_buyer_caches()
    have = set(idx.get("buyers_indexed") or [])
    missing = [c for c in government_codes if c and c.lower() not in have]
    missing = missing[:max_buyers]
    mined = 0
    with_prices = 0
    client = httpx.Client(timeout=40.0)
    try:
        for i, code in enumerate(missing):
            if on_progress:
                on_progress(phase="MINE_BUYER_HISTORY", pct=int(5 + 40 * i / max(len(missing), 1)), buyer=code)
            try:
                profile = build_or_load_buyer_history(
                    code,
                    client=client,
                    max_projects=30,
                    max_awarded_detail=max_awarded_detail,
                )
                mined += 1
                if profile.get("priced_lines"):
                    with_prices += 1
            except Exception as exc:
                log.warning("mine buyer %s failed: %s", code, type(exc).__name__)
            time.sleep(0.05)
    finally:
        client.close()
    idx = rebuild_index_from_buyer_caches(force=True)
    return {
        "missing_requested": len(missing),
        "mined": mined,
        "with_prices": with_prices,
        "index_records": len(idx.get("records") or []),
        "buyers_indexed": len(idx.get("buyers_indexed") or []),
    }


def lookup_matches(
    idx: dict[str, Any],
    *,
    part_number: str | None = None,
    model: str | None = None,
    manufacturer: str | None = None,
    description: str | None = None,
    government_code: str | None = None,
    current_project_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank historical priced records for an identity."""
    records = idx.get("records") or []
    by_pn = idx.get("by_pn") or {}
    by_model = idx.get("by_model") or {}
    hits: dict[int, dict[str, Any]] = {}

    def _add(ri: int, grade: str, score: int) -> None:
        if ri < 0 or ri >= len(records):
            return
        prev = hits.get(ri)
        if prev is None or score > prev["score"]:
            hits[ri] = {"record": records[ri], "match_grade": grade, "score": score}

    def _discriminative(token: str) -> bool:
        # Short numeric tokens like 406/504 collide across catalogs.
        if len(token) >= 6:
            return True
        if len(token) >= 5 and re.search(r"[A-Z]", token):
            return True
        return False

    def _mfr_in_rec(rec: dict[str, Any], mfr_s: str) -> bool:
        if not mfr_s or len(mfr_s) < 3:
            return False
        blob = f"{rec.get('line_description') or ''} {rec.get('manufacturer') or ''} {rec.get('part_number') or ''}".upper()
        return mfr_s[:4] in blob or mfr_s in blob

    pn = _norm(part_number)
    md = _norm(model)
    mfr = (manufacturer or "").strip().upper()
    if pn and _discriminative(pn):
        for ri in by_pn.get(pn) or []:
            _add(ri, "EXACT_PN", 100)
    elif pn and mfr:
        # Short PN only when manufacturer also appears on the historical line
        for ri in by_pn.get(pn) or []:
            if _mfr_in_rec(records[ri], mfr):
                _add(ri, "EXACT_PN", 92)
    if md and md != pn:
        if _discriminative(md):
            for ri in by_pn.get(md) or []:
                _add(ri, "EXACT_MODEL_AS_PN", 95)
            for ri in by_model.get(md) or []:
                _add(ri, "EXACT_MODEL_TOKEN", 90)
        elif mfr:
            for ri in by_pn.get(md) or []:
                if _mfr_in_rec(records[ri], mfr):
                    _add(ri, "EXACT_MODEL_AS_PN", 88)
            for ri in by_model.get(md) or []:
                if _mfr_in_rec(records[ri], mfr):
                    _add(ri, "EXACT_MODEL_TOKEN", 85)

    # Avoid O(N) full-index scans at corpus scale. Exact both-sides uses PN/model indexes only.
    # Optional fuzzy grades only when indexed hits are empty and token is discriminative.
    if not hits and mfr and (pn or md) and _discriminative(pn or md or ""):
        needle = pn or md
        # Bound scan: only records sharing the PN/model token via by_model secondary
        cand_ris = set(by_pn.get(needle) or []) | set(by_model.get(needle) or [])
        for ri in list(cand_ris)[:50]:
            rec = records[ri]
            blob = f"{rec.get('line_description') or ''} {rec.get('part_number') or ''}".upper()
            if mfr[:4] in blob and needle and needle in _norm(blob):
                _add(ri, "MFR_MODEL_IN_DESC", 80)

    ranked = sorted(hits.values(), key=lambda h: -h["score"])
    # Boost same buyer / demote same project for acquisition diversity
    for h in ranked:
        rec = h["record"]
        if government_code and str(rec.get("government_code") or "").lower() == government_code.lower():
            h["score"] += 15
            h["same_buyer"] = True
        else:
            h["same_buyer"] = False
        if current_project_id and str(rec.get("project_id") or "") == str(current_project_id):
            h["same_project"] = True
            h["score"] -= 5
        else:
            h["same_project"] = False
    ranked.sort(key=lambda h: -h["score"])
    return ranked[:limit]


def buyers_from_identity_store() -> list[str]:
    from evidence_breakthrough.corpus import load_identity_store

    store = load_identity_store()
    codes: list[str] = []
    seen: set[str] = set()
    # Prefer buyers with more usable identities
    counts: dict[str, int] = {}
    for oid, pack in (store.get("by_opportunity") or {}).items():
        code, _ = parse_opengov_opportunity_id(oid)
        if not code:
            continue
        n = sum(1 for i in (pack.get("identities") or []) if i.get("confidence_grade") in {"A", "B", "C"})
        counts[code] = counts.get(code, 0) + n
    for code, _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes
