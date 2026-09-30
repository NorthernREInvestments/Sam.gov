"""M3 Opportunity Identity Graph — BUILD 1 foundation.

Thin bridge across DiscoveredOpportunity, pipeline rows, and Contract.
Does not rewrite engines, change scoring, or alter discovery behavior.
No SQL migration — durable map lives in AppSetting (optional).
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from discovery.dedup import strong_canonical_key
from solicitation_identity import identity_key, normalize_solicitation_number

BUILD_TAG = "20260918-m3-opportunity-identity-graph-1"
IDENTITY_MAP_SETTINGS_KEY = "m3_opportunity_identity_map_v1"

RESOLVED = "RESOLVED"
PARTIAL = "PARTIAL"
UNRESOLVED = "UNRESOLVED"

_NOTICE_RE = re.compile(r"^[a-f0-9]{32}$", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _alias_notice(notice_id: str) -> str:
    return f"notice:{notice_id.lower()}"


def _alias_pipeline(canonical_id: str) -> str:
    return f"pipeline:{canonical_id}"


def _alias_discovered(canonical_key: str) -> str:
    return f"discovered:{canonical_key}"


def _alias_contract(contract_id: int | str) -> str:
    return f"contract:{int(contract_id)}"


def _alias_external(source_id: str, external_id: str) -> str:
    return f"ext:{source_id}:{external_id}"


def _alias_sol(sol: str) -> str:
    return f"solnorm:{normalize_solicitation_number(sol)}"


def looks_like_sam_notice_id(value: str | None) -> bool:
    v = _clean(value)
    return bool(v and _NOTICE_RE.match(v))


def compute_pipeline_canonical_id(record: dict[str, Any]) -> str | None:
    """Same key M3PipelineStore uses — do not invent a parallel scheme."""
    try:
        return identity_key(record)
    except Exception:
        return None


def compute_discovered_canonical_key(record: dict[str, Any]) -> str | None:
    key = strong_canonical_key(record)
    if key:
        return key
    ext = _clean(record.get("external_id"))
    src = _clean(record.get("source_id")) or "unknown"
    if ext:
        return f"src:{src}|{ext}"
    return None


def empty_identity(
    *,
    opportunity_uid: str | None = None,
    status: str = UNRESOLVED,
) -> dict[str, Any]:
    return {
        "kind": "OpportunityIdentity",
        "build": BUILD_TAG,
        "opportunity_uid": opportunity_uid,
        "resolution_status": status,
        "notice_id": None,
        "pipeline_canonical_id": None,
        "discovered_canonical_key": None,
        "contract_id": None,
        "contract_notice_id": None,
        "external_id": None,
        "source_id": None,
        "solicitation_number": None,
        "aliases": [],
        "updated_at": _utc(),
    }


def _choose_uid(parts: dict[str, Any]) -> str | None:
    """Deterministic UID preference — notice > pipeline > discovered > contract > ext."""
    notice = _clean(parts.get("notice_id"))
    if notice and looks_like_sam_notice_id(notice):
        return f"opp:notice:{notice.lower()}"
    pipe = _clean(parts.get("pipeline_canonical_id"))
    if pipe:
        return f"opp:pipeline:{pipe}"
    disc = _clean(parts.get("discovered_canonical_key"))
    if disc:
        return f"opp:discovered:{disc}"
    cid = parts.get("contract_id")
    if cid is not None and str(cid).strip():
        try:
            return f"opp:contract:{int(cid)}"
        except (TypeError, ValueError):
            pass
    ext = _clean(parts.get("external_id"))
    src = _clean(parts.get("source_id")) or "unknown"
    if ext:
        return f"opp:ext:{src}:{ext}"
    return None


def _collect_aliases(parts: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    notice = _clean(parts.get("notice_id"))
    if notice:
        aliases.append(_alias_notice(notice))
    pipe = _clean(parts.get("pipeline_canonical_id"))
    if pipe:
        aliases.append(_alias_pipeline(pipe))
    disc = _clean(parts.get("discovered_canonical_key"))
    if disc:
        aliases.append(_alias_discovered(disc))
    if parts.get("contract_id") is not None and str(parts.get("contract_id")).strip():
        try:
            aliases.append(_alias_contract(parts["contract_id"]))
        except (TypeError, ValueError):
            pass
    cn = _clean(parts.get("contract_notice_id"))
    if cn and (not notice or cn.lower() != notice.lower()):
        aliases.append(_alias_notice(cn))
    ext = _clean(parts.get("external_id"))
    src = _clean(parts.get("source_id"))
    if ext and src:
        aliases.append(_alias_external(src, ext))
    sol = _clean(parts.get("solicitation_number"))
    if sol and len(normalize_solicitation_number(sol)) >= 6:
        aliases.append(_alias_sol(sol))
    # de-dupe preserve order
    return list(dict.fromkeys(aliases))


class OpportunityIdentityResolver:
    """Idempotent identity bridge. Never creates duplicate UIDs for the same alias."""

    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self._store = store or {"kind": "M3OpportunityIdentityMap", "by_alias": {}, "by_uid": {}}

    @property
    def by_alias(self) -> dict[str, str]:
        return self._store.setdefault("by_alias", {})

    @property
    def by_uid(self) -> dict[str, dict[str, Any]]:
        return self._store.setdefault("by_uid", {})

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._store)

    def identity_count(self) -> int:
        return len(self.by_uid)

    def resolve(
        self,
        *,
        source_id: str | None = None,
        notice_id: str | None = None,
        canonical_id: str | None = None,
        pipeline_id: str | None = None,
        contract_id: int | str | None = None,
        discovered_canonical_key: str | None = None,
        external_id: str | None = None,
        solicitation_number: str | None = None,
        contract_notice_id: str | None = None,
        register: bool = True,
    ) -> dict[str, Any]:
        """
        Resolve unified opportunity identity from any known handle.

        Unknown / empty inputs → UNRESOLVED (no identity created).
        """
        pipe = _clean(pipeline_id) or _clean(canonical_id)
        parts = {
            "source_id": _clean(source_id),
            "notice_id": _clean(notice_id),
            "pipeline_canonical_id": pipe,
            "discovered_canonical_key": _clean(discovered_canonical_key),
            "contract_id": contract_id,
            "contract_notice_id": _clean(contract_notice_id) or _clean(notice_id),
            "external_id": _clean(external_id),
            "solicitation_number": _clean(solicitation_number),
        }

        aliases = _collect_aliases(parts)
        if not aliases:
            return empty_identity(status=UNRESOLVED)

        # Find existing UIDs hit by any alias
        hit_uids: list[str] = []
        for a in aliases:
            uid = self.by_alias.get(a)
            if uid and uid not in hit_uids:
                hit_uids.append(uid)

        if hit_uids:
            primary = hit_uids[0]
            # Merge accidental duplicate UIDs into primary (idempotent collapse)
            for other in hit_uids[1:]:
                self._merge_uids(primary, other)
            if register:
                self._bind(primary, parts, aliases)
            return deepcopy(self.by_uid[primary])

        uid = _choose_uid(parts)
        if not uid:
            return empty_identity(status=UNRESOLVED)

        if not register:
            out = empty_identity(opportunity_uid=uid, status=PARTIAL)
            out.update({k: parts.get(k) for k in parts})
            out["aliases"] = aliases
            return out

        self._bind(uid, parts, aliases)
        return deepcopy(self.by_uid[uid])

    def _merge_uids(self, primary: str, other: str) -> None:
        if primary == other or other not in self.by_uid:
            return
        prim = self.by_uid[primary]
        oth = self.by_uid[other]
        for field in (
            "notice_id",
            "pipeline_canonical_id",
            "discovered_canonical_key",
            "contract_id",
            "contract_notice_id",
            "external_id",
            "source_id",
            "solicitation_number",
        ):
            if prim.get(field) in (None, "") and oth.get(field) not in (None, ""):
                prim[field] = oth[field]
        aliases = list(dict.fromkeys(list(prim.get("aliases") or []) + list(oth.get("aliases") or [])))
        prim["aliases"] = aliases
        prim["updated_at"] = _utc()
        for a in aliases:
            self.by_alias[a] = primary
        del self.by_uid[other]

    def _bind(self, uid: str, parts: dict[str, Any], aliases: list[str]) -> None:
        row = self.by_uid.get(uid) or empty_identity(opportunity_uid=uid, status=RESOLVED)
        row["opportunity_uid"] = uid
        for field in (
            "notice_id",
            "pipeline_canonical_id",
            "discovered_canonical_key",
            "external_id",
            "source_id",
            "solicitation_number",
            "contract_notice_id",
        ):
            if parts.get(field) not in (None, ""):
                row[field] = parts[field]
        if parts.get("contract_id") is not None and str(parts.get("contract_id")).strip():
            try:
                row["contract_id"] = int(parts["contract_id"])
            except (TypeError, ValueError):
                pass
        merged_aliases = list(dict.fromkeys(list(row.get("aliases") or []) + aliases))
        row["aliases"] = merged_aliases
        # Status: RESOLVED if we have at least one strong handle
        strong = bool(
            row.get("notice_id")
            or row.get("pipeline_canonical_id")
            or row.get("discovered_canonical_key")
            or row.get("contract_id") is not None
        )
        row["resolution_status"] = RESOLVED if strong else PARTIAL
        row["updated_at"] = _utc()
        row["build"] = BUILD_TAG
        self.by_uid[uid] = row
        for a in merged_aliases:
            self.by_alias[a] = uid

    def resolve_pipeline_row(self, row: dict[str, Any], *, register: bool = True) -> dict[str, Any]:
        cid = _clean(row.get("canonical_id")) or compute_pipeline_canonical_id(row)
        notice = _clean(row.get("notice_id"))
        ext = _clean(row.get("external_id"))
        if not notice and ext and looks_like_sam_notice_id(ext):
            notice = ext
        return self.resolve(
            source_id=row.get("source_id"),
            notice_id=notice,
            pipeline_id=cid,
            external_id=ext,
            solicitation_number=row.get("solicitation_number"),
            contract_id=row.get("contract_id"),
            register=register,
        )

    def resolve_discovered(self, row: dict[str, Any], *, register: bool = True) -> dict[str, Any]:
        key = _clean(row.get("canonical_key")) or compute_discovered_canonical_key(row)
        notice = _clean(row.get("notice_id"))
        ext = _clean(row.get("external_id"))
        if not notice and ext and looks_like_sam_notice_id(ext):
            notice = ext
        # raw metadata may hold SAM notice
        meta = row.get("raw_metadata_json") if isinstance(row.get("raw_metadata_json"), dict) else {}
        if not notice and looks_like_sam_notice_id(meta.get("notice_id") or meta.get("noticeId")):
            notice = _clean(meta.get("notice_id") or meta.get("noticeId"))
        return self.resolve(
            source_id=row.get("preferred_source_id") or row.get("source_id"),
            notice_id=notice,
            discovered_canonical_key=key,
            external_id=ext,
            solicitation_number=row.get("solicitation_number"),
            contract_id=row.get("contract_id"),
            register=register,
        )

    def resolve_contract(self, contract: Any, *, register: bool = True) -> dict[str, Any]:
        """Accept ORM Contract or dict with id/notice_id."""
        if isinstance(contract, dict):
            cid = contract.get("id") or contract.get("contract_id")
            notice = contract.get("notice_id")
            title = contract.get("title")
            sol = contract.get("solicitation_number")
            agency = contract.get("agency")
        else:
            cid = getattr(contract, "id", None)
            notice = getattr(contract, "notice_id", None)
            title = getattr(contract, "title", None)
            sol = getattr(contract, "solicitation_number", None)
            agency = getattr(contract, "agency", None)
        # Pipeline key if we can form a record (best-effort; notice alone is enough for SAM)
        pipe = None
        if notice or sol:
            pipe = compute_pipeline_canonical_id(
                {
                    "solicitation_number": sol or notice,
                    "agency": agency,
                    "external_id": notice,
                    "title": title,
                    "source_id": "fed_sam_contract_opportunities",
                }
            )
        return self.resolve(
            notice_id=notice if looks_like_sam_notice_id(str(notice or "")) else None,
            contract_id=cid,
            contract_notice_id=notice,
            pipeline_id=pipe,
            external_id=notice,
            source_id="fed_sam_contract_opportunities" if notice else None,
            solicitation_number=sol or (None if looks_like_sam_notice_id(str(notice or "")) else notice),
            register=register,
        )

    def register_contract_promotion(
        self,
        *,
        pipeline_row: dict[str, Any],
        contract_id: int,
        contract_notice_id: str | None = None,
    ) -> dict[str, Any]:
        """Link Contract to existing pipeline identity — preserves UID."""
        self.resolve_pipeline_row(pipeline_row, register=True)
        return self.resolve(
            notice_id=pipeline_row.get("notice_id"),
            pipeline_id=pipeline_row.get("canonical_id") or compute_pipeline_canonical_id(pipeline_row),
            external_id=pipeline_row.get("external_id"),
            source_id=pipeline_row.get("source_id"),
            solicitation_number=pipeline_row.get("solicitation_number"),
            contract_id=contract_id,
            contract_notice_id=contract_notice_id or pipeline_row.get("notice_id"),
            register=True,
        )


def load_identity_resolver() -> OpportunityIdentityResolver:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == IDENTITY_MAP_SETTINGS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict) and "by_alias" in data:
                    return OpportunityIdentityResolver(data)
        finally:
            db.close()
    except Exception:
        pass
    return OpportunityIdentityResolver()


def save_identity_resolver(resolver: OpportunityIdentityResolver) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = resolver.snapshot()
        payload["updated_at"] = _utc()
        payload["build"] = BUILD_TAG
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == IDENTITY_MAP_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=IDENTITY_MAP_SETTINGS_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False
