"""OpenGovRouteResolver — per-entity route map, working-route cache, blocked cooldowns."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from opengov_auth.states import (
    ANTI_BOT,
    INVALID_PORTAL,
    MOVED,
    NO_OPEN_BIDS,
    RECOVERY_BLOCKED,
    WORKING_AGENCY_FALLBACK,
    WORKING_AUTH_REQUEST,
    WORKING_BROWSER,
    WORKING_EMBED,
    WORKING_PAGE_STATE,
    WORKING_STRUCTURED,
)

log = logging.getLogger("govtracker.opengov_discovery.route_resolver")

RESOLVER_FILE = "opengov_auth/route_resolver.json"
DEFAULT_COOLDOWN_HOURS = 24

ROUTE_PUBLIC_STRUCTURED = "PUBLIC_STRUCTURED"
ROUTE_PUBLIC_EMBED = "PUBLIC_EMBED_PROJECT_LIST"
ROUTE_PUBLIC_PAGE_STATE = "PUBLIC_PAGE_STATE"
ROUTE_AUTH_SESSION = "AUTHENTICATED_SESSION_REQUEST"
ROUTE_AGENCY_SOURCE = "ORIGINAL_AGENCY_SOURCE"
ROUTE_BROWSER = "BROWSER_FALLBACK"

ROUTE_PRIORITY = (
    ROUTE_PUBLIC_STRUCTURED,
    ROUTE_PUBLIC_EMBED,
    ROUTE_PUBLIC_PAGE_STATE,
    ROUTE_AUTH_SESSION,
    ROUTE_AGENCY_SOURCE,
    ROUTE_BROWSER,
)

STATUS_FOR_ROUTE = {
    ROUTE_PUBLIC_STRUCTURED: WORKING_STRUCTURED,
    ROUTE_PUBLIC_EMBED: WORKING_EMBED,
    ROUTE_PUBLIC_PAGE_STATE: WORKING_PAGE_STATE,
    ROUTE_AUTH_SESSION: WORKING_AUTH_REQUEST,
    ROUTE_AGENCY_SOURCE: WORKING_AGENCY_FALLBACK,
    ROUTE_BROWSER: WORKING_BROWSER,
}


def _slug_from_portal(url: str) -> str | None:
    m = re.search(r"/portal/(?:embed/)?([^/?#]+)", url or "", re.I)
    return m.group(1) if m else None


def entity_key(portal: dict[str, Any]) -> str:
    url = str(portal.get("portal_url") or "")
    slug = _slug_from_portal(url)
    if slug:
        return f"slug:{slug.lower()}"
    name = re.sub(r"[^a-z0-9]+", "_", str(portal.get("entity_name") or "").lower())[:64]
    return name or urlparse(url).netloc or url[:80]


class OpenGovRouteResolver:
    """Persist working/blocked routes so scheduled runs skip Cloudflare thrash."""

    def __init__(self) -> None:
        self._data = self._load()

    def _path(self):
        from m3_data_root import data_path

        return data_path(RESOLVER_FILE)

    def _load(self) -> dict[str, Any]:
        path = self._path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("entities", {})
                    return data
            except Exception:
                pass
        return {"kind": "OpenGovRouteResolver", "entities": {}, "updated_at": None}

    def save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = now_utc().isoformat()
        path.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")

    def get_entity(self, portal: dict[str, Any]) -> dict[str, Any]:
        key = entity_key(portal)
        entities = self._data.setdefault("entities", {})
        row = entities.get(key)
        if not isinstance(row, dict):
            url = str(portal.get("portal_url") or "")
            slug = _slug_from_portal(url)
            base = "https://procurement.opengov.com"
            gov_code = portal.get("government_code")
            api_slug = gov_code or slug
            row = {
                "entity_key": key,
                "entity_name": portal.get("entity_name"),
                "entity_type": portal.get("entity_type"),
                "state": portal.get("state"),
                "portal_url": url,
                "portal_id": slug or gov_code,
                "government_code": gov_code,
                "public_list_url": f"{base}/portal/{api_slug}/projects" if api_slug else None,
                "public_embed_url": f"{base}/portal/embed/{api_slug}/project-list" if api_slug else None,
                "structured_endpoint": (
                    f"https://api.procurement.opengov.com/api/v1/government/{api_slug}/project/public"
                    if api_slug
                    else None
                ),
                "detail_endpoint_pattern": (
                    f"{base}/portal/{api_slug}/projects/{{id}}" if api_slug else None
                ),
                "agency_source_url": portal.get("agency_source_url")
                or portal.get("procurement_page")
                or portal.get("bid_portal"),
                "working_route": None,
                "working_route_url": None,
                "route_priority": list(ROUTE_PRIORITY),
                "routes_attempted": [],
                "failure_per_route": {},
                "blocked_routes": {},
                "last_success": None,
                "last_failure": None,
                "last_verified": None,
                "status": None,
                "anti_bot_primary": False,
                "recovered_via_fallback": False,
            }
            entities[key] = row
        else:
            # Keep identity fields fresh
            row["entity_name"] = portal.get("entity_name") or row.get("entity_name")
            row["portal_url"] = portal.get("portal_url") or row.get("portal_url")
            if portal.get("agency_source_url"):
                row["agency_source_url"] = portal.get("agency_source_url")
            if portal.get("government_code"):
                row["government_code"] = portal.get("government_code")
                row["structured_endpoint"] = (
                    f"https://api.procurement.opengov.com/api/v1/government/"
                    f"{portal['government_code']}/project/public"
                )
        return row

    def is_route_cooled(self, entity: dict[str, Any], route_kind: str, route_url: str | None = None) -> bool:
        """Cooldownout is per kind|url only (never whole-kind)."""
        if not route_url:
            return False
        blocked = entity.get("blocked_routes") or {}
        info = blocked.get(f"{route_kind}|{route_url}")
        if not isinstance(info, dict):
            return False
        until = info.get("cooldown_until")
        if not until:
            return True
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            return now_utc() < dt
        except Exception:
            return True

    def mark_blocked(
        self,
        portal: dict[str, Any],
        *,
        route_kind: str,
        route_url: str | None,
        reason: str = ANTI_BOT,
        cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
    ) -> None:
        ent = self.get_entity(portal)
        until = (now_utc() + timedelta(hours=max(1, cooldown_hours))).isoformat()
        blocked = ent.setdefault("blocked_routes", {})
        payload = {"status": "BLOCKED", "reason": reason, "cooldown_until": until, "fallback": None}
        # Only cool this specific kind|url — never the whole kind (would skip siblings)
        if route_url:
            blocked[f"{route_kind}|{route_url}"] = payload
        # Soft kind marker for telemetry only (no cooldown)
        fails = ent.setdefault("failure_per_route", {})
        fails[route_kind] = reason
        attempted = ent.setdefault("routes_attempted", [])
        if route_kind not in attempted:
            attempted.append(route_kind)
        ent["last_failure"] = now_utc().isoformat()
        if reason == ANTI_BOT and route_kind in {
            ROUTE_PUBLIC_STRUCTURED,
            ROUTE_PUBLIC_EMBED,
            ROUTE_PUBLIC_PAGE_STATE,
        }:
            ent["anti_bot_primary"] = True
        self.save()

    def mark_success(
        self,
        portal: dict[str, Any],
        *,
        route_kind: str,
        route_url: str,
        status: str | None = None,
        retrieved: int = 0,
    ) -> None:
        ent = self.get_entity(portal)
        ent["working_route"] = route_kind
        ent["working_route_url"] = route_url
        ent["status"] = status or STATUS_FOR_ROUTE.get(route_kind) or "WORKING"
        ent["last_success"] = now_utc().isoformat()
        ent["last_verified"] = ent["last_success"]
        ent["live_count"] = retrieved
        attempted = ent.setdefault("routes_attempted", [])
        if route_kind not in attempted:
            attempted.append(route_kind)
        if ent.get("anti_bot_primary") and route_kind not in {
            ROUTE_PUBLIC_STRUCTURED,
            ROUTE_PUBLIC_EMBED,
            ROUTE_PUBLIC_PAGE_STATE,
        }:
            ent["recovered_via_fallback"] = True
        blocked = ent.setdefault("blocked_routes", {})
        blocked.pop(f"{route_kind}|{route_url}", None)
        blocked.pop(route_kind, None)
        self.save()

    def mark_final_status(self, portal: dict[str, Any], status: str) -> None:
        ent = self.get_entity(portal)
        ent["status"] = status
        ent["last_verified"] = now_utc().isoformat()
        if status in {RECOVERY_BLOCKED, INVALID_PORTAL, MOVED, NO_OPEN_BIDS}:
            ent["last_failure"] = ent["last_verified"]
        self.save()

    def candidate_urls(self, portal: dict[str, Any]) -> list[tuple[str, str]]:
        """Return (route_kind, url) in cascade order, skipping cooled-down blocked routes."""
        ent = self.get_entity(portal)
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, url: str | None) -> None:
            u = (url or "").strip()
            if not u:
                return
            key = (kind, u)
            if key in seen:
                return
            if self.is_route_cooled(ent, kind, u):
                return
            seen.add(key)
            out.append((kind, u))

        if ent.get("working_route_url") and ent.get("working_route"):
            add(str(ent["working_route"]), str(ent["working_route_url"]))

        slug = ent.get("portal_id") or _slug_from_portal(str(ent.get("portal_url") or ""))
        gov_code = (
            str(portal.get("government_code") or ent.get("government_code") or "").strip().lower()
            or None
        )
        if not gov_code:
            try:
                from opengov_discovery.government_directory import OpenGovGovernmentDirectory

                # Module-level cache via file; cheap after first load
                if not hasattr(OpenGovRouteResolver, "_gov_dir"):
                    OpenGovRouteResolver._gov_dir = OpenGovGovernmentDirectory()  # type: ignore[attr-defined]
                gov_code = OpenGovRouteResolver._gov_dir.resolve_code(portal)  # type: ignore[attr-defined]
            except Exception:
                gov_code = None
        if gov_code:
            ent["government_code"] = gov_code
            ent["portal_id"] = ent.get("portal_id") or gov_code
            ent["structured_endpoint"] = (
                f"https://api.procurement.opengov.com/api/v1/government/{gov_code}/project/public"
            )
        api_slug = gov_code or slug
        base = "https://procurement.opengov.com"
        if api_slug:
            # Real public structured route (POST) — primary unlock
            add(
                ROUTE_PUBLIC_STRUCTURED,
                f"https://api.procurement.opengov.com/api/v1/government/{api_slug}/project/public",
            )
            # Legacy guesses kept as low-priority structured fallthrough
            if slug and slug != api_slug:
                add(
                    ROUTE_PUBLIC_STRUCTURED,
                    f"https://api.procurement.opengov.com/api/v1/government/{slug}/project/public",
                )
            add(ROUTE_PUBLIC_STRUCTURED, f"https://api.procurement.opengov.com/api/v1/portal/{api_slug}/projects")
            add(
                ROUTE_PUBLIC_STRUCTURED,
                f"https://api.procurement.opengov.com/api/procurated/portal/{api_slug}/projects",
            )
        if slug or api_slug:
            s = slug or api_slug
            add(ROUTE_PUBLIC_EMBED, f"{base}/portal/embed/{s}/project-list")
            add(ROUTE_PUBLIC_EMBED, f"{base}/portal/{s}/project-list")
            add(ROUTE_PUBLIC_PAGE_STATE, f"{base}/portal/{s}/projects")
            add(ROUTE_PUBLIC_PAGE_STATE, f"{base}/portal/{s}?status=open")
            add(ROUTE_PUBLIC_PAGE_STATE, f"{base}/portal/{s}")
            # Auth session: prefer same project/public + project detail APIs
            if api_slug:
                add(
                    ROUTE_AUTH_SESSION,
                    f"https://api.procurement.opengov.com/api/v1/government/{api_slug}/project/public",
                )
            add(ROUTE_AUTH_SESSION, f"https://api.procurement.opengov.com/api/v1/portal/{s}/projects")
            add(ROUTE_AUTH_SESSION, f"https://api.procurement.opengov.com/api/procurated/portal/{s}/projects")
            # Do not queue HTML portal URLs under AUTH — session API only (fast, non-nav)

        portal_url = str(ent.get("portal_url") or "")
        if portal_url and "opengov.com" not in portal_url.lower():
            add(ROUTE_AGENCY_SOURCE, portal_url)
            add(ROUTE_AGENCY_SOURCE, portal_url.rstrip("/") + "/bids")
            add(ROUTE_AGENCY_SOURCE, portal_url.rstrip("/") + "/solicitations")
        if ent.get("agency_source_url"):
            add(ROUTE_AGENCY_SOURCE, str(ent["agency_source_url"]))
        if portal_url and "opengov.com" not in portal_url.lower():
            add(ROUTE_BROWSER, portal_url)
        elif slug:
            add(ROUTE_BROWSER, f"{base}/portal/embed/{slug}/project-list")

        return out

    def telemetry_summary(self) -> dict[str, Any]:
        entities = self._data.get("entities") or {}
        counts: dict[str, int] = {}
        anti_bot_primary = 0
        recovered = 0
        for ent in entities.values():
            if not isinstance(ent, dict):
                continue
            st = ent.get("status") or "UNKNOWN"
            counts[st] = counts.get(st, 0) + 1
            if ent.get("anti_bot_primary"):
                anti_bot_primary += 1
            if ent.get("recovered_via_fallback"):
                recovered += 1
        return {
            "opengov_entities_total": len(entities),
            "status_counts": counts,
            "working_structured": counts.get(WORKING_STRUCTURED, 0),
            "working_embed": counts.get(WORKING_EMBED, 0),
            "working_page_state": counts.get(WORKING_PAGE_STATE, 0),
            "working_auth_request": counts.get(WORKING_AUTH_REQUEST, 0),
            "working_agency_fallback": counts.get(WORKING_AGENCY_FALLBACK, 0),
            "working_browser": counts.get(WORKING_BROWSER, 0),
            "no_open_bids": counts.get(NO_OPEN_BIDS, 0),
            "moved": counts.get(MOVED, 0),
            "invalid": counts.get(INVALID_PORTAL, 0),
            "recovery_blocked": counts.get(RECOVERY_BLOCKED, 0),
            "anti_bot_primary_failures": anti_bot_primary,
            "anti_bot_recovered_via_fallback": recovered,
        }
