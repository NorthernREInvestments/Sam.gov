"""ProcurementSourceRegistry — national registry of public procurement sources."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from discovery.selection import _pool_map
from national_discovery_constants import (
    DEFAULT_OVERLAP_HOURS,
    ENTITY_TYPES,
    SRC_AUTH,
    SRC_DEAD,
    SRC_DEGRADED,
    SRC_DISCOVERED_UNVALIDATED,
    SRC_DUPLICATE,
    SRC_HEALTHY,
    SRC_NOT_SOLICITATION,
    SRC_QUARANTINED,
    SRC_REPLACED,
    SRC_UNKNOWN,
    SRC_VALIDATING,
)
from source_health import classify_source_failure, recommend_retry

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "procurement_source_registry.json"


def _utc() -> str:
    return now_utc().isoformat()


def _kind_to_entity(kind: str | None, name: str = "") -> str:
    k = (kind or "").upper()
    n = (name or "").lower()
    if k == "STATE":
        return "STATE"
    if k == "FEDERAL":
        return "FEDERAL"
    if k == "COOPERATIVE":
        return "COOPERATIVE"
    if "airport" in n:
        return "AIRPORT"
    if "county" in n:
        return "COUNTY"
    if "university" in n or "college" in n:
        return "HIGHER_EDUCATION"
    if "school" in n or "k-12" in n or "k12" in n:
        return "K12_SCHOOL_DISTRICT"
    if "transit" in n or "metro" in n:
        return "TRANSIT"
    if "port" in n:
        return "PORT_AUTHORITY"
    if "housing" in n:
        return "HOUSING_AUTHORITY"
    if "city" in n or "municipal" in n:
        return "CITY_MUNICIPAL"
    if "dot" in n or "transport" in n:
        return "DOT_TRANSPORTATION"
    return "OTHER_PUBLIC"


def source_record(
    *,
    source_id: str,
    source_name: str,
    discovery_url: str | None = None,
    canonical_base_url: str | None = None,
    platform_family: str | None = None,
    adapter_family: str | None = None,
    government_level: str | None = None,
    entity_type: str | None = None,
    geographic_scope: str | None = None,
    jurisdiction: str | None = None,
    health_state: str = SRC_UNKNOWN,
    priority: int = 50,
    auth_requirement: str = "NONE",
    provenance: str = "seeded_from_discovery_matrix",
    **extra: Any,
) -> dict[str, Any]:
    url = discovery_url or canonical_base_url or ""
    base = canonical_base_url
    if not base and url:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}/" if p.netloc else url
    now = _utc()
    row = {
        "source_id": source_id,
        "source_name": source_name,
        "source_family": platform_family or adapter_family or "OTHER",
        "platform_family": platform_family,
        "adapter_family": adapter_family,
        "government_level": government_level,
        "entity_type": entity_type or "OTHER_PUBLIC",
        "geographic_scope": geographic_scope,
        "jurisdiction": jurisdiction,
        "canonical_base_url": base,
        "discovery_url": discovery_url,
        "source_recipe": extra.pop("source_recipe", None),
        "access_method": extra.pop("access_method", "PUBLIC_HTTP"),
        "pagination_method": extra.pop("pagination_method", "UNKNOWN"),
        "incremental_method": extra.pop("incremental_method", "OVERLAP_WINDOW"),
        "last_successful_checkpoint": None,
        "last_attempted_checkpoint": None,
        "last_success_at": None,
        "last_failure_at": None,
        "health_state": health_state,
        "failure_class": None,
        "expected_update_behavior": extra.pop("expected_update_behavior", "DAILY_LISTING"),
        "supports_modified_since": False,
        "supports_created_since": False,
        "supports_status_filter": True,
        "supports_pagination": True,
        "supports_detail_fetch": True,
        "supports_attachment_fetch": True,
        "supports_amendment_detection": True,
        "supports_award_status_tracking": False,
        "auth_requirement": auth_requirement,
        "anti_automation_state": "NONE",
        "request_cost_class": extra.pop("request_cost_class", "MEDIUM"),
        "priority": priority,
        "confidence": extra.pop("confidence", "MEDIUM"),
        "first_discovered_at": now,
        "last_validated_at": None,
        "replacement_source_id": None,
        "overlap_hours": DEFAULT_OVERLAP_HOURS,
        "notes": extra.pop("notes", None),
        "provenance": provenance,
        "cycle_status": None,
        "failure_history": [],
    }
    row.update(extra)
    return row


class ProcurementSourceRegistry:
    """Persistent compact registry — no bulk page storage."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._sources: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for row in data.get("sources") or []:
                if isinstance(row, dict) and row.get("source_id"):
                    self._sources[row["source_id"]] = row

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "ProcurementSourceRegistry",
            "updated_at": _utc(),
            "source_count": len(self._sources),
            "sources": sorted(self._sources.values(), key=lambda s: s["source_id"]),
        }
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self, source_id: str) -> dict[str, Any] | None:
        return deepcopy(self._sources.get(source_id))

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        sid = record["source_id"]
        existing = self._sources.get(sid)
        if existing:
            merged = dict(existing)
            for k, v in record.items():
                if v is not None and k not in {"first_discovered_at"}:
                    merged[k] = v
            self._sources[sid] = merged
        else:
            self._sources[sid] = dict(record)
        return deepcopy(self._sources[sid])

    def all_sources(self) -> list[dict[str, Any]]:
        return [deepcopy(s) for s in sorted(self._sources.values(), key=lambda x: x["source_id"])]

    def by_health(self, *states: str) -> list[dict[str, Any]]:
        return [s for s in self.all_sources() if s.get("health_state") in states]

    def healthy_production(self) -> list[dict[str, Any]]:
        return self.by_health(SRC_HEALTHY, SRC_DEGRADED)

    def seed_from_discovery_pool(self, *, health_overrides: dict[str, str] | None = None) -> int:
        """Bootstrap registry from existing discovery matrix/selection pool."""
        overrides = health_overrides or {}
        added = 0
        for sid, meta in _pool_map().items():
            if sid in self._sources:
                continue
            auth = "NONE"
            health = overrides.get(sid, SRC_UNKNOWN)
            if meta.get("adapter_status") == "LIVE_VERIFIED":
                health = SRC_HEALTHY
            name = str(meta.get("name") or sid)
            entity = _kind_to_entity(meta.get("kind"), name)
            # Known auth-gated from prior source health
            if sid == "state_wv":
                auth = "LOGIN_REQUIRED"
                health = SRC_AUTH
            rec = source_record(
                source_id=sid,
                source_name=name,
                discovery_url=meta.get("list_url"),
                platform_family=meta.get("platform_family"),
                adapter_family=meta.get("adapter_family"),
                government_level=meta.get("kind"),
                entity_type=entity,
                geographic_scope=meta.get("state_code"),
                jurisdiction=meta.get("state_code"),
                health_state=health,
                priority=30 if meta.get("validation_candidate") else 60,
                auth_requirement=auth,
                provenance="seeded_from_discovery_matrix",
            )
            self._sources[sid] = rec
            added += 1
        return added

    def register_discovered_candidate(
        self,
        *,
        source_id: str,
        source_name: str,
        discovery_url: str,
        entity_type: str = "OTHER_PUBLIC",
        platform_family: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """New sources enter DISCOVERED_UNVALIDATED — never auto-trusted."""
        # Duplicate URL detection
        for existing in self._sources.values():
            if (existing.get("discovery_url") or "").rstrip("/") == discovery_url.rstrip("/"):
                return self.upsert(
                    {
                        **existing,
                        "health_state": SRC_DUPLICATE,
                        "notes": f"duplicate_of:{existing['source_id']}",
                    }
                )
            if existing["source_id"] == source_id:
                return deepcopy(existing)
        rec = source_record(
            source_id=source_id,
            source_name=source_name,
            discovery_url=discovery_url,
            entity_type=entity_type if entity_type in ENTITY_TYPES else "OTHER_PUBLIC",
            platform_family=platform_family,
            health_state=SRC_DISCOVERED_UNVALIDATED,
            provenance="source_discovery_engine",
            notes=notes,
            confidence="LOW",
            priority=80,
        )
        return self.upsert(rec)

    def begin_validation(self, source_id: str) -> dict[str, Any]:
        row = self._sources[source_id]
        row["health_state"] = SRC_VALIDATING
        return deepcopy(row)

    def promote_healthy(self, source_id: str, *, notes: str | None = None) -> dict[str, Any]:
        row = self._sources[source_id]
        row["health_state"] = SRC_HEALTHY
        row["last_validated_at"] = _utc()
        row["last_success_at"] = _utc()
        row["failure_class"] = None
        if notes:
            row["notes"] = notes
        return deepcopy(row)

    def quarantine(self, source_id: str, *, reason: str, failure_class: str | None = None) -> dict[str, Any]:
        row = self._sources[source_id]
        row["health_state"] = SRC_QUARANTINED
        row["failure_class"] = failure_class or reason
        row["last_failure_at"] = _utc()
        row.setdefault("failure_history", []).append({"at": _utc(), "reason": reason})
        return deepcopy(row)

    def mark_not_solicitation(self, source_id: str) -> dict[str, Any]:
        row = self._sources[source_id]
        row["health_state"] = SRC_NOT_SOLICITATION
        row["last_validated_at"] = _utc()
        return deepcopy(row)

    def mark_migrated(self, old_id: str, new_id: str) -> dict[str, Any]:
        old = self._sources[old_id]
        old["health_state"] = SRC_REPLACED
        old["replacement_source_id"] = new_id
        old["last_validated_at"] = _utc()
        return deepcopy(old)

    def mark_dead(self, source_id: str) -> dict[str, Any]:
        row = self._sources[source_id]
        row["health_state"] = SRC_DEAD
        row["last_failure_at"] = _utc()
        return deepcopy(row)

    def record_attempt(
        self,
        source_id: str,
        *,
        attempted_checkpoint: str,
        success: bool,
        successful_checkpoint: str | None = None,
        failure_class: str | None = None,
        cycle_complete: bool = True,
        records_seen: int = 0,
    ) -> dict[str, Any]:
        """CRITICAL: failed source must NOT advance last_successful_checkpoint."""
        row = self._sources.setdefault(
            source_id,
            source_record(source_id=source_id, source_name=source_id, health_state=SRC_UNKNOWN),
        )
        row["last_attempted_checkpoint"] = attempted_checkpoint
        row["cycle_status"] = "COMPLETE" if cycle_complete else "INCOMPLETE"
        if success:
            if successful_checkpoint is not None:
                row["last_successful_checkpoint"] = successful_checkpoint
            row["last_success_at"] = _utc()
            if row.get("health_state") in {SRC_DEGRADED, SRC_UNKNOWN, SRC_VALIDATING}:
                row["health_state"] = SRC_HEALTHY
            row["failure_class"] = None
            row["last_validated_at"] = _utc()
        else:
            row["last_failure_at"] = _utc()
            row["failure_class"] = failure_class or SRC_UNKNOWN
            hist = list(row.get("failure_history") or [])
            hist.append({"at": _utc(), "failure_class": failure_class, "records_seen": records_seen})
            row["failure_history"] = hist[-20:]
            # Transient failure → DEGRADED, not permanent blacklist
            if row.get("health_state") == SRC_HEALTHY:
                row["health_state"] = SRC_DEGRADED
            if failure_class == "AUTH_REQUIRED":
                row["health_state"] = SRC_AUTH
        return deepcopy(row)

    def apply_discovery_health(self, per_source: dict[str, Any]) -> None:
        from discovery.source_backoff import compute_backoff_until
        from discovery.source_failure_taxonomy import classify_root_cause

        for sid, metrics in (per_source or {}).items():
            if sid not in self._sources:
                continue
            row_m = metrics if isinstance(metrics, dict) else {}
            ftype = classify_source_failure(row_m)
            # Prefer precise taxonomy when present
            precise = row_m.get("root_cause") or classify_root_cause(row_m).get("primary")
            if ftype == "OK" or (row_m.get("ok") and int(row_m.get("raw") or 0) > 0):
                self.record_attempt(
                    sid,
                    attempted_checkpoint=_utc(),
                    success=True,
                    successful_checkpoint=_utc(),
                    cycle_complete=True,
                    records_seen=int(row_m.get("unique") or 0),
                )
                self._sources[sid]["backoff_until"] = None
                self._sources[sid]["root_cause"] = "OK"
                self._sources[sid]["productive"] = True
            else:
                fail_class = precise if precise and precise != "OK" else ftype
                self.record_attempt(
                    sid,
                    attempted_checkpoint=_utc(),
                    success=False,
                    failure_class=str(fail_class),
                    cycle_complete=True,
                )
                self._sources[sid]["recommended_retry"] = recommend_retry(ftype)
                self._sources[sid]["root_cause"] = fail_class
                self._sources[sid]["productive"] = False
                self._sources[sid]["access_outcome"] = row_m.get("access_outcome")
                try:
                    self._sources[sid]["backoff_until"] = compute_backoff_until(str(fail_class))
                    self._sources[sid]["backoff_hours"] = row_m.get("backoff_hours")
                except Exception:
                    pass


    def coverage_summary(self) -> dict[str, Any]:
        by_entity: dict[str, int] = {}
        by_health: dict[str, int] = {}
        by_platform: dict[str, int] = {}
        for s in self.all_sources():
            by_entity[s.get("entity_type") or "OTHER"] = by_entity.get(s.get("entity_type") or "OTHER", 0) + 1
            by_health[s.get("health_state") or "UNKNOWN"] = by_health.get(s.get("health_state") or "UNKNOWN", 0) + 1
            fam = s.get("platform_family") or s.get("source_family") or "OTHER"
            by_platform[fam] = by_platform.get(fam, 0) + 1
        return {
            "kind": "SourceCoverageReport",
            "KNOWN_SOURCE_COVERAGE": {
                "total_sources": len(self._sources),
                "healthy_production": len(self.by_health(SRC_HEALTHY)),
                "degraded": len(self.by_health(SRC_DEGRADED)),
                "by_entity_type": by_entity,
                "by_health_state": by_health,
                "by_platform_family": by_platform,
            },
            "note": "KNOWN_SOURCE_COVERAGE only — not claimed as 100% national coverage",
        }


def bootstrap_registry(path: Path | None = None) -> ProcurementSourceRegistry:
    reg = ProcurementSourceRegistry(path=path)
    reg.seed_from_discovery_pool()
    # Promote known productive SciQuest / prior healthy sources
    for sid in ("state_ia", "state_mt", "state_tx", "state_ne", "coop_sourcewell_live", "agency_city_phoenix_az"):
        if reg.get(sid):
            reg.promote_healthy(sid, notes="prior_live_validation_productive")
    if reg.get("state_id"):
        reg.promote_healthy("state_id", notes="url_repaired_became_productive")
    if reg.get("state_wv"):
        reg.quarantine("state_wv", reason="auth_bulletin_only", failure_class="AUTH_REQUIRED")
    # Wire reusable family adapters onto registered sources
    from source_network_audit import sync_adapter_families

    sync_adapter_families(reg)
    reg.save()
    return reg
