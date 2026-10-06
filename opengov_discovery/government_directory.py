"""OpenGov government directory — maps portal slugs/names to API government.code."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("govtracker.opengov_discovery.government_directory")

GOVERNMENT_LIST_URL = "https://api.procurement.opengov.com/api/v1/government"
PROJECT_PUBLIC_TMPL = (
    "https://api.procurement.opengov.com/api/v1/government/{code}/project/public"
)
CACHE_FILE = "opengov_auth/government_directory.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _norm_name(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\b(city|county|town|township|borough|village|parish|of|the|and)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def slug_from_portal_url(url: str) -> str | None:
    m = re.search(r"/portal/(?:embed/)?([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else None


class OpenGovGovernmentDirectory:
    """Cached GET /api/v1/government → active code index."""

    def __init__(self, *, client: httpx.Client | None = None, ttl_hours: int = 24) -> None:
        self._owns = client is None
        self._client = client or httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Origin": "https://procurement.opengov.com",
                "Referer": "https://procurement.opengov.com/",
            },
        )
        self.ttl_hours = ttl_hours
        self._by_code: dict[str, dict[str, Any]] = {}
        self._by_norm: dict[str, list[str]] = {}
        self._loaded = False

    def close(self) -> None:
        if self._owns:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self) -> "OpenGovGovernmentDirectory":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _cache_path(self):
        from m3_data_root import data_path

        return data_path(CACHE_FILE)

    def ensure_loaded(self, *, force: bool = False) -> None:
        if self._loaded and not force:
            return
        from application_clock import now_utc
        from datetime import timedelta

        path = self._cache_path()
        if not force and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                fetched = raw.get("fetched_at")
                if fetched:
                    from datetime import datetime

                    ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
                    if now_utc() - ts < timedelta(hours=self.ttl_hours):
                        self._ingest(raw.get("governments") or [])
                        self._loaded = True
                        return
            except Exception:
                pass

        try:
            r = self._client.get(GOVERNMENT_LIST_URL)
            r.raise_for_status()
            govs = r.json()
            if not isinstance(govs, list):
                govs = []
        except Exception as exc:
            log.warning("OpenGov government directory fetch failed: %s", type(exc).__name__)
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    self._ingest(raw.get("governments") or [])
                    self._loaded = True
                    return
                except Exception:
                    pass
            self._by_code = {}
            self._by_norm = {}
            self._loaded = True
            return

        slim = []
        for g in govs:
            if not isinstance(g, dict):
                continue
            nested = g.get("government") if isinstance(g.get("government"), dict) else {}
            code = str(nested.get("code") or "").strip().lower()
            if not code:
                continue
            if g.get("isVendor") or g.get("isInternal") or g.get("isActive") is False:
                continue
            slim.append(
                {
                    "code": code,
                    "name": g.get("name"),
                    "state": g.get("state"),
                    "id": g.get("id"),
                    "city": g.get("city"),
                }
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"fetched_at": now_utc().isoformat(), "count": len(slim), "governments": slim},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        self._ingest(slim)
        self._loaded = True

    def _ingest(self, governments: list[Any]) -> None:
        by_code: dict[str, dict[str, Any]] = {}
        by_norm: dict[str, list[str]] = {}
        for g in governments:
            if not isinstance(g, dict):
                continue
            code = str(g.get("code") or "").strip().lower()
            if not code:
                continue
            by_code[code] = g
            nn = _norm_name(str(g.get("name") or ""))
            if nn:
                by_norm.setdefault(nn, []).append(code)
        self._by_code = by_code
        self._by_norm = by_norm

    @property
    def codes(self) -> list[str]:
        self.ensure_loaded()
        return sorted(self._by_code.keys())

    def resolve_code(self, portal: dict[str, Any]) -> str | None:
        """Resolve API government.code for a known portal. Prefer exact matches only."""
        self.ensure_loaded()
        if portal.get("government_code"):
            code = str(portal["government_code"]).strip().lower()
            if code in self._by_code:
                return code
        slug = slug_from_portal_url(str(portal.get("portal_url") or ""))
        if slug and slug in self._by_code:
            return slug
        name = str(portal.get("entity_name") or "")
        state = str(portal.get("state") or "").strip().upper()
        nn = _norm_name(name)
        cands = list(self._by_norm.get(nn) or [])
        if state:
            filtered = [
                c
                for c in cands
                if str((self._by_code.get(c) or {}).get("state") or "").upper() == state
            ]
            if filtered:
                cands = filtered
        if len(cands) == 1:
            return cands[0]
        # Try "City of X" / "County of X" style against portal entity name
        for prefix in ("city of ", "county of ", "town of "):
            nn2 = _norm_name(prefix + name)
            cands2 = list(self._by_norm.get(nn2) or [])
            if state:
                cands2 = [
                    c
                    for c in cands2
                    if str((self._by_code.get(c) or {}).get("state") or "").upper() == state
                ]
            if len(cands2) == 1:
                return cands2[0]
        return None

    def as_portals(self) -> list[dict[str, Any]]:
        """Full active OpenGov government universe as portal-shaped dicts."""
        self.ensure_loaded()
        out = []
        for code, g in sorted(self._by_code.items()):
            out.append(
                {
                    "entity_name": g.get("name") or code,
                    "entity_type": "government",
                    "state": g.get("state"),
                    "portal_url": f"https://procurement.opengov.com/portal/{code}",
                    "government_code": code,
                    "platform_family": "OPENGOV",
                    "source": "opengov_government_directory",
                }
            )
        return out

    def project_public_url(self, code: str) -> str:
        return PROJECT_PUBLIC_TMPL.format(code=code.strip().lower())
