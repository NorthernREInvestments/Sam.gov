"""Free-source adapters. Documented vs experimentally verified tracked in registry."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from m3.research.http_util import fetch_json, fetch_text
from m3.research.schema_map import (
    AGENCY_CANDIDATES,
    CATEGORY_CANDIDATES,
    DEADLINE_CANDIDATES,
    NUMBER_CANDIDATES,
    POSTED_CANDIDATES,
    STATUS_CANDIDATES,
    TITLE_CANDIDATES,
    URL_CANDIDATES,
    VALUE_CANDIDATES,
    pick_first_key,
)

PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s\-.]?)?\(?([2-9]\d{2})\)?[\s\-.]?([2-9]\d{2})[\s\-.]?(\d{4})(?!\d)")
VALUE_RANGE_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?\s*([\d,]+(?:\.\d+)?)",
    re.I,
)
VALUE_SINGLE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _guess_category(title: str) -> str | None:
    t = title.lower()
    mapping = [
        ("furniture", r"furniture|desk|chair|cubicle|seating"),
        ("janitorial", r"janitorial|custodial|cleaning suppl|paper towel|toilet paper"),
        ("it_hardware", r"laptop|computer|chromebook|server|networking|printer|monitor"),
        ("appliances", r"appliance|refrigerat|washer|dryer|freezer|ice machine"),
        ("electrical", r"electrical|breaker|conduit|lighting|wire|cable"),
        ("plumbing", r"plumbing|faucet|valve|pipe"),
        ("tools_mro", r"\btools?\b|mro|fastener|industrial suppl|lubricant|oil"),
        ("safety_ppe", r"ppe|safety equipment|respirator|glove|hard hat"),
        ("office_supplies", r"office suppl|toner|paper cup|stationery"),
        ("vehicles_parts", r"vehicle|tire|automotive|parts"),
    ]
    for cat, pat in mapping:
        if re.search(pat, t):
            return cat
    return None


def _parse_money(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val)
    m = VALUE_RANGE_RE.search(s)
    if m:
        try:
            return (float(m.group(1).replace(",", "")) + float(m.group(2).replace(",", ""))) / 2.0
        except ValueError:
            pass
    m = re.search(r"([\d,]+(?:\.\d+)?)", s.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_value_band(val: Any) -> tuple[float | None, float | None]:
    """Parse published ranges like '$50,001 - $100,000' into min/max."""
    if val is None:
        return None, None
    if isinstance(val, (int, float)):
        f = float(val)
        return f, f
    s = str(val)
    m = VALUE_RANGE_RE.search(s)
    if m:
        try:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", ""))
            return lo, hi
        except ValueError:
            return None, None
    single = _parse_money(s)
    return single, single


def _extract_url(val: Any, fallback: str | None = None) -> str | None:
    if isinstance(val, dict):
        u = val.get("url") or val.get("href")
        if u:
            return str(u)
    if isinstance(val, str) and val.startswith("http"):
        return val
    return fallback


def _row_get(row: dict, *keys: str) -> Any:
    for k in keys:
        if k and k in row and row[k] not in (None, ""):
            return row[k]
    return None


def search_socrata(
    source: dict,
    *,
    keywords: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    page_size: int = 500,
    max_pages: int | None = None,
    apply_adapter_keywords: bool = False,
) -> dict[str, Any]:
    """Stage 1 unrestricted solicitation retrieval (status filters only).

    Build 002B invariant: no product/keyword/category/business suitability gates.
    Classification happens centrally after normalization/dedup.
    """
    from m3.config import get_settings

    settings = get_settings()
    page_size = min(500, max(50, page_size or settings.research_socrata_page_size))
    # 0 / None → use configured safeguard (default high); never a silent 3-page cap
    configured = int(max_pages if max_pages is not None else settings.research_max_pages_per_source)
    max_pages = max(1, configured if configured > 0 else 500)
    soft_limit = int(limit) if limit else None
    cfg = dict(source.get("config") or {})
    urls = [cfg.get("dataset_url") or source.get("base_url")]
    urls += list(cfg.get("fallback_urls") or [])
    items: list[dict] = []
    errors: list[str] = []
    used_url = None
    pages_fetched = 0
    total_rows_seen = 0
    mapping_failures = 0
    closed_excluded = 0
    truncated = False
    more_available = False
    pagination_complete = False
    page_offset = max(0, int(offset or 0))
    prefer_statuses = [s.lower() for s in (cfg.get("prefer_statuses") or [])]
    keywords = list(keywords or [])
    # Runtime field resolution from config + universal candidates
    title_keys = tuple(
        k
        for k in (cfg.get("title_field"), *TITLE_CANDIDATES)
        if k
    )
    status_keys = tuple(k for k in (cfg.get("status_field"), *STATUS_CANDIDATES) if k)
    agency_keys = tuple(k for k in (cfg.get("agency_field"), *AGENCY_CANDIDATES) if k)
    number_keys = tuple(k for k in (cfg.get("number_field"), *NUMBER_CANDIDATES) if k)
    deadline_keys = tuple(k for k in (cfg.get("deadline_field"), *DEADLINE_CANDIDATES) if k)
    posted_keys = tuple(k for k in (cfg.get("posted_field"), *POSTED_CANDIDATES) if k)
    value_keys = tuple(k for k in (cfg.get("value_field"), *VALUE_CANDIDATES) if k)
    category_keys = tuple(k for k in (cfg.get("category_field"), *CATEGORY_CANDIDATES) if k)
    url_keys = tuple(k for k in (cfg.get("url_field"), *URL_CANDIDATES) if k)

    def _append_row(row: dict, final: str) -> None:
        nonlocal items, mapping_failures, closed_excluded
        if not isinstance(row, dict):
            return
        title_key = pick_first_key(row, title_keys, cfg.get("title_field"))
        title = str((row.get(title_key) if title_key else "") or "").strip()
        if not title:
            best = ""
            for k, v in row.items():
                if not isinstance(v, str):
                    continue
                kl = str(k).lower()
                if kl.startswith(":") or "url" in kl or "id" in kl or "date" in kl:
                    continue
                if 12 <= len(v.strip()) <= 400 and len(v.strip()) > len(best):
                    best = v.strip()
            title = best
        if not title or len(title) < 5:
            mapping_failures += 1
            return
        status_raw = str(_row_get(row, *status_keys) or "")
        # Source-level active-status filter only (permitted). No product/category gates.
        if prefer_statuses:
            if status_raw and status_raw.lower() not in [s.lower() for s in prefer_statuses]:
                closed_excluded += 1
                return
        elif status_raw and re.search(
            r"^(closed|award|awarded|cancelled|canceled|inactive|expired)$", status_raw, re.I
        ):
            closed_excluded += 1
            return
        if "cancelledpostponed" in row and row.get("cancelledpostponed"):
            closed_excluded += 1
            return
        category_raw = str(_row_get(row, *category_keys) or "")
        # prefer_categories / exclude_categories intentionally ignored in Stage 1 (Build 002B)
        agency = str(_row_get(row, *agency_keys) or cfg.get("buyer_type") or "Unknown")
        sol = str(_row_get(row, *number_keys) or "")
        deadline_raw = _row_get(row, *deadline_keys)
        posted_raw = _row_get(row, *posted_keys)
        value_raw = _row_get(row, *value_keys)
        vmin, vmax = _parse_value_band(value_raw)
        set_aside_raw = _row_get(row, cfg.get("set_aside_field") or "set_aside", "mwbe", "lsbrpindicator")
        set_aside = "unknown"
        set_aside_verified = False
        if set_aside_raw is not None:
            smap = cfg.get("set_aside_map") or {}
            mapped = smap.get(str(set_aside_raw))
            if mapped:
                set_aside = mapped
                set_aside_verified = True
            else:
                set_aside = str(set_aside_raw)
                set_aside_verified = False
        cat = _guess_category(f"{title} {category_raw}")
        proc_type = str(
            _row_get(row, cfg.get("type_field") or "procurement_type", "type")
            or cfg.get("procurement_type_default")
            or "unknown"
        )
        qty = _row_get(row, "quantity", "qty", "estimated_quantity")
        sol_status = "unknown"
        if cfg.get("is_forecast"):
            sol_status = "forecast"
        elif status_raw:
            if re.search(r"(?i)amend", status_raw):
                sol_status = "amended"
            elif re.search(r"(?i)\b(open|active|posted|advertised)\b", status_raw):
                sol_status = "open"
            elif re.search(r"(?i)cancel", status_raw):
                sol_status = "cancelled"
            elif re.search(r"(?i)award", status_raw):
                sol_status = "historical_award"
            elif re.search(r"(?i)closed|expired|inactive", status_raw):
                sol_status = "closed"
        # Prefer mapped detail URL over dataset resource / listing fallback
        detail_from_row = _extract_url(_row_get(row, *url_keys), None)
        listing_fallback = cfg.get("official_listing_url") or final
        official = detail_from_row or listing_fallback
        source_purpose = (
            cfg.get("dataset_purpose")
            or cfg.get("record_purpose")
            or (
                "forecast"
                if cfg.get("is_forecast")
                else (
                    "historical_award"
                    if cfg.get("is_historical")
                    else "individual_solicitation_candidate"
                )
            )
        )
        items.append(
            {
                "external_key": _key(source["source_key"], sol or title, official or ""),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": agency[:255],
                "geo_state": cfg.get("geo_state"),
                "geo_city": None,
                "geo_location": agency[:255],
                "deadline_at": str(deadline_raw) if deadline_raw else None,
                "deadline_timezone": "UTC" if deadline_raw else None,
                "posted_at": str(posted_raw)[:10] if posted_raw else None,
                "procurement_type": proc_type[:120],
                "product_category": cat or ("unknown" if not category_raw else category_raw[:80].lower()),
                "set_aside": set_aside if isinstance(set_aside, str) else "unknown",
                "set_aside_verified": set_aside_verified,
                "estimated_value_min": vmin,
                "estimated_value_max": vmax,
                "estimated_value_unknown": vmin is None and vmax is None,
                "value_estimate_method": (
                    "published_estimate_band"
                    if value_raw and vmin is not None
                    else ("published_amount" if vmin is not None else None)
                ),
                "value_evidence": str(value_raw)[:200] if value_raw else None,
                "value_evidence_class": ("published_estimate" if value_raw else "not_yet_researched"),
                "official_url": (official or final)[:2048],
                "discovery_url": (final or "")[:2048],
                "documents_available": "yes" if official and "http" in (official or "") else "unknown",
                "quantity": str(qty) if qty not in (None, "") else None,
                # Classification deferred to central Stage 3; identity applied later
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": sol_status,
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "is_forecast": bool(cfg.get("is_forecast")),
                "source_purpose": source_purpose,
                "title_field_used": title_key,
                "discovery_stage": "unrestricted",
                "evidence": {
                    "row_keys": list(row.keys())[:20],
                    "source_url": final,
                    "status": status_raw or None,
                    "category_raw": category_raw or None,
                    "title_field_used": title_key,
                    "detail_url_mapped": bool(detail_from_row and "resource/" not in (detail_from_row or "")),
                },
            }
        )

    for url in urls:
        if not url:
            continue
        try:
            page_offset = max(0, int(offset or 0))
            hit_empty = False
            for _page in range(max_pages):
                if soft_limit is not None and len(items) >= soft_limit:
                    truncated = True
                    more_available = True
                    break
                q_url = f"{url}?$limit={page_size}&$offset={page_offset}"
                order_field = cfg.get("posted_field") or cfg.get("deadline_field")
                if order_field:
                    q_url += f"&$order={order_field} DESC"
                status, final, data = fetch_json(q_url)
                pages_fetched += 1
                if status >= 400 or not isinstance(data, list):
                    errors.append(f"{url} status={status} offset={page_offset}")
                    break
                used_url = final
                if not data:
                    hit_empty = True
                    pagination_complete = True
                    more_available = False
                    break
                total_rows_seen += len(data)
                for row in data:
                    if soft_limit is not None and len(items) >= soft_limit:
                        truncated = True
                        more_available = True
                        break
                    _append_row(row, final)
                if len(data) < page_size:
                    pagination_complete = True
                    more_available = False
                    break
                page_offset += page_size
                more_available = True
            else:
                # Exhausted max_pages without empty page → incomplete unless soft-limited
                if not hit_empty and not truncated:
                    more_available = True
                    pagination_complete = False
            if items or pagination_complete:
                break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    out_items = items if soft_limit is None else items[:soft_limit]
    return {
        "ok": bool(out_items) or (pagination_complete and total_rows_seen > 0 and not errors),
        "items": out_items,
        "source_key": source["source_key"],
        "used_url": used_url,
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": pages_fetched,
        "rows_examined": total_rows_seen,
        "truncated": truncated,
        "more_available": more_available,
        "pagination_complete": pagination_complete and not more_available,
        "next_offset": page_offset if more_available else None,
        "stats": {
            "mapping_failures": mapping_failures,
            "closed_excluded": closed_excluded,
            "accepted": len(out_items),
            "product_gates_removed": True,
        },
    }



def search_html_links(
    source: dict,
    *,
    keywords: list[str] | None = None,
    limit: int = 30,
    apply_adapter_keywords: bool = False,
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    urls = [cfg.get("list_url") or source.get("base_url")]
    urls += list(cfg.get("extra_urls") or [])
    hints = [h.lower() for h in (cfg.get("link_hints") or ["bid", "rfp"])]
    reject_pats = [re.compile(p, re.I) for p in (cfg.get("reject_title_patterns") or [])]
    items: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    keywords = list(keywords or [])

    for url in urls:
        if not url or len(items) >= limit:
            continue
        try:
            status, final, html = fetch_text(url)
            if status >= 400:
                errors.append(f"{url} status={status}")
                continue
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
            texts = re.findall(r"<a[^>]*>(.*?)</a>", html, flags=re.I | re.S)
            for i, href in enumerate(hrefs):
                text = texts[i] if i < len(texts) else ""
                label = re.sub(r"<[^>]+>", "", text or "").strip()
                abs_url = urljoin(final, href)
                if abs_url in seen:
                    continue
                blob = f"{label} {abs_url}".lower()
                if not any(h in blob for h in hints):
                    continue
                # Skip pure templates / forms
                if any(p.search(label) or p.search(abs_url) for p in reject_pats):
                    continue
                # Prefer document/pdf/html solicitation pages over generic site chrome
                path = urlparse(abs_url).path.lower()
                if path.endswith((".css", ".js", ".png", ".jpg", ".gif", ".svg")):
                    continue
                # Stage 1: no keyword / product / service exclusions
                title = label or abs_url.rsplit("/", 1)[-1]
                title = re.sub(r"\s+", " ", title).strip()
                if len(title) < 6:
                    continue
                seen.add(abs_url)
                is_doc = path.endswith((".pdf", ".doc", ".docx", ".xlsx", ".xls", ".zip"))
                items.append(
                    {
                        "external_key": _key(source["source_key"], abs_url),
                        "title": title[:500],
                        "solicitation_number": None,
                        "buyer_name": source.get("name"),
                        "geo_state": cfg.get("geo_state"),
                        "geo_city": None,
                        "geo_location": cfg.get("geo_state"),
                        "deadline_at": None,
                        "deadline_timezone": None,
                        "posted_at": None,
                        "procurement_type": "unknown",
                        "product_category": _guess_category(title) or "unknown",
                        "set_aside": "unknown",
                        "set_aside_verified": False,
                        "estimated_value_min": None,
                        "estimated_value_max": None,
                        "estimated_value_unknown": True,
                        "value_estimate_method": None,
                        "value_evidence": None,
                        "value_evidence_class": "not_yet_researched",
                        "official_url": abs_url[:2048],
                        "discovery_url": (final or "")[:2048],
                        "documents_available": "yes" if is_doc else "unknown",
                        "quantity": None,
                        "requirement_class": "pending_classification",
                        "classification_evidence": ["deferred_to_central_classifier"],
                        "solicitation_status": "unknown",
                        "source_key": source["source_key"],
                        "source_verified_at": _utc(),
                        "source_purpose": cfg.get("record_purpose")
                        or ("supporting_document" if is_doc else "html_link_harvest"),
                        "discovery_stage": "unrestricted",
                        "evidence": {"list_url": final, "harvested_href": True},
                    }
                )
                if len(items) >= limit:
                    break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    return {
        "ok": bool(items),
        "items": items,
        "source_key": source["source_key"],
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": len([u for u in urls if u]),
        "rows_examined": len(seen) or len(items),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "mapping_failures": 0},
    }


def search_georgia_gpr(
    source: dict,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Georgia Procurement Registry — live-verify public eventDetails (+ attachments).

    Listing via DataTables `./eventSearch` returns HTTP 400 for non-browser clients.
    Bootstrap event IDs may come from an operational snapshot; every retained row is
    re-fetched from official eventDetails HTML (status/title/buyer/docs). Snapshot
    IDs are never emitted without live verification.
    """
    from pathlib import Path

    cfg = source.get("config") or {}
    errors: list[str] = []
    items: list[dict] = []
    snap_path = Path(__file__).resolve().parent / "snapshots" / "georgia_gpr_open_goods_v1.json"
    events: list[dict] = []
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            events = list(snap.get("events") or [])
        except Exception as exc:
            errors.append(f"snapshot_read:{type(exc).__name__}")
    # Optional config overrides (URLs only — still live-verified)
    for u in cfg.get("bootstrap_detail_urls") or []:
        if isinstance(u, str) and "eventDetails" in u:
            events.append({"detail_url": u, "event_id": None, "title": None})

    verified = 0
    closed = 0
    candidates = events[: int(limit)] if limit else list(events)
    for ev in candidates:
        if limit and len(items) >= limit:
            break
        url = ev.get("detail_url") or ""
        if not url and ev.get("event_id"):
            sst = ev.get("source_system_type") or "gpr20"
            url = (
                "https://ssl.doas.state.ga.us/gpr/eventDetails?"
                f"eSourceNumber={ev['event_id']}&sourceSystemType={sst}"
            )
        if not url.startswith("http"):
            continue
        try:
            status, final, html = fetch_text(url, timeout=18)
            if status >= 400 or not html:
                errors.append(f"{url} status={status}")
                continue
            text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            # Status
            if re.search(r"(?i)\b(awarded|cancelled|closed|completed)\b", text) and not re.search(
                r"(?i)\bOpen\b", text[:3000]
            ):
                closed += 1
                continue
            title = ev.get("title") or ""
            hm = re.search(r"(?is)<h[12][^>]*>([\s\S]*?)</h[12]>", html)
            if hm:
                ht = re.sub(r"<[^>]+>", " ", hm.group(1))
                ht = re.sub(r"\s+", " ", ht).strip()
                if ht and ht.lower() not in {"announcements", "georgia procurement registry"}:
                    title = ht
            if not title or len(title) < 6:
                continue
            eid = ev.get("event_id") or ""
            m = re.search(r"eSourceNumber=([^&]+)", final or url, re.I)
            if m:
                eid = m.group(1)
            buyer = ev.get("buyer") or "Georgia public entity"
            bm = re.search(r"(?i)([A-Z0-9].{0,80}(?:County|City Of|Board Of|University|Department Of)[^<]{0,40})", text)
            if bm:
                buyer = re.sub(r"\s+", " ", bm.group(1)).strip()[:200]
            has_docs = bool(re.search(r"downloadAttachment", html or "", re.I))
            items.append(
                {
                    "external_key": _key(source["source_key"], eid or final),
                    "title": title[:500],
                    "solicitation_number": eid or None,
                    "buyer_name": buyer,
                    "geo_state": "GA",
                    "geo_city": None,
                    "geo_location": "GA",
                    "deadline_at": ev.get("deadline"),
                    "deadline_timezone": "America/New_York",
                    "posted_at": None,
                    "procurement_type": "unknown",
                    "product_category": _guess_category(title) or "unknown",
                    "set_aside": "unknown",
                    "set_aside_verified": False,
                    "estimated_value_min": None,
                    "estimated_value_max": None,
                    "estimated_value_unknown": True,
                    "value_estimate_method": None,
                    "value_evidence": None,
                    "value_evidence_class": "not_yet_researched",
                    "official_url": (final or url)[:2048],
                    "discovery_url": "https://ssl.doas.state.ga.us/gpr/",
                    "documents_available": "yes" if has_docs else "unknown",
                    "quantity": None,
                    "requirement_class": "pending_classification",
                    "classification_evidence": ["deferred_to_central_classifier"],
                    "solicitation_status": "open",
                    "source_key": source["source_key"],
                    "source_verified_at": _utc(),
                    "source_purpose": "individual_solicitation_candidate",
                    "discovery_stage": "unrestricted",
                    "verified_individual_solicitation": True,
                    "record_type": "INDIVIDUAL_SOLICITATION",
                    "evidence": {
                        "detail_url": final or url,
                        "live_event_details": True,
                        "documents_linked": has_docs,
                        "listing_api": "eventSearch_blocked_non_browser_400",
                    },
                }
            )
            verified += 1
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}")

    return {
        "ok": bool(items),
        "items": items,
        "source_key": source["source_key"],
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": verified,
        "rows_examined": len(events),
        "pagination_complete": False,  # full eventSearch population not accessible without browser
        "more_available": True,
        "stats": {
            "accepted": len(items),
            "closed_or_inactive": closed,
            "mapping_failures": 0,
            "listing_api_blocked": True,
            "bootstrap_events": len(events),
        },
    }


_NE_SPB_LIST_URL = "https://das.nebraska.gov/materiel/bid-opportunities.html"
_NE_SPB_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"consulting|staffing|case\s+management|evaluation\s+services|"
    r"construction|renovation|maintenance|janitorial|professional\s+services|"
    r"managed\s+services|training\s+services|audit\s+services"
    r")\b"
)


def _parse_ne_spb_opening(raw: str) -> str | None:
    """Parse Opening column like '01/5/27' or '11/24/26' → ISO date (date only)."""
    s = (raw or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return None


def _ne_spb_cell_text(td_html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", td_html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_nebraska_spb_listing_html(html: str, *, list_url: str = _NE_SPB_LIST_URL) -> list[dict[str, Any]]:
    """Parse Current Bid Opportunities table rows from das.nebraska.gov."""
    # Prefer the Current Bid Opportunities table body
    section = html
    msec = re.search(
        r"(?is)Current Bid Opportunities.*?<tbody>([\s\S]*?)</tbody>",
        html or "",
    )
    if msec:
        section = msec.group(1)
    else:
        mb = re.search(r"(?is)<tbody>([\s\S]*?)</tbody>", html or "")
        section = mb.group(1) if mb else (html or "")

    rows: list[dict[str, Any]] = []
    for tr in re.findall(r"(?is)<tr[^>]*>([\s\S]*?)</tr>", section):
        tds = re.findall(r"(?is)<td[^>]*>([\s\S]*?)</td>", tr)
        if len(tds) < 7:
            continue
        am = re.search(r'href=["\']([^"\']+)["\']', tds[1], flags=re.I)
        if not am:
            continue
        href = am.group(1).strip()
        if "materiel/purchasing/" not in href.replace("\\", "/").lower():
            continue
        title = _ne_spb_cell_text(tds[1])
        if not title or len(title) < 3:
            continue
        detail = urljoin(list_url, href)
        rows.append(
            {
                "posted": _ne_spb_cell_text(tds[0]),
                "title": title[:500],
                "category_nigp": _ne_spb_cell_text(tds[2]) if len(tds) > 2 else "",
                "opening": _ne_spb_cell_text(tds[3]) if len(tds) > 3 else "",
                "bid_type": _ne_spb_cell_text(tds[4]) if len(tds) > 4 else "",
                "buyer": _ne_spb_cell_text(tds[5]) if len(tds) > 5 else "",
                "solicitation_number": _ne_spb_cell_text(tds[6]) if len(tds) > 6 else "",
                "agency": _ne_spb_cell_text(tds[7]) if len(tds) > 7 else "State Purchasing Bureau",
                "updated": _ne_spb_cell_text(tds[8]) if len(tds) > 8 else "",
                "detail_url": detail,
            }
        )
    return rows


def search_nebraska_spb(
    source: dict,
    *,
    limit: int | None = None,
    fetch_detail_docs: bool = True,
) -> dict[str, Any]:
    """Nebraska State Purchasing Bureau — public bid opportunities (free, no login).

    Listing: https://das.nebraska.gov/materiel/bid-opportunities.html
    Detail pages under /materiel/purchasing/{sol}/… with Word/Excel project documents.
    Same product-first rules as M3: skip obvious services; keep ITB goods buys.
    """
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or _NE_SPB_LIST_URL)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    try:
        status, final, html = fetch_text(list_url, timeout=30)
    except Exception as exc:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_fetch:{type(exc).__name__}:{exc}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }
    if status >= 400 or not html:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_status={status}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }

    rows = parse_nebraska_spb_listing_html(html, list_url=final or list_url)
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        sol = row.get("solicitation_number") or ""
        bid_type = row.get("bid_type") or ""
        category = row.get("category_nigp") or ""
        blob = f"{title} {category} {bid_type}"
        skip = listing_skip_before_detail_reason(title)
        if skip or _NE_SPB_SERVICEISH_RE.search(blob):
            skipped += 1
            continue
        # Prefer commodity ITBs; still allow goods-looking RFQs if not serviceish
        if re.search(r"(?i)request\s+for\s+proposal", bid_type) and not re.search(
            r"(?i)purchase|supply|furnish|commodity|salt|fuel|vehicle|equipment|material",
            title,
        ):
            skipped += 1
            continue

        detail_url = row["detail_url"]
        doc_urls: list[str] = []
        project_description = ""
        if fetch_detail_docs:
            try:
                dstat, dfinal, dhtml = fetch_text(detail_url, timeout=25)
                if dstat < 400 and dhtml:
                    detail_url = dfinal or detail_url
                    for href in re.findall(r'href=["\']([^"\']+)["\']', dhtml, flags=re.I):
                        low = href.lower()
                        if any(low.endswith(ext) for ext in (".docx", ".doc", ".xlsx", ".xls", ".pdf", ".zip")):
                            if "public_record" in low:
                                continue
                            doc_urls.append(urljoin(detail_url, href))
                    dm = re.search(
                        r"(?is)PROJECT DESCRIPTION:\s*</[^>]+>\s*<[^>]+>([\s\S]*?)</(?:p|div|td)",
                        dhtml,
                    )
                    if not dm:
                        dm = re.search(
                            r"(?is)purpose of selecting a qualified Bidder to provide ([^.<]{5,200})",
                            dhtml,
                        )
                    if dm:
                        project_description = _ne_spb_cell_text(dm.group(1))[:500]
            except Exception as exc:
                errors.append(f"detail:{sol or title[:40]}:{type(exc).__name__}")

        opening_iso = _parse_ne_spb_opening(row.get("opening") or "")
        posted_iso = _parse_ne_spb_opening(row.get("posted") or "")
        items.append(
            {
                "external_key": _key(source["source_key"], sol or detail_url, title),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": (row.get("agency") or "Nebraska State Purchasing Bureau")[:200],
                "geo_state": "NE",
                "geo_city": None,
                "geo_location": "NE",
                "deadline_at": opening_iso,
                "deadline_timezone": "America/Chicago",
                "posted_at": posted_iso,
                "procurement_type": bid_type or "unknown",
                "product_category": _guess_category(title) or _guess_category(category) or "unknown",
                "nigp_category": category or None,
                "set_aside": "unknown",
                "set_aside_verified": False,
                "estimated_value_min": None,
                "estimated_value_max": None,
                "estimated_value_unknown": True,
                "value_estimate_method": None,
                "value_evidence": None,
                "value_evidence_class": "not_yet_researched",
                "official_url": detail_url[:2048],
                "discovery_url": final or list_url,
                "documents_available": "yes" if doc_urls else "unknown",
                "document_urls": doc_urls,
                "quantity": None,
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": "open",
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "source_purpose": "individual_solicitation_candidate",
                "discovery_stage": "unrestricted",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "buyer_contact": row.get("buyer") or None,
                "summary": project_description or None,
                "evidence": {
                    "detail_url": detail_url,
                    "live_listing": True,
                    "live_detail": bool(fetch_detail_docs),
                    "documents_linked": len(doc_urls),
                    "bid_type": bid_type,
                    "opening_raw": row.get("opening"),
                },
            }
        )

    return {
        "ok": bool(items) or (len(rows) > 0 and skipped == len(rows)),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": 1 + (len(items) if fetch_detail_docs else 0),
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "mapping_failures": 0,
            "listing_rows": len(rows),
        },
    }


_NJ_PURCHASE_LIST_URL = "https://www.nj.gov/treasury/purchase/"
_NJ_NOTICE_RE = re.compile(
    r"(?i)^\s*NOTICE\s+OF\s+BID\s+SOLICITATION"
    r"(?:\s*[-–—]\s*BID\s+AMENDMENT)?\s*:\s*"
    r"([A-Z0-9]+)\s*[-–—]\s*(.+?)\s*$"
)
_NJ_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"case\s+management|consulting|staffing|construction|renovation|"
    r"professional\s+services|training\s+services|maintenance\s+services|"
    r"inspection\s+maintenance|re-?entry\s+programming|fiscal\s+intermediary|"
    r"pharmacy\s+benefit|behavioral\s+risk|workforce\s+development|"
    r"remedial\s+investigation|employee\s+benefits"
    r")\b"
)


def parse_nj_purchase_notice_label(label: str) -> dict[str, Any] | None:
    """Parse NJ Purchase Bureau notice link text into solicitation fields."""
    text = re.sub(r"\s+", " ", (label or "").strip())
    text = (
        text.replace("&amp;", "&")
        .replace("&#039;", "'")
        .replace("&quot;", '"')
    )
    m = _NJ_NOTICE_RE.match(text)
    if not m:
        return None
    sol = m.group(1).strip()
    title = m.group(2).strip()
    is_amendment = bool(re.search(r"(?i)bid\s+amendment", text))
    return {
        "solicitation_number": sol,
        "title": title[:500],
        "is_amendment": is_amendment,
        "notice_label": text[:700],
    }


def parse_nj_purchase_listing_html(
    html: str,
    *,
    list_url: str = _NJ_PURCHASE_LIST_URL,
) -> list[dict[str, Any]]:
    """Parse public NOTICE OF BID SOLICITATION PDF links from NJ Purchase Bureau."""
    notices: list[dict[str, Any]] = []
    for href, lab in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        html or "",
        flags=re.I,
    ):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab or "")).strip()
        parsed = parse_nj_purchase_notice_label(label)
        if not parsed:
            continue
        abs_u = urljoin(list_url, href)
        low = abs_u.lower().split("?", 1)[0]
        if not low.endswith(".pdf"):
            continue
        if "specialnotices" not in low and "notice" not in label.lower():
            # Prefer special notices board; still accept explicit notice PDFs.
            if "purchase" not in low:
                continue
        notices.append(
            {
                **parsed,
                "detail_url": abs_u,
                "document_url": abs_u,
            }
        )
    # Group amendments under the same solicitation number (primary = non-amendment first).
    by_sol: dict[str, dict[str, Any]] = {}
    for n in notices:
        sol = n["solicitation_number"]
        cur = by_sol.get(sol)
        if not cur:
            by_sol[sol] = {
                "solicitation_number": sol,
                "title": n["title"],
                "detail_url": n["detail_url"],
                "document_urls": [n["document_url"]],
                "is_amendment_only": bool(n["is_amendment"]),
                "notice_labels": [n["notice_label"]],
            }
            continue
        if n["document_url"] not in cur["document_urls"]:
            cur["document_urls"].append(n["document_url"])
        cur["notice_labels"].append(n["notice_label"])
        if n["is_amendment"]:
            continue
        # Prefer original notice title/url when present.
        cur["title"] = n["title"]
        cur["detail_url"] = n["detail_url"]
        cur["is_amendment_only"] = False
    return list(by_sol.values())


def search_nj_purchase(
    source: dict,
    *,
    limit: int | None = None,
    fetch_detail_docs: bool = True,
) -> dict[str, Any]:
    """New Jersey Purchase Bureau — public bid solicitation notice PDFs (free).

    Listing: https://www.nj.gov/treasury/purchase/
    Each NOTICE OF BID SOLICITATION is a public PDF (body + primary document).
    """
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or _NJ_PURCHASE_LIST_URL)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    try:
        status, final, html = fetch_text(list_url, timeout=30)
    except Exception as exc:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_fetch:{type(exc).__name__}:{exc}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }
    if status >= 400 or not html:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_status={status}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }

    rows = parse_nj_purchase_listing_html(html, list_url=final or list_url)
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        sol = row.get("solicitation_number") or ""
        skip = listing_skip_before_detail_reason(title)
        if skip or _NJ_SERVICEISH_RE.search(title):
            skipped += 1
            continue
        doc_urls = list(row.get("document_urls") or [])
        # Optional: confirm PDF reachable (cheap HEAD-like GET of bytes meta via fetch_text skip).
        # Body text extraction happens in stepped pipeline / package layer.
        items.append(
            {
                "external_key": _key(source["source_key"], sol or doc_urls[0], title),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": "New Jersey Division of Purchase and Property",
                "geo_state": "NJ",
                "geo_city": None,
                "geo_location": "NJ",
                "deadline_at": None,
                "deadline_timezone": "America/New_York",
                "posted_at": None,
                "procurement_type": "bid_solicitation_notice",
                "product_category": _guess_category(title) or "unknown",
                "nigp_category": None,
                "set_aside": "unknown",
                "set_aside_verified": False,
                "estimated_value_min": None,
                "estimated_value_max": None,
                "estimated_value_unknown": True,
                "value_estimate_method": None,
                "value_evidence": None,
                "value_evidence_class": "not_yet_researched",
                "official_url": (row.get("detail_url") or (doc_urls[0] if doc_urls else list_url))[:2048],
                "discovery_url": final or list_url,
                "documents_available": "yes" if doc_urls else "unknown",
                "document_urls": doc_urls,
                "quantity": None,
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": "open",
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "source_purpose": "individual_solicitation_candidate",
                "discovery_stage": "unrestricted",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "summary": title,
                "evidence": {
                    "detail_url": row.get("detail_url"),
                    "live_listing": True,
                    "live_detail": bool(fetch_detail_docs),
                    "documents_linked": len(doc_urls),
                    "notice_labels": row.get("notice_labels") or [],
                    "amendment_only": bool(row.get("is_amendment_only")),
                },
            }
        )

    return {
        "ok": bool(items) or (len(rows) > 0 and skipped == len(rows)),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "mapping_failures": 0,
            "listing_rows": len(rows),
        },
    }


_TN_CPO_LIST_URL = (
    "https://www.tn.gov/generalservices/procurement/central-procurement-office--cpo-/"
    "supplier-information/request-for-proposals--rfp--opportunities.html"
)
_TN_RFP_FOLDER_RE = re.compile(r"(?i)/rfp-updates/([^/]+)/")
_TN_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"inmate\s+health|health\s+services|admin(?:istrative)?\s+serv|"
    r"consulting|staffing|case\s+management|counseling|training\s+services|"
    r"professional\s+services|burial\s+services|reservation\s+and\s+retail|"
    r"queuing|virtual\s+line|point\s+of\s+sale|"
    r"vision\s+services|life\s+and\s+ad|tpa\b|broker|captive\s+mgmt|"
    r"family\s+support|insurance|software\s+platform|direct\s+certification|"
    r"maintenance\s+rfp|\btitan\s+maintenance\b"
    r")\b"
)


def _tn_title_from_package(sol: str, docs: list[dict[str, Any]]) -> str:
    """Prefer descriptive RFP filename over bare 'RFP {sol}' labels."""
    for d in docs:
        url = str(d.get("url") or "")
        name = unquote(urlparse(url).path.split("/")[-1])
        stem = re.sub(r"\.[^.]+$", "", name)
        if re.search(r"(?i)attachment|amendment|pro[_\s-]?forma|notice", stem):
            continue
        if not re.search(r"(?i)\bRFP\b|^RFP", stem):
            continue
        cleaned = re.sub(rf"(?i)^RFP[_-]?{re.escape(sol)}[_-]?", "", stem)
        cleaned = cleaned.replace("_", " ").strip(" -_")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and cleaned.upper() != sol.upper():
            return cleaned[:500]
    for d in docs:
        lab = str(d.get("label") or "").strip()
        if re.match(rf"(?i)^RFP\s+{re.escape(sol)}$", lab):
            continue
        if lab and not re.match(r"(?i)^(attachment|amendment|appendix|pro\s*forma)\b", lab):
            return lab[:500]
    return f"RFP {sol}"


def parse_tn_cpo_listing_html(
    html: str,
    *,
    list_url: str = _TN_CPO_LIST_URL,
) -> list[dict[str, Any]]:
    """Parse TN CPO RFP opportunity packages grouped by /rfp-updates/{sol}/ folders."""
    by_sol: dict[str, list[dict[str, Any]]] = {}
    for href, lab in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        html or "",
        flags=re.I,
    ):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab or "")).strip()
        label = label.replace("&amp;", "&")
        abs_u = urljoin(list_url, href)
        m = _TN_RFP_FOLDER_RE.search(abs_u)
        if not m:
            continue
        low = abs_u.lower().split("?", 1)[0]
        if not any(low.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".xlsx", ".xls", ".zip")):
            continue
        sol = m.group(1)
        by_sol.setdefault(sol, []).append({"label": label, "url": abs_u})

    rows: list[dict[str, Any]] = []
    for sol, docs in by_sol.items():
        # De-dupe URLs preserving order
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for d in docs:
            if d["url"] in seen:
                continue
            seen.add(d["url"])
            uniq.append(d)
        title = _tn_title_from_package(sol, uniq)
        # Prefer a primary RFP/solicitation file as detail URL
        primary = uniq[0]["url"]
        for d in uniq:
            name = unquote(urlparse(d["url"]).path.split("/")[-1]).lower()
            if name.startswith("rfp") and "attachment" not in name and "amendment" not in name:
                primary = d["url"]
                break
        rows.append(
            {
                "solicitation_number": sol,
                "title": title,
                "detail_url": primary,
                "document_urls": [d["url"] for d in uniq],
                "notice_labels": [d["label"] for d in uniq],
            }
        )
    return rows


def search_tn_cpo(
    source: dict,
    *,
    limit: int | None = None,
    fetch_detail_docs: bool = True,
) -> dict[str, Any]:
    """Tennessee Central Procurement Office — public RFP packages (free).

    Listing: TN CPO RFP Opportunities page with /rfp-updates/{sol}/ document folders.
    """
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    import time

    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or _TN_CPO_LIST_URL)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    status, final, html = 0, list_url, ""
    last_err = ""
    for attempt in range(4):
        try:
            status, final, html = fetch_text(list_url, timeout=35)
            if status < 400 and html:
                break
            last_err = f"list_status={status}"
        except Exception as exc:
            last_err = f"list_fetch:{type(exc).__name__}:{exc}"
            status, final, html = 0, list_url, ""
        time.sleep(0.8 * (attempt + 1))
    if status >= 400 or not html:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [last_err or "list_fetch_failed"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }

    rows = parse_tn_cpo_listing_html(html, list_url=final or list_url)
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        sol = row.get("solicitation_number") or ""
        skip = listing_skip_before_detail_reason(title)
        if skip or _TN_SERVICEISH_RE.search(title):
            skipped += 1
            continue
        doc_urls = list(row.get("document_urls") or [])
        items.append(
            {
                "external_key": _key(source["source_key"], sol or doc_urls[0], title),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": "Tennessee Central Procurement Office",
                "geo_state": "TN",
                "geo_city": None,
                "geo_location": "TN",
                "deadline_at": None,
                "deadline_timezone": "America/Chicago",
                "posted_at": None,
                "procurement_type": "rfp_package",
                "product_category": _guess_category(title) or "unknown",
                "nigp_category": None,
                "set_aside": "unknown",
                "set_aside_verified": False,
                "estimated_value_min": None,
                "estimated_value_max": None,
                "estimated_value_unknown": True,
                "value_estimate_method": None,
                "value_evidence": None,
                "value_evidence_class": "not_yet_researched",
                "official_url": (row.get("detail_url") or (doc_urls[0] if doc_urls else list_url))[:2048],
                "discovery_url": final or list_url,
                "documents_available": "yes" if doc_urls else "unknown",
                "document_urls": doc_urls,
                "quantity": None,
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": "open",
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "source_purpose": "individual_solicitation_candidate",
                "discovery_stage": "unrestricted",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "summary": title,
                "evidence": {
                    "detail_url": row.get("detail_url"),
                    "live_listing": True,
                    "live_detail": bool(fetch_detail_docs),
                    "documents_linked": len(doc_urls),
                    "notice_labels": row.get("notice_labels") or [],
                },
            }
        )

    return {
        "ok": bool(items) or (len(rows) > 0 and skipped == len(rows)),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "mapping_failures": 0,
            "listing_rows": len(rows),
        },
    }


_UW_PROCUREMENT_LIST_URL = (
    "https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices"
)
_UW_SOL_FILE_RE = re.compile(
    r"(?i)/ps/files/purchasing/Solicitations-PostedPurchWebsite/[^\"'\s]+\.(?:pdf|docx?|xlsx?|zip)"
)
# Sol numbers look like 22-0038 or UW-24-0030 (not the whole filename).
_UW_SOL_ID_RE = re.compile(r"(?i)\b(?:UW[-\s]?)?(\d{2}-\d{3,5})\b")
_UW_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"mentor|caregiver|foster\s+parent|training|consulting|staffing|"
    r"catering|food\s+service|professional\s+services|software|"
    r"case\s+management|counseling|lived\s+experience"
    r")\b"
)


def _uw_sol_from_text(text: str) -> str | None:
    m = _UW_SOL_ID_RE.search(text or "")
    if not m:
        return None
    num = m.group(1).upper()
    if re.search(r"(?i)\bUW[-\s]?" + re.escape(m.group(1)), text or ""):
        return f"UW-{num}"
    return num


def _uw_title_from_filename(href: str, sol: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", unquote(urlparse(href or "").path.split("/")[-1] or ""))
    # Drop leading rfq/rfp + sol token(s); keep descriptive remainder.
    stem = re.sub(r"(?i)^(rfq{1,2}|rfp|itb|ifb)[-_]+", "", stem)
    stem = re.sub(r"(?i)^uw[-_]+", "", stem)
    stem = re.sub(rf"(?i)^{re.escape(sol)}[-_]+", "", stem)
    stem = re.sub(r"(?i)^\d{2}-\d{3,5}[-_]+", "", stem)
    stem = re.sub(r"(?i)[-_]?(spanish|revisar)([-_]|$)", "", stem)
    stem = re.sub(r"[-_]+", " ", stem).strip(" -_")
    stem = re.sub(r"\s+", " ", stem).strip()
    # Trailing lone revision digit (…-1) is noise when alone.
    if re.fullmatch(r"\d+", stem or ""):
        return f"UW solicitation {sol}"
    return stem or f"UW solicitation {sol}"


def parse_uw_procurement_notice_label(label: str, *, href: str = "") -> dict[str, Any] | None:
    """Parse UW solicitation link text / filename into solicitation fields."""
    text = re.sub(r"\s+", " ", (label or "").strip())
    text = text.replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"')
    name = unquote(urlparse(href or "").path.split("/")[-1] or "")
    sol = _uw_sol_from_text(text) or _uw_sol_from_text(name)
    if not sol:
        return None
    title = re.sub(r"(?i)^\s*revisar\s+", "", text).strip()
    bare = bool(re.match(r"(?i)^(?:RFQ{1,2}|RFP|ITB|IFB)\s*(?:UW[-\s]?)?\d{2}-\d{3,5}\s*$", title))
    if not title or bare:
        title = _uw_title_from_filename(href, sol)
    return {
        "solicitation_number": sol[:80],
        "title": title[:500],
        "notice_label": text[:700] or title[:700],
    }


def parse_uw_procurement_listing_html(
    html: str,
    *,
    list_url: str = _UW_PROCUREMENT_LIST_URL,
) -> list[dict[str, Any]]:
    """Parse UW Supplier Solicitation Notices — public files under Solicitations-PostedPurchWebsite."""
    by_sol: dict[str, list[dict[str, Any]]] = {}
    for m in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        html or "",
        flags=re.I,
    ):
        href, lab = m.group(1), m.group(2)
        abs_u = urljoin(list_url, href)
        if not _UW_SOL_FILE_RE.search(abs_u):
            continue
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab or "")).strip()
        label = label.replace("&amp;", "&")
        # Preceding cell/paragraph text only (avoid neighboring RFQ anchors).
        start = max(0, m.start() - 450)
        context = re.sub(
            r"\s+", " ", re.sub(r"<[^>]+>", " ", (html or "")[start : m.start()])
        ).strip()
        parsed = parse_uw_procurement_notice_label(label, href=abs_u)
        if not parsed:
            continue
        title = parsed["title"]
        if title.lower().startswith("uw solicitation") and context:
            ctx_clean = re.sub(r"(?i)\b(revisar|rfq{1,2}|rfp|itb)\b", " ", context)
            ctx_clean = re.sub(r"\s+", " ", ctx_clean).strip()
            if len(ctx_clean) > 24:
                title = ctx_clean[-400:]
        sol = parsed["solicitation_number"]
        by_sol.setdefault(sol, []).append(
            {
                "label": parsed["notice_label"] or label,
                "url": abs_u,
                "title": title,
                "context": context[:700],
            }
        )

    rows: list[dict[str, Any]] = []
    for sol, docs in by_sol.items():
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for d in docs:
            if d["url"] in seen:
                continue
            seen.add(d["url"])
            uniq.append(d)
        # Prefer English / non-translation filename as primary title
        title = uniq[0]["title"]
        for d in uniq:
            name = unquote(urlparse(d["url"]).path.split("/")[-1]).lower()
            if "spanish" in name or "revisar" in (d.get("label") or "").lower():
                continue
            if d.get("title"):
                title = d["title"]
                break
        primary = uniq[0]["url"]
        for d in uniq:
            name = unquote(urlparse(d["url"]).path.split("/")[-1]).lower()
            if "spanish" not in name:
                primary = d["url"]
                break
        rows.append(
            {
                "solicitation_number": sol,
                "title": title,
                "detail_url": primary,
                "document_urls": [d["url"] for d in uniq],
                "notice_labels": [d["label"] for d in uniq],
                "contexts": [d.get("context") or "" for d in uniq if d.get("context")],
            }
        )
    return rows


def search_uw_procurement(
    source: dict,
    *,
    limit: int | None = None,
    fetch_detail_docs: bool = True,
) -> dict[str, Any]:
    """University of Washington Procurement — public solicitation notices (free).

    Listing: https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices
    Public RFQ/RFP files under /ps/files/purchasing/Solicitations-PostedPurchWebsite/.
    """
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or _UW_PROCUREMENT_LIST_URL)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    try:
        status, final, html = fetch_text(list_url, timeout=30)
    except Exception as exc:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_fetch:{type(exc).__name__}:{exc}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }
    if status >= 400 or not html:
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_status={status}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }

    rows = parse_uw_procurement_listing_html(html, list_url=final or list_url)
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        sol = row.get("solicitation_number") or ""
        skip = listing_skip_before_detail_reason(title)
        blob = " ".join(
            [
                title,
                " ".join(row.get("notice_labels") or []),
                " ".join(row.get("document_urls") or []),
                " ".join(str(x) for x in (row.get("contexts") or [])),
            ]
        )
        if skip or _UW_SERVICEISH_RE.search(blob):
            skipped += 1
            continue
        doc_urls = list(row.get("document_urls") or [])
        items.append(
            {
                "external_key": _key(source["source_key"], sol or doc_urls[0], title),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": "University of Washington Procurement Services",
                "geo_state": "WA",
                "geo_city": "Seattle",
                "geo_location": "WA",
                "deadline_at": None,
                "deadline_timezone": "America/Los_Angeles",
                "posted_at": None,
                "procurement_type": "solicitation_notice",
                "product_category": _guess_category(title) or "unknown",
                "nigp_category": None,
                "set_aside": "unknown",
                "set_aside_verified": False,
                "estimated_value_min": None,
                "estimated_value_max": None,
                "estimated_value_unknown": True,
                "value_estimate_method": None,
                "value_evidence": None,
                "value_evidence_class": "not_yet_researched",
                "official_url": (row.get("detail_url") or (doc_urls[0] if doc_urls else list_url))[:2048],
                "discovery_url": final or list_url,
                "documents_available": "yes" if doc_urls else "unknown",
                "document_urls": doc_urls,
                "quantity": None,
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": "open",
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "source_purpose": "individual_solicitation_candidate",
                "discovery_stage": "unrestricted",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "summary": title,
                "evidence": {
                    "detail_url": row.get("detail_url"),
                    "live_listing": True,
                    "live_detail": bool(fetch_detail_docs),
                    "documents_linked": len(doc_urls),
                    "notice_labels": row.get("notice_labels") or [],
                },
            }
        )

    return {
        "ok": bool(items) or (len(rows) > 0 and skipped == len(rows)),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "mapping_failures": 0,
            "listing_rows": len(rows),
        },
    }


_DE_DATASET_URL = "https://data.delaware.gov/resource/2hnj-zwix.json"
_DE_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"design\s+services|professional\s+services|consulting|staffing|"
    r"construction|renovation|mechanical\s+renovation|asphalt|paving|"
    r"security\s+&\s+ada|migrant\s+education|audit|microgrid|"
    r"training|case\s+management"
    r")\b"
)


def search_delaware_mmp(
    source: dict,
    *,
    limit: int | None = None,
    fetch_detail_docs: bool = True,
) -> dict[str, Any]:
    """Delaware MMP — open-data list + public bidcondocs packages (free).

    List: data.delaware.gov open bids. Packages: mmp.delaware.gov GetBidDocumentList.
    """
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason
    from m3.research.package import discover_delaware_mmp_documents

    cfg = source.get("config") or {}
    dataset = str(cfg.get("dataset_url") or cfg.get("list_url") or _DE_DATASET_URL)
    errors: list[str] = []
    items: list[dict] = []
    skipped = 0

    # Pull a wide page; free site — no credit cost.
    soft = int(limit) if limit is not None else 500
    joiner = "&" if "?" in dataset else "?"
    list_url = f"{dataset}{joiner}$limit={max(soft * 3, 100)}"
    try:
        status, final, data = fetch_json(list_url, timeout=35)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_fetch:{type(exc).__name__}:{exc}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }
    if status >= 400 or not isinstance(data, list):
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"list_status={status}"],
            "verified_at": _utc(),
            "pages_fetched": 0,
            "rows_examined": 0,
            "pagination_complete": False,
            "more_available": True,
            "stats": {"accepted": 0, "skipped": 0, "mapping_failures": 1},
        }

    rows_examined = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        rows_examined += 1
        if limit is not None and len(items) >= int(limit):
            break
        title = str(row.get("contracttitle") or "").strip()
        sol = str(row.get("contractnumber") or "").strip()
        if not title:
            skipped += 1
            continue
        skip = listing_skip_before_detail_reason(title)
        if skip or _DE_SERVICEISH_RE.search(title):
            skipped += 1
            continue
        bid = _extract_url(row.get("bidurl"))
        if not bid:
            skipped += 1
            continue
        doc_urls: list[str] = []
        primary = bid
        if fetch_detail_docs:
            try:
                docs = discover_delaware_mmp_documents(bid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"docs:{sol}:{type(exc).__name__}")
                docs = []
            doc_urls = [str(d.get("source_url") or "") for d in docs if d.get("source_url")]
            # Prefer a primary solicitation PDF for body triage.
            for u in doc_urls:
                name = unquote(urlparse(u).path.split("/")[-1]).lower()
                if name.endswith((".pdf", ".docx", ".doc")) and not re.search(
                    r"(?i)(addendum|amend|ad\d+|app|att|pw|vol|specs?)", name
                ):
                    primary = u
                    break
            if primary == bid and doc_urls:
                primary = doc_urls[0]
        items.append(
            {
                "external_key": _key(source["source_key"], sol or bid, title),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": str(row.get("agencycode") or "Delaware public entity")[:255],
                "geo_state": "DE",
                "geo_city": None,
                "geo_location": "DE",
                "deadline_at": str(row.get("deadlinedate") or "") or None,
                "deadline_timezone": "America/New_York",
                "posted_at": str(row.get("opendate") or "")[:10] or None,
                "procurement_type": "open_bid",
                "product_category": _guess_category(title) or "unknown",
                "nigp_category": None,
                "set_aside": "unknown",
                "set_aside_verified": False,
                "estimated_value_min": None,
                "estimated_value_max": None,
                "estimated_value_unknown": True,
                "value_estimate_method": None,
                "value_evidence": None,
                "value_evidence_class": "not_yet_researched",
                "official_url": primary[:2048],
                "discovery_url": final or dataset,
                "documents_available": "yes" if doc_urls else "unknown",
                "document_urls": doc_urls,
                "quantity": None,
                "requirement_class": "pending_classification",
                "classification_evidence": ["deferred_to_central_classifier"],
                "solicitation_status": "open",
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "source_purpose": "individual_solicitation_candidate",
                "discovery_stage": "unrestricted",
                "verified_individual_solicitation": True,
                "record_type": "INDIVIDUAL_SOLICITATION",
                "summary": title,
                "evidence": {
                    "detail_url": bid,
                    "live_listing": True,
                    "live_detail": bool(fetch_detail_docs),
                    "documents_linked": len(doc_urls),
                },
            }
        )

    return {
        "ok": bool(items) or (rows_examined > 0 and skipped == rows_examined),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": rows_examined,
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "mapping_failures": 0,
            "listing_rows": rows_examined,
        },
    }


def discover_socrata_catalog(source: dict) -> dict[str, Any]:
    """Discover free public bid datasets via the Socrata catalog API (no paid key)."""
    cfg = source.get("config") or {}
    queries = list(cfg.get("queries") or ["open bid opportunities", "solicitations"])
    found: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for q in queries:
        try:
            url = f"https://api.us.socrata.com/api/catalog/v1?q={q.replace(' ', '%20')}&only=datasets&limit=15"
            status, final, data = fetch_json(url)
            if status >= 400 or not isinstance(data, dict):
                errors.append(f"{q} status={status}")
                continue
            for it in data.get("results") or []:
                res = it.get("resource") or {}
                rid = res.get("id")
                domain = (it.get("metadata") or {}).get("domain")
                name = res.get("name") or ""
                if not rid or not domain or rid in seen:
                    continue
                if re.search(r"charit|campaign|political|broadband|test\b", name, re.I):
                    continue
                seen.add(rid)
                found.append(
                    {
                        "dataset_id": rid,
                        "name": name,
                        "domain": domain,
                        "dataset_url": f"https://{domain}/resource/{rid}.json",
                        "link": it.get("link"),
                        "discovered_via": q,
                    }
                )
        except Exception as exc:
            errors.append(f"{q}:{type(exc).__name__}:{exc}"[:200])
    return {
        "ok": bool(found),
        "items": found,
        "source_key": source["source_key"],
        "errors": errors,
        "verified_at": _utc(),
    }


def _valid_phone(match: re.Match[str]) -> str | None:
    a, b, c = match.group(1), match.group(2), match.group(3)
    # Reject obvious non-phones (repeating, 000, etc.)
    if a[0] in "01" or b[0] in "01":
        return None
    if len(set(a + b + c)) <= 2:
        return None
    return f"({a}) {b}-{c}"


def _extract_phone(html: str) -> str | None:
    # Prefer explicit tel: links
    for href in re.findall(r'href=["\']tel:([^"\']+)["\']', html, flags=re.I):
        digits = re.sub(r"\D", "", href)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) == 10:
            m = PHONE_RE.search(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
            if m:
                p = _valid_phone(m)
                if p:
                    return p
    # Prefer numbers near contact language (windowed scan)
    for m in re.finditer(r"(?:phone|tel\.?|call us|contact|toll[- ]free)(.{0,60})", html, flags=re.I):
        inner = PHONE_RE.search(m.group(0))
        if inner:
            p = _valid_phone(inner)
            if p:
                return p
    for m in PHONE_RE.finditer(html):
        p = _valid_phone(m)
        if not p:
            continue
        start = max(0, m.start() - 12)
        ctx = html[start : m.end() + 12]
        if re.search(r"(rgba?|width|height|px|em|rem|version|sku|isbn)", ctx, re.I):
            continue
        return p
    return None


def probe_supplier_page(source: dict) -> dict[str, Any]:
    cfg = source.get("config") or {}
    urls = [source.get("base_url")] + list(cfg.get("extra_pages") or [])
    errors: list[str] = []
    phone = None
    email = None
    app_links: list[str] = []
    final_ok = None
    status_ok = None

    for url in urls:
        if not url:
            continue
        try:
            status, final, html = fetch_text(url)
            if status >= 400:
                errors.append(f"{url} status={status}")
                continue
            final_ok = final
            status_ok = status
            phone = phone or _extract_phone(html)
            emails = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", html, flags=re.I)
            for e in emails:
                if not re.search(r"(example\.com|sentry|wixpress|schema|webpack|cloudflare|noreply)", e, re.I):
                    email = email or e
                    break
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
            texts = re.findall(r"<a[^>]*>(.*?)</a>", html, flags=re.I | re.S)
            for i, href in enumerate(hrefs):
                label = re.sub(r"<[^>]+>", "", texts[i] if i < len(texts) else "").lower()
                h = href.lower()
                if any(
                    x in label or x in h
                    for x in (
                        "reseller",
                        "become a partner",
                        "become a dealer",
                        "apply",
                        "credit application",
                        "open an account",
                        "new account",
                        "government",
                        "trade account",
                    )
                ):
                    abs_u = urljoin(final, href)
                    if not abs_u.startswith("http"):
                        continue
                    if re.search(
                        r"\.(css|js|png|jpg|gif|svg|woff2?)(\?|$)|datadome|adobecqms|_next/static|images-na\.ssl-images",
                        abs_u,
                        re.I,
                    ):
                        continue
                    if abs_u not in app_links:
                        app_links.append(abs_u)
            if final_ok:
                break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    if not final_ok:
        return {"ok": False, "item": None, "errors": errors or ["unreachable"]}

    reseller = next(
        (a for a in app_links if re.search(r"reseller|partner|dealer|account|apply|government", a, re.I)),
        None,
    )
    item = {
        "name": source.get("name"),
        "website": final_ok,
        "supplier_type": cfg.get("supplier_type") or "unknown",
        "product_categories": list(cfg.get("categories") or []),
        "brands": list(cfg.get("brands") or []),
        "reseller_application_url": reseller,
        "credit_application_url": next((a for a in app_links if "credit" in a.lower()), None),
        "phone": phone,
        "email": email,
        "catalog_available": "unknown",
        "geo_coverage": "nationwide",
        "verification_status": "website_retrieved",
        "source_urls": [final_ok] + ([reseller] if reseller else [])[:3],
        "next_action": "Confirm reseller/credit path and record contacts",
        "source_key": source["source_key"],
        "evidence": {"http_status": status_ok, "app_links_found": len(app_links)},
    }
    return {"ok": True, "item": item, "errors": errors, "verified_at": _utc()}
