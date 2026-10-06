"""OpenGov buyer history — closed/awarded projects + public bid tabulations."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.evidence_breakthrough.opengov_history")

API_ROOT = "https://api.procurement.opengov.com/api/v1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9\-/\.]{2,}", re.I)


def _headers(government_code: str) -> dict[str, str]:
    code = (government_code or "portal").strip().lower()
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://procurement.opengov.com",
        "Referer": f"https://procurement.opengov.com/portal/{code}/",
    }


def _cache_path(government_code: str) -> Path:
    return data_path("opengov_buyer_history", f"{government_code}.json")


def _norm_token(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def parse_opengov_opportunity_id(opportunity_id: str) -> tuple[str | None, str | None]:
    """Return (government_code, project_id) from opengov:{code}:{id}."""
    parts = str(opportunity_id or "").split(":")
    if len(parts) >= 3 and parts[0].lower() == "opengov":
        return parts[1].strip().lower() or None, parts[2].strip() or None
    return None, None


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def list_public_projects(
    government_code: str,
    *,
    limit: int = 50,
    offset: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    url = f"{API_ROOT}/government/{government_code}/project/public"
    own = client is None
    c = client or httpx.Client(timeout=40.0)
    try:
        r = c.post(url, json={"limit": limit, "offset": offset}, headers=_headers(government_code))
        if r.status_code >= 500:
            return {"ok": False, "retryable": True, "status_code": r.status_code, "rows": []}
        if r.status_code >= 400:
            return {"ok": False, "retryable": False, "status_code": r.status_code, "rows": []}
        data = r.json()
        rows = data.get("rows") if isinstance(data, dict) else data
        return {
            "ok": True,
            "rows": rows if isinstance(rows, list) else [],
            "count": (data.get("count") if isinstance(data, dict) else len(rows or [])),
            "status_code": r.status_code,
        }
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return {"ok": False, "retryable": True, "error": type(exc).__name__, "rows": []}
    finally:
        if own:
            c.close()


def fetch_project(project_id: str | int, government_code: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    url = f"{API_ROOT}/project/{project_id}"
    own = client is None
    c = client or httpx.Client(timeout=40.0)
    try:
        r = c.get(url, headers=_headers(government_code))
        if r.status_code == 404:
            return {"ok": False, "status_code": 404, "project": None}
        if r.status_code >= 500:
            return {"ok": False, "retryable": True, "status_code": r.status_code, "project": None}
        if r.status_code >= 400:
            return {"ok": False, "retryable": False, "status_code": r.status_code, "project": None}
        return {"ok": True, "status_code": r.status_code, "project": r.json()}
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return {"ok": False, "retryable": True, "error": type(exc).__name__, "project": None}
    finally:
        if own:
            c.close()


def extract_bid_tab_lines(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten public bid tabulation rows into priced historical line records."""
    out: list[dict[str, Any]] = []
    if not isinstance(project, dict):
        return out
    if not project.get("isPublicBidPricingResult") and not project.get("showBidsWithPricing"):
        # Still try if bidResults present
        if not project.get("bidResults"):
            return out
    br = project.get("bidResults") or {}
    proposals = br.get("proposalsData") or []
    closed_at = project.get("closedAt") or project.get("solicitationClosedDate")
    title = project.get("title")
    financial_id = project.get("financialId")
    pid = project.get("id")
    gov = (project.get("government") or {}) if isinstance(project.get("government"), dict) else {}
    gov_code = gov.get("code")
    gov_name = gov.get("name") or gov.get("displayName")

    for tab in br.get("bidTabulations") or []:
        if not isinstance(tab, dict):
            continue
        for row in tab.get("rows") or []:
            if not isinstance(row, dict) or row.get("isHeaderRow"):
                continue
            responses = row.get("vendorResponses") or []
            priced: list[dict[str, Any]] = []
            for i, vr in enumerate(responses):
                if not isinstance(vr, dict) or vr.get("noBid"):
                    continue
                up = _f(vr.get("unitPrice"))
                if up is None or up <= 0:
                    continue
                vendor = None
                if i < len(proposals) and isinstance(proposals[i], dict):
                    vendor = proposals[i].get("vendorName")
                priced.append(
                    {
                        "unit_price": up,
                        "vendor": vendor,
                        "quantity": _f(vr.get("quantity")) or _f(row.get("quantity")),
                        "custom1": vr.get("custom1"),
                        "custom2": vr.get("custom2"),
                        "discount": _f(vr.get("discount")),
                    }
                )
            if not priced:
                continue
            # Prefer lowest positive unit price as historical paid / competitive reference
            best = min(priced, key=lambda x: x["unit_price"])
            pn = best.get("custom1") or best.get("custom2") or row.get("custom1") or row.get("custom2")
            # Also pull from priceItems mirror if present
            desc = row.get("description") or ""
            qty = _f(row.get("quantity"))
            uom = row.get("unitToMeasure")
            out.append(
                {
                    "project_id": pid,
                    "project_title": title,
                    "financial_id": financial_id,
                    "closed_at": closed_at,
                    "government_code": gov_code,
                    "buyer": gov_name,
                    "line_item": row.get("lineItem"),
                    "description": desc,
                    "part_number": pn,
                    "quantity": qty,
                    "uom": uom,
                    "unit_price": best["unit_price"],
                    "extended": (best["unit_price"] * qty) if qty else None,
                    "winning_vendor": best.get("vendor"),
                    "bidder_count": len(priced),
                    "all_priced_vendors": priced,
                    "source": "opengov_public_bid_tabulation",
                    "source_url": (
                        f"https://procurement.opengov.com/portal/{gov_code}/projects/{pid}"
                        if gov_code and pid
                        else None
                    ),
                    "match_keys": _match_keys(desc, pn),
                }
            )
    return out


def _match_keys(description: str | None, part_number: str | None) -> list[str]:
    keys: list[str] = []
    pn = _norm_token(part_number)
    if pn and len(pn) >= 3:
        keys.append(f"pn:{pn}")
    for tok in _TOKEN.findall(description or ""):
        nt = _norm_token(tok)
        if len(nt) >= 4 and nt not in {k.split(":", 1)[-1] for k in keys}:
            # skip pure words without digits for pn-like
            if re.search(r"\d", nt):
                keys.append(f"tok:{nt}")
    # description fingerprint (significant alnum tokens)
    words = [w for w in re.findall(r"[A-Z0-9]{3,}", (description or "").upper()) if not w.isdigit() or len(w) >= 5]
    if words:
        keys.append("desc:" + "|".join(words[:6]))
    return keys


def build_or_load_buyer_history(
    government_code: str,
    *,
    max_projects: int = 40,
    max_awarded_detail: int = 15,
    force: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Build OpenGovBuyerHistoryProfile with priced historical lines."""
    code = (government_code or "").strip().lower()
    path = _cache_path(code)
    if path.exists() and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            # Reuse any completed scan, including zero-yield (avoid repeat crawl)
            if cached.get("last_verified") and "historical_projects_scanned" in cached:
                return cached
        except Exception:
            pass

    profile: dict[str, Any] = {
        "kind": "OpenGovBuyerHistoryProfile",
        "government_code": code,
        "historical_projects_scanned": 0,
        "award_records_found": 0,
        "bid_tabs_found": 0,
        "price_schedules_found": 0,
        "working_history_routes": [],
        "last_verified": now_utc().isoformat(),
        "projects": [],
        "priced_lines": [],
        "index": {},  # key -> [line indices]
        "errors": [],
        "retryable": False,
    }
    if not code:
        profile["errors"].append("NO_GOVERNMENT_CODE")
        return profile

    own = client is None
    c = client or httpx.Client(timeout=40.0)
    try:
        listed: list[dict[str, Any]] = []
        offset = 0
        while len(listed) < max_projects:
            batch = list_public_projects(code, limit=min(50, max_projects - len(listed)), offset=offset, client=c)
            if not batch.get("ok"):
                profile["errors"].append(f"list:{batch.get('status_code') or batch.get('error')}")
                profile["retryable"] = bool(batch.get("retryable"))
                break
            rows = [r for r in (batch.get("rows") or []) if isinstance(r, dict)]
            if not rows:
                break
            listed.extend(rows)
            offset += len(rows)
            if len(rows) < 20:
                break
            time.sleep(0.15)

        # Prefer closed/awarded, then evaluation, then others
        def _rank(p: dict[str, Any]) -> tuple[int, str]:
            st = str(p.get("status") or "").lower()
            sub = str(p.get("closedSubstatus") or "").lower()
            if st == "closed" and sub == "awarded":
                return (0, str(p.get("closedAt") or ""))
            if st == "closed":
                return (1, str(p.get("closedAt") or ""))
            if st == "evaluation":
                return (2, str(p.get("proposalDeadline") or ""))
            return (3, str(p.get("proposalDeadline") or ""))

        listed.sort(key=_rank)
        detail_n = 0
        for meta in listed:
            if detail_n >= max_awarded_detail:
                break
            # Skip open projects for history (unless evaluating with public results)
            st = str(meta.get("status") or "").lower()
            if st == "open":
                continue
            pid = meta.get("id")
            if not pid:
                continue
            detail_n += 1
            profile["historical_projects_scanned"] += 1
            fr = fetch_project(pid, code, client=c)
            if not fr.get("ok"):
                if fr.get("retryable"):
                    profile["retryable"] = True
                profile["errors"].append(f"project:{pid}:{fr.get('status_code') or fr.get('error')}")
                time.sleep(0.1)
                continue
            proj = fr["project"]
            profile["projects"].append(
                {
                    "id": pid,
                    "title": proj.get("title"),
                    "financial_id": proj.get("financialId"),
                    "status": proj.get("status"),
                    "closed_substatus": proj.get("closedSubstatus"),
                    "closed_at": proj.get("closedAt"),
                    "is_public_bid_pricing": bool(proj.get("isPublicBidPricingResult")),
                }
            )
            lines = extract_bid_tab_lines(proj)
            if lines:
                profile["bid_tabs_found"] += 1
                profile["award_records_found"] += len(lines)
                if "opengov_public_bid_tabulation" not in profile["working_history_routes"]:
                    profile["working_history_routes"].append("opengov_public_bid_tabulation")
                for ln in lines:
                    idx = len(profile["priced_lines"])
                    profile["priced_lines"].append(ln)
                    for k in ln.get("match_keys") or []:
                        profile["index"].setdefault(k, []).append(idx)
            # priceTables with non-null unit prices (rare on closed)
            for pt in proj.get("priceTables") or []:
                for it in (pt.get("priceItems") or []) if isinstance(pt, dict) else []:
                    if isinstance(it, dict) and _f(it.get("unitPrice")):
                        profile["price_schedules_found"] += 1
            time.sleep(0.12)

        profile["last_verified"] = now_utc().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
        return profile
    finally:
        if own:
            c.close()


def search_buyer_history(
    profile: dict[str, Any],
    *,
    part_number: str | None = None,
    model: str | None = None,
    manufacturer: str | None = None,
    description: str | None = None,
    financial_id: str | None = None,
    title: str | None = None,
) -> list[dict[str, Any]]:
    """Rank matching historical priced lines from a buyer profile."""
    lines = profile.get("priced_lines") or []
    index = profile.get("index") or {}
    hits: dict[int, dict[str, Any]] = {}

    def _add(idx: int, grade: str, score: int) -> None:
        if idx < 0 or idx >= len(lines):
            return
        prev = hits.get(idx)
        if prev is None or score > prev["score"]:
            hits[idx] = {"line": lines[idx], "match_grade": grade, "score": score}

    pn = _norm_token(part_number)
    md = _norm_token(model)
    if pn:
        for idx in index.get(f"pn:{pn}") or []:
            _add(idx, "EXACT_PN", 100)
        for idx in index.get(f"tok:{pn}") or []:
            _add(idx, "EXACT_PN_TOKEN", 95)
    if md and md != pn:
        for idx in index.get(f"pn:{md}") or []:
            _add(idx, "EXACT_MODEL", 90)
        for idx in index.get(f"tok:{md}") or []:
            _add(idx, "EXACT_MODEL_TOKEN", 85)

    # Solicitation-level: same financial id / title on any priced line's project
    fid = (financial_id or "").strip().lower()
    title_n = re.sub(r"\s+", " ", (title or "").strip().lower())
    if fid or title_n:
        for i, ln in enumerate(lines):
            if fid and str(ln.get("financial_id") or "").strip().lower() == fid:
                _add(i, "SAME_SOLICITATION", 70)
            pt = re.sub(r"\s+", " ", str(ln.get("project_title") or "").strip().lower())
            if title_n and pt and (title_n in pt or pt in title_n):
                _add(i, "SAME_TITLE", 60)

    # Manufacturer + model in description
    mfr = (manufacturer or "").strip().upper()
    if mfr and (md or pn):
        needle = md or pn
        for i, ln in enumerate(lines):
            blob = f"{ln.get('description') or ''} {ln.get('part_number') or ''}".upper()
            if mfr[:4] in blob and needle and needle in _norm_token(blob):
                _add(i, "MFR_MODEL_IN_DESC", 80)

    # Description token overlap
    desc_words = set(re.findall(r"[A-Z0-9]{4,}", (description or "").upper()))
    if desc_words:
        for i, ln in enumerate(lines):
            lw = set(re.findall(r"[A-Z0-9]{4,}", str(ln.get("description") or "").upper()))
            overlap = desc_words & lw
            if len(overlap) >= 3:
                _add(i, "DESC_OVERLAP", 40 + min(20, len(overlap) * 3))

    ranked = sorted(hits.values(), key=lambda x: -x["score"])
    return ranked
