"""Generic Tier-1/Tier-2 structured adapters — Socrata, CKAN, ArcGIS, REST, CSV, XLSX, RSS.

Map heterogeneous public feeds into canonical discovery / history schemas.
Do not write one-off clients per dataset; pass domain + dataset id + field_map.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from discovery.schema import CanonicalOpportunity

TIER_OFFICIAL = "TIER_1_OFFICIAL_STRUCTURED"
TIER_STABLE = "TIER_2_STABLE_PUBLIC_STRUCTURED"
TIER_STATIC = "TIER_3_STABLE_STATIC"
TIER_FRAGILE = "TIER_4_FRAGILE"
PARKED_FRAGILE_SOURCE = "PARKED_FRAGILE_SOURCE"

ADAPTER_KINDS = ("rest_json", "socrata", "ckan", "arcgis", "csv", "xlsx", "rss")

# Engineering stop-loss: max attempts / seconds before parking fragile paths
STOP_LOSS_MAX_ATTEMPTS = 3
STOP_LOSS_MAX_SECONDS = 45.0


def classify_structured_tier(
    *,
    endpoint_type: str | None = None,
    official: bool = False,
    auth: str | None = None,
    fragile_signals: list[str] | None = None,
) -> str:
    et = (endpoint_type or "").upper()
    auth_u = (auth or "").upper()
    fragile = fragile_signals or []
    if any(x in et for x in ("CAPTCHA", "ANTI_BOT", "JS_SPA", "BROWSER")) or fragile:
        return TIER_FRAGILE
    if "AUTH" in auth_u and "FREE" not in auth_u and "PUBLIC" not in auth_u:
        if "LOGIN" in auth_u or "ACCOUNT" in auth_u:
            return TIER_FRAGILE
    if official and any(
        x in et for x in ("REST_API", "GRAPHQL", "BULK", "CSV", "XLSX", "XML", "JSON", "OPEN_DATA")
    ):
        return TIER_OFFICIAL
    if any(x in et for x in ("SOCRATA", "CKAN", "ARCGIS", "RSS", "ATOM", "FEATURESERVER", "JSON", "CSV")):
        return TIER_STABLE
    if any(x in et for x in ("HTML", "PDF", "STATIC")):
        return TIER_STATIC
    return TIER_STABLE if official else TIER_FRAGILE


def apply_engineering_stop_loss(
    *,
    attempts: int,
    elapsed_s: float,
    stable_path_found: bool,
    unique_yield: int = 0,
) -> dict[str, Any]:
    """Bounded stop-loss for Tier-4 / unstable integrations."""
    if stable_path_found and unique_yield > 0:
        return {"stop": False, "status": "CONTINUE", "reason": None}
    if attempts >= STOP_LOSS_MAX_ATTEMPTS or elapsed_s >= STOP_LOSS_MAX_SECONDS:
        return {
            "stop": True,
            "status": PARKED_FRAGILE_SOURCE,
            "reason": "no_stable_structured_path",
            "attempts": attempts,
            "elapsed_s": round(elapsed_s, 2),
        }
    return {"stop": False, "status": "CONTINUE", "reason": None}


def _pick(row: dict[str, Any], keys: str | list[str] | None) -> Any:
    if keys is None:
        return None
    if isinstance(keys, str):
        keys = [keys]
    for k in keys:
        if not k:
            continue
        if "." in k:
            cur: Any = row
            ok = True
            for part in k.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        elif k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _as_url(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("url") or val.get("href") or val.get("link")
    s = str(val).strip()
    if not s:
        return None
    if s.startswith("{") and "url" in s:
        try:
            # Socrata sometimes returns Python-repr-ish dict strings
            fixed = s.replace("'", '"')
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return obj.get("url")
        except Exception:
            m = re.search(r"https?://[^\s'\"}]+", s)
            return m.group(0) if m else None
    if s.startswith("http"):
        return s
    return None


def map_discovery_row(
    raw: dict[str, Any],
    *,
    field_map: dict[str, Any],
    source_id: str,
    list_url: str,
    trust_tier: int = 2,
    defaults: dict[str, Any] | None = None,
) -> CanonicalOpportunity | None:
    """Map one structured record → CanonicalOpportunity via field_map."""
    defaults = defaults or {}
    title = _pick(raw, field_map.get("title")) or defaults.get("title")
    if not title:
        return None
    title = str(title).strip()
    if not title or title.lower() in {"untitled", "none", "n/a"}:
        return None

    sol = _pick(raw, field_map.get("solicitation_id") or field_map.get("solicitation_number"))
    ext = str(sol or _pick(raw, field_map.get("external_id")) or title)[:160]
    deadline = _pick(raw, field_map.get("deadline") or field_map.get("open_date_deadline"))
    posted = _pick(raw, field_map.get("open_date") or field_map.get("posted_date"))
    status = str(_pick(raw, field_map.get("status")) or defaults.get("status") or "OPEN")
    agency = _pick(raw, field_map.get("buyer") or field_map.get("agency")) or defaults.get("agency")
    desc = _pick(raw, field_map.get("description"))
    detail = _as_url(_pick(raw, field_map.get("detail_url") or field_map.get("authoritative_url")))
    category = _pick(raw, field_map.get("category"))

    # Optional live-filter: skip closed / award-only when map says so
    status_live = field_map.get("live_status_values")
    if status_live and str(status).lower() not in {str(x).lower() for x in status_live}:
        # still allow if no explicit closed markers
        closed_markers = ("closed", "awarded", "cancelled", "canceled", "expired", "complete")
        if any(m in str(status).lower() for m in closed_markers):
            return None

    return CanonicalOpportunity(
        external_id=ext,
        source_id=source_id,
        source_url=list_url,
        detail_url=detail or list_url,
        title=title[:500],
        solicitation_number=str(sol)[:120] if sol else None,
        agency=str(agency)[:200] if agency else defaults.get("agency"),
        state_code=defaults.get("state_code"),
        city=defaults.get("city"),
        buyer_type=defaults.get("buyer_type"),
        jurisdiction=defaults.get("jurisdiction") or defaults.get("buyer_type"),
        description=str(desc)[:4000] if desc else (str(category) if category else None),
        status=status[:80],
        deadline_raw=str(deadline) if deadline else None,
        posted_date=None,
        trust_tier=trust_tier,
        raw_metadata={
            **raw,
            "structured_adapter": True,
            "field_map_keys": list(field_map.keys()),
            "posted_raw": posted,
            "category": category,
            "canonical_discovery": {
                "source": source_id,
                "buyer": agency,
                "solicitation_id": sol,
                "title": title,
                "description": desc,
                "open_date": posted,
                "deadline": deadline,
                "status": status,
                "category": category,
                "authoritative_url": detail or list_url,
                "freshness": "LIVE_STRUCTURED",
            },
        },
    )


def map_history_row(
    raw: dict[str, Any],
    *,
    field_map: dict[str, Any],
    source_id: str,
    source_url: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Map award/PO/payment record → canonical history schema."""
    defaults = defaults or {}
    product = _pick(raw, field_map.get("product") or field_map.get("description") or field_map.get("title"))
    if not product:
        return None
    total = _pick(raw, field_map.get("total") or field_map.get("amount"))
    qty = _pick(raw, field_map.get("quantity"))
    unit_price = _pick(raw, field_map.get("unit_price"))
    # Do not infer unit price unless quantity is clear
    if unit_price is None and total is not None and qty not in (None, "", 0, "0"):
        try:
            qf = float(qty)
            tf = float(str(total).replace(",", "").replace("$", ""))
            if qf > 0:
                unit_price = round(tf / qf, 4)
        except (TypeError, ValueError):
            unit_price = None

    buyer = _pick(raw, field_map.get("buyer") or field_map.get("agency")) or defaults.get("buyer")
    vendor = _pick(raw, field_map.get("vendor") or field_map.get("awardee"))
    return {
        "kind": "CanonicalHistoryRow",
        "source": source_id,
        "buyer": buyer,
        "solicitation_id": _pick(raw, field_map.get("solicitation_id")),
        "vendor": vendor,
        "product": str(product)[:500],
        "manufacturer": _pick(raw, field_map.get("manufacturer")),
        "model_mpn": _pick(raw, field_map.get("model") or field_map.get("mpn")),
        "quantity": qty,
        "uom": _pick(raw, field_map.get("uom")) or "EA",
        "unit_price": unit_price,
        "total": total,
        "award_date": _pick(raw, field_map.get("award_date") or field_map.get("date")),
        "source_url": source_url,
        "po_number": _pick(raw, field_map.get("po_number")),
        "evidence_grade_candidate": "GOV_C" if unit_price is not None else "GOV_D",
        "raw": {k: raw.get(k) for k in list(raw.keys())[:24]},
    }


def parse_socrata_json(
    body: str,
    *,
    field_map: dict[str, Any],
    source_id: str,
    list_url: str,
    role: str = "LIVE",
    defaults: dict[str, Any] | None = None,
    trust_tier: int = 2,
) -> list[CanonicalOpportunity] | list[dict[str, Any]]:
    data = json.loads(body)
    rows = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
    out: list[Any] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if role.upper() == "HISTORY":
            h = map_history_row(raw, field_map=field_map, source_id=source_id, source_url=list_url, defaults=defaults)
            if h:
                out.append(h)
        else:
            opp = map_discovery_row(
                raw,
                field_map=field_map,
                source_id=source_id,
                list_url=list_url,
                trust_tier=trust_tier,
                defaults=defaults,
            )
            if opp:
                out.append(opp)
    return out


def parse_ckan_package_search(body: str) -> list[dict[str, Any]]:
    data = json.loads(body)
    results = ((data.get("result") or {}).get("results")) or []
    out = []
    for pkg in results:
        if not isinstance(pkg, dict):
            continue
        resources = pkg.get("resources") or []
        structured = [
            r
            for r in resources
            if str(r.get("format") or "").upper() in {"CSV", "JSON", "XLSX", "XML", "GEOJSON"}
            or str(r.get("url") or "").endswith((".csv", ".json", ".xlsx"))
        ]
        out.append(
            {
                "title": pkg.get("title"),
                "name": pkg.get("name"),
                "organization": (pkg.get("organization") or {}).get("title"),
                "notes": (pkg.get("notes") or "")[:300],
                "structured_resources": [
                    {"name": r.get("name"), "format": r.get("format"), "url": r.get("url")}
                    for r in structured[:8]
                ],
                "resource_count": len(resources),
            }
        )
    return out


def parse_arcgis_features(
    body: str,
    *,
    field_map: dict[str, Any],
    source_id: str,
    list_url: str,
    role: str = "LIVE",
    defaults: dict[str, Any] | None = None,
) -> list[Any]:
    data = json.loads(body)
    feats = data.get("features") or []
    out: list[Any] = []
    for f in feats:
        attrs = f.get("attributes") if isinstance(f, dict) else None
        if not isinstance(attrs, dict):
            continue
        if role.upper() == "HISTORY":
            h = map_history_row(attrs, field_map=field_map, source_id=source_id, source_url=list_url, defaults=defaults)
            if h:
                out.append(h)
        else:
            opp = map_discovery_row(
                attrs, field_map=field_map, source_id=source_id, list_url=list_url, defaults=defaults
            )
            if opp:
                out.append(opp)
    return out


def parse_csv_feed(
    body: str,
    *,
    field_map: dict[str, Any],
    source_id: str,
    list_url: str,
    role: str = "LIVE",
    defaults: dict[str, Any] | None = None,
) -> list[Any]:
    reader = csv.DictReader(io.StringIO(body))
    out: list[Any] = []
    for raw in reader:
        if role.upper() == "HISTORY":
            h = map_history_row(raw, field_map=field_map, source_id=source_id, source_url=list_url, defaults=defaults)
            if h:
                out.append(h)
        else:
            opp = map_discovery_row(
                raw, field_map=field_map, source_id=source_id, list_url=list_url, defaults=defaults
            )
            if opp:
                out.append(opp)
    return out


def parse_xlsx_rows(
    rows: list[dict[str, Any]],
    *,
    field_map: dict[str, Any],
    source_id: str,
    list_url: str,
    role: str = "LIVE",
    defaults: dict[str, Any] | None = None,
) -> list[Any]:
    """XLSX already parsed into dict rows (openpyxl/pandas upstream)."""
    out: list[Any] = []
    for raw in rows:
        if role.upper() == "HISTORY":
            h = map_history_row(raw, field_map=field_map, source_id=source_id, source_url=list_url, defaults=defaults)
            if h:
                out.append(h)
        else:
            opp = map_discovery_row(
                raw, field_map=field_map, source_id=source_id, list_url=list_url, defaults=defaults
            )
            if opp:
                out.append(opp)
    return out


def parse_rss_atom(
    body: str,
    *,
    source_id: str,
    list_url: str,
    defaults: dict[str, Any] | None = None,
) -> list[CanonicalOpportunity]:
    root = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall("atom:entry", ns)
    if not items and channel is None:
        items = root.findall("{http://www.w3.org/2005/Atom}entry")
    out: list[CanonicalOpportunity] = []
    for it in items:
        title_el = it.find("title")
        if title_el is None:
            title_el = it.find("{http://www.w3.org/2005/Atom}title")
        title = (title_el.text if title_el is not None else None) or "Untitled"
        link = None
        link_el = it.find("link")
        if link_el is not None:
            link = link_el.get("href") or link_el.text
        else:
            link_el = it.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = link_el.get("href") or link_el.text
        desc_el = it.find("description")
        if desc_el is None:
            desc_el = it.find("{http://www.w3.org/2005/Atom}summary")
        desc = desc_el.text if desc_el is not None else None
        guid_el = it.find("guid")
        if guid_el is None:
            guid_el = it.find("{http://www.w3.org/2005/Atom}id")
        ext = (guid_el.text if guid_el is not None else None) or title
        out.append(
            CanonicalOpportunity(
                external_id=str(ext)[:160],
                source_id=source_id,
                source_url=list_url,
                detail_url=link,
                title=str(title)[:500],
                description=str(desc)[:4000] if desc else None,
                status="OPEN",
                agency=(defaults or {}).get("agency"),
                state_code=(defaults or {}).get("state_code"),
                trust_tier=2,
                raw_metadata={"structured_adapter": True, "feed": "rss_atom"},
            )
        )
    return out


def build_socrata_url(domain: str, dataset_id: str, *, params: dict[str, Any] | None = None) -> str:
    base = f"https://{domain.rstrip('/')}/resource/{dataset_id}.json"
    if not params:
        return base
    return base + "?" + urlencode({k: v for k, v in params.items() if v is not None})


def extract_socrata_ids(list_url: str) -> tuple[str | None, str | None]:
    """Return (domain, dataset_id) from a Socrata resource URL."""
    p = urlparse(list_url)
    m = re.search(r"/resource/([a-z0-9]{4}-[a-z0-9]{4})", p.path or "", re.I)
    if not m:
        return None, None
    return p.netloc, m.group(1)


def unique_contribution_score(
    *,
    unique_live: int = 0,
    unique_commercial: int = 0,
    unique_stage3: int = 0,
    exact_history: int = 0,
    quote_targets: int = 0,
) -> dict[str, Any]:
    score = (
        unique_live * 2
        + unique_commercial * 4
        + unique_stage3 * 5
        + exact_history * 3
        + quote_targets * 10
    )
    return {
        "UniqueCommercialContribution": score,
        "unique_live_rows": unique_live,
        "unique_commercial_rows": unique_commercial,
        "unique_stage3": unique_stage3,
        "exact_history_rows": exact_history,
        "quote_targets": quote_targets,
    }


def structured_source_value_score(
    *,
    unique_opportunity: int = 0,
    commercial_yield: int = 0,
    history_yield: int = 0,
    reliability: float = 0.5,
    refresh_speed: float = 0.5,
    auth_burden: float = 0.0,
    maintenance_burden: float = 0.3,
    economic_target_yield: int = 0,
    tier: str = TIER_STABLE,
) -> dict[str, Any]:
    tier_w = {
        TIER_OFFICIAL: 1.0,
        TIER_STABLE: 0.85,
        TIER_STATIC: 0.55,
        TIER_FRAGILE: 0.15,
    }.get(tier, 0.5)
    raw = (
        unique_opportunity * 2.0
        + commercial_yield * 3.0
        + history_yield * 2.5
        + economic_target_yield * 2.0
        + reliability * 20
        + refresh_speed * 10
        - auth_burden * 15
        - maintenance_burden * 12
    ) * tier_w
    return {
        "StructuredSourceValueScore": round(raw, 2),
        "tier": tier,
        "factors": {
            "unique_opportunity": unique_opportunity,
            "commercial_yield": commercial_yield,
            "history_yield": history_yield,
            "reliability": reliability,
            "refresh_speed": refresh_speed,
            "auth_burden": auth_burden,
            "maintenance_burden": maintenance_burden,
            "economic_target_yield": economic_target_yield,
        },
    }


def cross_source_dedupe_key(row: dict[str, Any]) -> str:
    sol = str(row.get("solicitation_number") or row.get("solicitation_id") or "").strip().lower()
    buyer = str(row.get("agency") or row.get("buyer") or "").strip().lower()[:40]
    url = str(row.get("detail_url") or row.get("source_url") or "").strip().lower()[:120]
    platform_id = str(row.get("external_id") or row.get("platform_id") or "").strip().lower()
    title = re.sub(r"\s+", " ", str(row.get("title") or "").lower())[:80]
    deadline = str(row.get("deadline_raw") or row.get("deadline") or "")[:20]
    if sol and buyer:
        return f"sol|{buyer}|{sol}"
    if platform_id and row.get("source_id"):
        return f"plat|{row.get('source_id')}|{platform_id}"
    if url and "http" in url:
        return f"url|{url}"
    return f"title|{title}|{deadline}|{buyer}"


def dedupe_structured_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (unique_rows, duplicate_provenance)."""
    seen: dict[str, str] = {}
    unique: list[dict[str, Any]] = []
    dups: list[dict[str, Any]] = []
    for r in rows:
        key = cross_source_dedupe_key(r)
        sid = str(r.get("source_id") or "")
        if key in seen:
            dups.append(
                {
                    "key": key,
                    "duplicate_source": sid,
                    "kept_source": seen[key],
                    "title": (r.get("title") or "")[:80],
                }
            )
            continue
        seen[key] = sid
        unique.append(r)
    return unique, dups
