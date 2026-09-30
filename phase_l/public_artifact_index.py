"""Phase L.13 — PublicArtifactIndex + URL pattern memory + discovery cache."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from phase_l.public_artifact_types import BUILD, UNKNOWN_PUBLIC_ARTIFACT

ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "data" / "phase_l13_public_artifact_index.json"
PATTERN_PATH = ROOT / "data" / "phase_l13_url_patterns.json"
CACHE_PATH = ROOT / "data" / "phase_l13_artifact_discovery_cache.json"


def _utc() -> str:
    return now_utc().isoformat()


def _load(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default or {"build": BUILD}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["build"] = BUILD
    data["updated_at"] = _utc()
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def empty_artifact(
    *,
    platform: str,
    buyer: str | None,
    solicitation: str,
    url: str,
    artifact_type: str = UNKNOWN_PUBLIC_ARTIFACT,
) -> dict[str, Any]:
    return {
        "kind": "PublicArtifactIndex",
        "platform": platform,
        "buyer": buyer,
        "solicitation_number": solicitation,
        "artifact_url": url,
        "artifact_type": artifact_type,
        "public_access_state": None,
        "discovered_via": None,
        "content_date": None,
        "evidence_types_available": [],
        "parsed_successfully": False,
        "source_health": None,
        "exact_match_basis": [],
        "updated_at": _utc(),
    }


def upsert_artifact(rec: dict[str, Any]) -> dict[str, Any]:
    data = _load(INDEX_PATH, {"artifacts": [], "build": BUILD})
    arts = list(data.get("artifacts") or [])
    url = str(rec.get("artifact_url") or "")
    sid = str(rec.get("solicitation_number") or "")
    replaced = False
    for i, a in enumerate(arts):
        if a.get("artifact_url") == url and str(a.get("solicitation_number") or "") == sid:
            arts[i] = {**a, **rec, "updated_at": _utc()}
            replaced = True
            break
    if not replaced:
        arts.append({**empty_artifact(
            platform=str(rec.get("platform") or "unknown"),
            buyer=rec.get("buyer"),
            solicitation=sid,
            url=url,
            artifact_type=str(rec.get("artifact_type") or UNKNOWN_PUBLIC_ARTIFACT),
        ), **rec, "updated_at": _utc()})
    data["artifacts"] = arts[-2000:]
    _save(INDEX_PATH, data)
    return rec


def all_artifacts() -> list[dict[str, Any]]:
    return list(_load(INDEX_PATH, {"artifacts": []}).get("artifacts") or [])


def artifacts_for_solicitation(solicitation: str) -> list[dict[str, Any]]:
    sid = str(solicitation or "").strip()
    return [a for a in all_artifacts() if str(a.get("solicitation_number") or "") == sid]


# --- Observed URL pattern memory (never invent IDs) ---

def record_observed_url_pattern(
    platform: str,
    *,
    url: str,
    artifact_type: str,
    solicitation: str | None = None,
    discovery_method: str | None = None,
    access_status: str | None = None,
) -> dict[str, Any]:
    """Learn structure only from actually observed public URLs."""
    data = _load(PATTERN_PATH, {"patterns": [], "build": BUILD})
    parsed = urlparse(url)
    # Structure template: replace this solicitation id with {SOLICITATION} if present
    structure = url
    if solicitation and solicitation in url:
        structure = url.replace(solicitation, "{SOLICITATION}")
    rec = {
        "platform": platform,
        "url_structure": structure,
        "host": parsed.netloc.lower(),
        "path_template": parsed.path.replace(solicitation or "___", "{SOLICITATION}") if solicitation else parsed.path,
        "artifact_type": artifact_type,
        "solicitation_linkage": solicitation,
        "access_status": access_status,
        "discovery_method": discovery_method,
        "date_last_verified": _utc(),
        "example_url": url,
    }
    patterns = list(data.get("patterns") or [])
    # Dedupe by structure
    for i, p in enumerate(patterns):
        if p.get("url_structure") == structure and p.get("platform") == platform:
            patterns[i] = {**p, **rec}
            data["patterns"] = patterns
            _save(PATTERN_PATH, data)
            return rec
    patterns.append(rec)
    data["patterns"] = patterns[-200:]
    _save(PATTERN_PATH, data)
    return rec


def observed_patterns_for(platform: str) -> list[dict[str, Any]]:
    return [p for p in (_load(PATTERN_PATH, {"patterns": []}).get("patterns") or []) if p.get("platform") == platform]


def apply_observed_pattern(pattern: dict[str, Any], solicitation: str) -> str | None:
    """Instantiate a previously observed structure — never invent new ID spaces."""
    structure = str(pattern.get("url_structure") or "")
    if "{SOLICITATION}" not in structure:
        return None
    if not solicitation:
        return None
    return structure.replace("{SOLICITATION}", solicitation)


# --- Discovery cache ---

def cache_key(platform: str, solicitation: str, query_kind: str) -> str:
    return f"{platform}|{solicitation}|{query_kind}"


def get_cached_discovery(platform: str, solicitation: str, query_kind: str) -> dict[str, Any] | None:
    data = _load(CACHE_PATH, {"entries": {}})
    return (data.get("entries") or {}).get(cache_key(platform, solicitation, query_kind))


def set_cached_discovery(
    platform: str,
    solicitation: str,
    query_kind: str,
    *,
    urls: list[str],
    provider: str | None = None,
    query: str | None = None,
) -> None:
    data = _load(CACHE_PATH, {"entries": {}})
    entries = data.setdefault("entries", {})
    entries[cache_key(platform, solicitation, query_kind)] = {
        "urls": urls[:20],
        "provider": provider,
        "query": query,
        "cached_at": _utc(),
    }
    # Cap cache size
    if len(entries) > 5000:
        keys = sorted(entries.keys(), key=lambda k: entries[k].get("cached_at") or "")[:1000]
        for k in keys:
            entries.pop(k, None)
    _save(CACHE_PATH, data)


def invalidate_cache_for(solicitation: str) -> None:
    data = _load(CACHE_PATH, {"entries": {}})
    entries = data.get("entries") or {}
    drop = [k for k in entries if f"|{solicitation}|" in k]
    for k in drop:
        entries.pop(k, None)
    data["entries"] = entries
    _save(CACHE_PATH, data)


def classify_artifact_type(url: str, title_blob: str | None = None) -> str:
    from phase_l.public_artifact_types import (
        AMENDMENT,
        AWARD_PDF,
        AWARD_PRINT_VIEW,
        BID_TAB,
        BOARD_DOCUMENT,
        PRICE_SHEET,
        PUBLIC_ATTACHMENT,
        SOLICITATION_PRINT_VIEW,
        SPECIFICATION,
        TABULATION,
        UNKNOWN_PUBLIC_ARTIFACT,
    )

    t = f"{url} {title_blob or ''}".lower()
    if "bidtab" in t or "bid-tab" in t or "bid_tab" in t:
        return BID_TAB
    if "tabulation" in t:
        return TABULATION
    if "award" in t and (".pdf" in t or "print-pdf" in t):
        return AWARD_PDF
    if "award" in t and "print" in t:
        return AWARD_PRINT_VIEW
    if "print" in t and ("solicitation" in t or "abstract" in t):
        return SOLICITATION_PRINT_VIEW
    if "abstract" in t or "/solicitations/" in t:
        return SOLICITATION_PRINT_VIEW if ("print" in t or "abstract" in t) else PUBLIC_ATTACHMENT
    if "amendment" in t or "addendum" in t:
        return AMENDMENT
    if "spec" in t or "specification" in t:
        return SPECIFICATION
    if "price" in t or "pricing" in t:
        return PRICE_SHEET
    if "agenda" in t or "board" in t or "council" in t or "minutes" in t:
        return BOARD_DOCUMENT
    if ".pdf" in t or "attachment" in t:
        return PUBLIC_ATTACHMENT
    return UNKNOWN_PUBLIC_ARTIFACT


def extract_public_links(html: str, *, base_host: str | None = None) -> list[str]:
    """Extract hrefs from public HTML — no ID invention."""
    if not html:
        return []
    links = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    out: list[str] = []
    for href in links:
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("http"):
            out.append(href.split("#")[0])
        elif href.startswith("/") and base_host:
            out.append(f"https://{base_host}{href}".split("#")[0])
    # dedupe
    seen: set[str] = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:80]
