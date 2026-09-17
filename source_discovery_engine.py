"""SourceDiscoveryEngine — find procurement sources M3 does not already know."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from application_clock import now_utc
from national_discovery_constants import (
    DEFAULT_WEEKLY_SOURCE_DISCOVERY_DAYS,
    SRC_DEAD,
    SRC_DEGRADED,
    SRC_DISCOVERED_UNVALIDATED,
    SRC_HEALTHY,
    SRC_NOT_SOLICITATION,
    SRC_QUARANTINED,
)
from procurement_source_registry import ProcurementSourceRegistry

# Legitimate public discovery patterns — not exhaustive, not auto-trusted
_PORTAL_HINTS = re.compile(
    r"(bid|rfp|rfq|ifb|solicitation|procurement|purchasing|vendor\s+opportunit|"
    r"open\s+bids?|current\s+bids?|bid\s+opportunit|invitation\s+for\s+bid)",
    re.I,
)
_JUNK = re.compile(
    r"(career|job\s+opening|linkedin|facebook|twitter|instagram|wikipedia|"
    r"amazon\.com|ebay\.com|youtube|tiktok|login\s+only\s+portal)",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _candidate_id(url: str) -> str:
    host = urlparse(url).netloc.replace(".", "_").replace("-", "_")[:40]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"discovered_{host}_{digest}"


def score_discovery_candidate(url: str, title: str = "", snippet: str = "") -> dict[str, Any]:
    blob = f"{url} {title} {snippet}"
    if _JUNK.search(blob):
        return {"accept": False, "reason": "junk_or_non_procurement"}
    if not _PORTAL_HINTS.search(blob):
        return {"accept": False, "reason": "no_procurement_signal"}
    return {"accept": True, "reason": "procurement_signal_present"}


def validate_source_candidate(
    *,
    listing_html: str | None,
    status_code: int | None,
    url: str,
) -> dict[str, Any]:
    """Lightweight validation — does this look like a public solicitation listing?"""
    if status_code and status_code in {401, 403}:
        return {"state": "AUTH_REQUIRED", "promote": False}
    if status_code and status_code >= 400:
        return {"state": "DEAD_OR_CHANGED", "promote": False}
    text = listing_html or ""
    if not text:
        return {"state": "UNKNOWN", "promote": False}
    low = text.lower()
    if "captcha" in low and len(text) < 5000:
        return {"state": "ANTI_AUTOMATION", "promote": False}
    # Count solicitation-like signals
    signals = len(re.findall(r"\b(rfp|rfq|ifb|rfb|solicitation|bid\s*#|invitation)\b", text, re.I))
    links = len(re.findall(r"href=", text, re.I))
    if signals >= 2 or (signals >= 1 and links >= 5):
        return {"state": "HEALTHY_CANDIDATE", "promote": True, "signals": signals}
    if "login" in low and signals == 0:
        return {"state": "AUTH_REQUIRED", "promote": False}
    if signals == 0 and "procurement" not in low and "purchasing" not in low:
        return {"state": "NOT_A_SOLICITATION_SOURCE", "promote": False}
    return {"state": "QUARANTINE_AMBIGUOUS", "promote": False, "signals": signals}


class SourceDiscoveryEngine:
    """Discover unknown sources; quarantine until validated. No auth bypass."""

    def __init__(self, registry: ProcurementSourceRegistry) -> None:
        self.registry = registry

    def known_urls(self) -> set[str]:
        urls = set()
        for s in self.registry.all_sources():
            for key in ("discovery_url", "canonical_base_url"):
                u = s.get(key)
                if u:
                    urls.add(u.rstrip("/").lower())
        return urls

    def ingest_search_hits(self, hits: list[dict[str, Any]]) -> dict[str, Any]:
        """
        hits: [{url, title, snippet, entity_type?}]
        Does not auto-promote. Returns discovery stats.
        """
        known = self.known_urls()
        discovered = []
        rejected = []
        duplicates = []
        for hit in hits:
            url = str(hit.get("url") or "").strip()
            if not url.startswith("http"):
                rejected.append({"url": url, "reason": "invalid_url"})
                continue
            if url.rstrip("/").lower() in known:
                duplicates.append(url)
                continue
            scored = score_discovery_candidate(url, hit.get("title") or "", hit.get("snippet") or "")
            if not scored["accept"]:
                rejected.append({"url": url, "reason": scored["reason"]})
                continue
            sid = _candidate_id(url)
            row = self.registry.register_discovered_candidate(
                source_id=sid,
                source_name=hit.get("title") or urlparse(url).netloc,
                discovery_url=url,
                entity_type=hit.get("entity_type") or "OTHER_PUBLIC",
                platform_family=hit.get("platform_family"),
                notes=f"discovered_via:{hit.get('discovery_method') or 'public_search'}",
            )
            if row.get("health_state") == "DUPLICATE_SOURCE":
                duplicates.append(url)
            else:
                discovered.append(row["source_id"])
                known.add(url.rstrip("/").lower())
        return {
            "discovered": discovered,
            "rejected": rejected,
            "duplicates": duplicates,
            "at": _utc(),
        }

    def validate_and_maybe_promote(
        self,
        source_id: str,
        *,
        listing_html: str | None = None,
        status_code: int | None = None,
    ) -> dict[str, Any]:
        row = self.registry.get(source_id)
        if not row:
            return {"error": "unknown_source"}
        self.registry.begin_validation(source_id)
        result = validate_source_candidate(
            listing_html=listing_html,
            status_code=status_code,
            url=row.get("discovery_url") or "",
        )
        if result.get("promote"):
            promoted = self.registry.promote_healthy(source_id, notes="validated_public_listing_signals")
            return {"action": "PROMOTED", "source": promoted, "validation": result}
        state = result.get("state")
        if state == "NOT_A_SOLICITATION_SOURCE":
            return {"action": "REJECTED", "source": self.registry.mark_not_solicitation(source_id), "validation": result}
        if state == "AUTH_REQUIRED":
            return {
                "action": "AUTH",
                "source": self.registry.quarantine(source_id, reason="auth", failure_class="AUTH_REQUIRED"),
                "validation": result,
            }
        if state == "DEAD_OR_CHANGED":
            return {"action": "DEAD", "source": self.registry.mark_dead(source_id), "validation": result}
        return {
            "action": "QUARANTINED",
            "source": self.registry.quarantine(source_id, reason=state or "ambiguous"),
            "validation": result,
        }

    def detect_migration(self, old_source_id: str, new_url: str, *, new_name: str | None = None) -> dict[str, Any]:
        new_id = _candidate_id(new_url)
        self.registry.register_discovered_candidate(
            source_id=new_id,
            source_name=new_name or f"Migration of {old_source_id}",
            discovery_url=new_url,
            notes=f"migration_from:{old_source_id}",
        )
        self.registry.mark_migrated(old_source_id, new_id)
        return {"old": old_source_id, "new": new_id, "url": new_url}

    def recovery_candidates(self) -> list[dict[str, Any]]:
        """Unhealthy sources worth retesting — not permanent blacklist."""
        out = []
        for s in self.registry.all_sources():
            if s.get("health_state") in {SRC_DEGRADED, SRC_QUARANTINED, SRC_DEAD}:
                hist = s.get("failure_history") or []
                out.append({**s, "retry_recommended": len(hist) < 10})
        return out


def run_weekly_source_discovery(
    registry: ProcurementSourceRegistry,
    *,
    search_hits: list[dict[str, Any]] | None = None,
    probe_results: dict[str, dict[str, Any]] | None = None,
    cadence_days: int = DEFAULT_WEEKLY_SOURCE_DISCOVERY_DAYS,
) -> dict[str, Any]:
    """
    Callable production job for later scheduling.
    search_hits: optional externally gathered legitimate public search results.
    probe_results: source_id -> {listing_html, status_code} for validation/recovery.
    """
    engine = SourceDiscoveryEngine(registry)
    ingest = engine.ingest_search_hits(search_hits or [])
    promoted = []
    quarantined = []
    rejected = []
    recovered = []
    for sid, probe in (probe_results or {}).items():
        out = engine.validate_and_maybe_promote(
            sid,
            listing_html=probe.get("listing_html"),
            status_code=probe.get("status_code"),
        )
        action = out.get("action")
        if action == "PROMOTED":
            if sid in [c["source_id"] for c in engine.recovery_candidates()]:
                recovered.append(sid)
            promoted.append(sid)
        elif action in {"QUARANTINED", "AUTH"}:
            quarantined.append(sid)
        elif action in {"REJECTED", "DEAD"}:
            rejected.append(sid)
    registry.save()
    coverage = registry.coverage_summary()
    return {
        "kind": "WeeklySourceDiscoveryReport",
        "cadence_days": cadence_days,
        "at": _utc(),
        "candidate_sources_discovered": len(ingest.get("discovered") or []),
        "duplicates": len(ingest.get("duplicates") or []),
        "rejected_hits": len(ingest.get("rejected") or []),
        "validated_promoted": promoted,
        "quarantined": quarantined,
        "rejected_sources": rejected,
        "recovered": recovered,
        "DISCOVERED_SOURCE_COVERAGE": {
            "unvalidated": len(registry.by_health(SRC_DISCOVERED_UNVALIDATED)),
            "healthy": len(registry.by_health(SRC_HEALTHY)),
        },
        "KNOWN_SOURCE_COVERAGE": coverage["KNOWN_SOURCE_COVERAGE"],
        "note": "Does not claim 100% national coverage",
    }
