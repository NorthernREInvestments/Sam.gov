"""BUILD 19 — Data Ownership + Master Record Architecture (read models).

Governance foundation: separate raw observations from verified intelligence,
preserve lineage/history, resolve entities without auto-merge, track conflicts
and field authority with evidence.

Does NOT rebuild engines, invent facts, create numeric confidence scores, or
allow AI assumptions to become facts. UNKNOWN stays UNKNOWN. History immutable.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-master-record-1"

ST_UNKNOWN = "UNKNOWN"

ENTITY_TYPES = (
    "Company",
    "Supplier",
    "Manufacturer",
    "Distributor",
    "Product",
    "Agency",
    "Contract",
    "Person",
    "Document",
)

# Source validation
SRC_OBSERVED = "OBSERVED"
SRC_EXTRACTED = "EXTRACTED"
SRC_REVIEW = "REVIEW_REQUIRED"
SRC_VALIDATED = "VALIDATED"
SRC_REJECTED = "REJECTED"

# Entity match
MATCH_POSSIBLE = "POSSIBLE_MATCH"
MATCH_REVIEWING = "REVIEWING"
MATCH_CONFIRMED = "CONFIRMED_MATCH"
MATCH_NOT = "NOT_MATCHED"

# Information lifecycle
LIFE_RAW = "RAW_INPUT"
LIFE_OBS = "OBSERVATION"
LIFE_REVIEWED = "REVIEWED_INFORMATION"
LIFE_VERIFIED = "VERIFIED_FACT"
LIFE_HISTORICAL = "HISTORICAL_RECORD"

ENTITY_INDEX_KEY = "m3_master_entities_v1"
SOURCE_INDEX_KEY = "m3_source_records_v1"
AUTHORITY_INDEX_KEY = "m3_field_authority_v1"
MATCH_INDEX_KEY = "m3_entity_match_candidates_v1"
MERGE_INDEX_KEY = "m3_entity_merges_v1"
IDENTIFIER_INDEX_KEY = "m3_identifier_records_v1"
CONFLICT_INDEX_KEY = "m3_data_conflicts_v1"
LINEAGE_INDEX_KEY = "m3_record_lineage_v1"
LIFECYCLE_INDEX_KEY = "m3_fact_lifecycle_v1"
CHANGE_INDEX_KEY = "m3_change_records_v1"


def _utc() -> str:
    return now_utc().isoformat()


def fact(
    value: Any = None,
    *,
    source: str = "UNKNOWN",
    date: str | None = None,
    confidence: str = "UNKNOWN",
    evidence: Any = "UNKNOWN",
    status: str = ST_UNKNOWN,
    owner: str = "UNKNOWN",
) -> dict[str, Any]:
    if value is None or value == "" or str(value).upper() in {"NONE", "NULL"}:
        value = "UNKNOWN"
    if evidence is None or evidence == "":
        evidence = "UNKNOWN"
    return {
        "value": value,
        "source": source or "UNKNOWN",
        "date": date or "UNKNOWN",
        "confidence": confidence or "UNKNOWN",  # label only — not a numeric score
        "status": status or ST_UNKNOWN,
        "evidence": evidence,
        "owner": owner or "UNKNOWN",
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _unwrap(v: Any) -> Any:
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _known(v: Any) -> bool:
    return _unwrap(v) not in (None, "", "UNKNOWN")


def _entity_id(entity_type: str, canonical: str) -> str:
    base = f"{entity_type}:{canonical}".strip().lower()
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", canonical.lower()).strip("-")[:40] or "unknown"
    return f"ent:{entity_type.lower()}:{slug}:{digest}"


def _load_index(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_key", {})
                    data.setdefault("entries", [])
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"by_key": {}, "entries": []}


def _save_index(key: str, payload: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = dict(payload)
        payload["updated_at"] = _utc()
        payload["build"] = BUILD_TAG
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=key, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def _append(key: str, entry: dict[str, Any], *, by: str | None = None, cap: int = 800) -> dict[str, Any]:
    idx = _load_index(key)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-cap:]
    if by:
        bag = _as_dict(idx.get("by_key"))
        bag.setdefault(by, []).append(entry)
        bag[by] = bag[by][-100:]
        idx["by_key"] = bag
    _save_index(key, idx)
    return entry


def _upsert_entity(entity: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    idx = _load_index(ENTITY_INDEX_KEY)
    by = _as_dict(idx.get("by_key"))
    eid = entity["entity_id"]
    prev = _as_dict(by.get(eid))
    if prev:
        # Preserve created_date; never delete history of aliases/identifiers
        entity["created_date"] = prev.get("created_date") or entity.get("created_date")
        aliases = list(dict.fromkeys(list(prev.get("aliases") or []) + list(entity.get("aliases") or [])))
        entity["aliases"] = aliases
        ids = list(prev.get("identifiers") or []) + list(entity.get("identifiers") or [])
        # dedupe identifier values
        seen = set()
        deduped = []
        for i in ids:
            if not isinstance(i, dict):
                continue
            k = f"{i.get('identifier_type')}|{i.get('identifier_value')}"
            if k in seen:
                continue
            seen.add(k)
            deduped.append(i)
        entity["identifiers"] = deduped
    by[eid] = entity
    idx["by_key"] = by
    # also keep entries list for audit browse
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict) and e.get("entity_id") != eid]
    entries.append(entity)
    idx["entries"] = entries[-500:]
    if persist:
        _save_index(ENTITY_INDEX_KEY, idx)
    return entity


# ---------------------------------------------------------------------------
# BUILD 1 — Master Entity
# ---------------------------------------------------------------------------
def create_master_entity(
    *,
    entity_type: str,
    canonical_name: str,
    aliases: list[str] | None = None,
    identifiers: list[dict[str, Any]] | None = None,
    status: str = ST_UNKNOWN,
    evidence_links: list[Any] | None = None,
    owner: str = "UNKNOWN",
    persist: bool = True,
) -> dict[str, Any]:
    et = entity_type if entity_type in ENTITY_TYPES else "Company"
    name = str(canonical_name or "UNKNOWN").strip() or "UNKNOWN"
    eid = _entity_id(et, name)
    entity = {
        "kind": "M3MasterEntity",
        "entity_id": eid,
        "entity_type": et,
        "canonical_name": name,
        "aliases": [a for a in (aliases or []) if a and a != name],
        "identifiers": identifiers or [],
        "status": status if status else ST_UNKNOWN,
        "evidence_links": evidence_links or ["UNKNOWN"],
        "created_date": _utc(),
        "updated_date": _utc(),
        "owner": owner or "UNKNOWN",
        "fabricated": False,
        "ai_assumption_as_fact": False,
    }
    return _upsert_entity(entity, persist=persist)


def build_master_entities_from_row(row: dict[str, Any], *, persist: bool = False) -> list[dict[str, Any]]:
    """Derive candidate master entities from a pipeline row — observations, not auto-verified."""
    entities = []
    # Product
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    pi = _as_dict(row.get("product_identity"))
    nsn = _unwrap(fields.get("nsn") or pi.get("nsn") or struct.get("nsn"))
    pn = _unwrap(fields.get("part_number") or pi.get("part_number"))
    if _known(nsn) or _known(pn):
        entities.append(
            create_master_entity(
                entity_type="Product",
                canonical_name=str(nsn or pn),
                aliases=[str(pn)] if _known(pn) and pn != nsn else [],
                identifiers=[
                    {"identifier_type": "NSN", "identifier_value": nsn, "source": "pipeline", "validation_status": SRC_OBSERVED}
                    if _known(nsn)
                    else None,
                    {"identifier_type": "PartNumber", "identifier_value": pn, "source": "pipeline", "validation_status": SRC_OBSERVED}
                    if _known(pn)
                    else None,
                ],
                status=SRC_OBSERVED,
                evidence_links=["product_identity|dla_product_structure"],
                owner="RESEARCHER",
                persist=persist,
            )
        )
    # Clean None identifiers
    for e in entities:
        e["identifiers"] = [i for i in (e.get("identifiers") or []) if isinstance(i, dict)]

    # Suppliers from graph
    spg = _as_dict(row.get("supplier_product_graph"))
    for edge in spg.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        name = edge.get("supplier_name")
        if not _known(name):
            continue
        rel = str(edge.get("relationship_type") or "").upper()
        et = "Manufacturer" if "MANUFACTURER" in rel or rel == "OEM" else "Distributor" if "DISTRIBUTOR" in rel else "Supplier"
        entities.append(
            create_master_entity(
                entity_type=et,
                canonical_name=str(name),
                status=SRC_OBSERVED,
                evidence_links=[edge.get("source") or "supplier_product_graph"],
                owner="RESEARCHER",
                persist=persist,
            )
        )

    # Agency
    agency = row.get("agency") or row.get("buyer")
    if _known(agency):
        entities.append(
            create_master_entity(
                entity_type="Agency",
                canonical_name=str(agency),
                status=SRC_OBSERVED,
                evidence_links=["pipeline.agency"],
                owner="SYSTEM",
                persist=persist,
            )
        )

    # Contract
    cn = row.get("contract_number") or row.get("award_number")
    if _known(cn):
        entities.append(
            create_master_entity(
                entity_type="Contract",
                canonical_name=str(cn),
                identifiers=[{"identifier_type": "ContractNumber", "identifier_value": cn, "source": "pipeline", "validation_status": SRC_OBSERVED}],
                status=SRC_OBSERVED,
                evidence_links=["pipeline.award"],
                owner="MANAGER",
                persist=persist,
            )
        )

    # Documents
    for d in row.get("documents") or []:
        if isinstance(d, dict) and d.get("filename"):
            entities.append(
                create_master_entity(
                    entity_type="Document",
                    canonical_name=str(d.get("filename")),
                    status=SRC_OBSERVED,
                    evidence_links=["documents"],
                    owner="SYSTEM",
                    persist=persist,
                )
            )

    return entities


# ---------------------------------------------------------------------------
# BUILD 2 — Source Record
# ---------------------------------------------------------------------------
def record_source(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    status = str(payload.get("validation_status") or payload.get("status") or SRC_OBSERVED).upper()
    if status not in {SRC_OBSERVED, SRC_EXTRACTED, SRC_REVIEW, SRC_VALIDATED, SRC_REJECTED}:
        status = SRC_OBSERVED
    entry = {
        "kind": "M3SourceRecord",
        "source": payload.get("source") or "UNKNOWN",
        "source_type": payload.get("source_type") or "UNKNOWN",
        "original_value": payload.get("original_value") if payload.get("original_value") is not None else "UNKNOWN",
        "received_date": payload.get("received_date") or _utc(),
        "related_entity": payload.get("related_entity") or "UNKNOWN",
        "extraction_method": payload.get("extraction_method") or "UNKNOWN",
        "validation_status": status,
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "note": "External observation — not automatically an internal verified fact",
    }
    if persist:
        _append(SOURCE_INDEX_KEY, entry, by=str(entry["related_entity"]))
    return entry


def build_source_records(*, entity_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    entries = [e for e in (_load_index(SOURCE_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if entity_id:
        entries = [e for e in entries if e.get("related_entity") == entity_id]
    return {
        "kind": "M3SourceRecordSet",
        "records": entries[-limit:],
        "states": [SRC_OBSERVED, SRC_EXTRACTED, SRC_REVIEW, SRC_VALIDATED, SRC_REJECTED],
        "separates_external_from_internal": True,
    }


# ---------------------------------------------------------------------------
# BUILD 3 — Field Authority
# ---------------------------------------------------------------------------
def record_field_authority(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entity = payload.get("entity") or "UNKNOWN"
    field = payload.get("field") or "UNKNOWN"
    key = f"{entity}|{field}"
    idx = _load_index(AUTHORITY_INDEX_KEY)
    by = _as_dict(idx.get("by_key"))
    prev = _as_dict(by.get(key))
    previous_values = list(prev.get("previous_values") or [])
    if prev.get("current_value") is not None and prev.get("current_value") != payload.get("current_value"):
        previous_values.append(
            {
                "value": prev.get("current_value"),
                "source": prev.get("source"),
                "date": prev.get("updated_at") or "UNKNOWN",
                "evidence": prev.get("evidence") or "UNKNOWN",
            }
        )
    entry = {
        "kind": "M3FieldAuthority",
        "entity": entity,
        "field": field,
        "current_value": payload.get("current_value") if payload.get("current_value") is not None else "UNKNOWN",
        "source": payload.get("source") or "UNKNOWN",
        "authority_status": payload.get("authority_status") or SRC_REVIEW,
        "previous_values": previous_values[-50:],
        "change_history": list(prev.get("change_history") or []) + [
            {
                "from": prev.get("current_value") if prev else "UNKNOWN",
                "to": payload.get("current_value"),
                "reason": payload.get("reason") or "UNKNOWN",
                "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
                "date": _utc(),
                "actor": payload.get("actor") or "UNKNOWN",
            }
        ],
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "updated_at": _utc(),
        "note": "Authority determination requires evidence — sources preserved",
        "auto_selected": False,
    }
    entry["change_history"] = entry["change_history"][-50:]
    by[key] = entry
    idx["by_key"] = by
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-500:]
    if persist:
        _save_index(AUTHORITY_INDEX_KEY, idx)
    return entry


def build_field_authority(*, entity_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    by = _as_dict(_load_index(AUTHORITY_INDEX_KEY).get("by_key"))
    records = list(by.values())
    if entity_id:
        records = [r for r in records if isinstance(r, dict) and r.get("entity") == entity_id]
    return {
        "kind": "M3FieldAuthoritySet",
        "records": records[:limit],
        "note": "Multiple sources preserved; authority requires evidence",
    }


# ---------------------------------------------------------------------------
# BUILD 4 — Entity Resolution
# ---------------------------------------------------------------------------
def record_entity_match(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    status = str(payload.get("review_status") or payload.get("status") or MATCH_POSSIBLE).upper()
    if status not in {MATCH_POSSIBLE, MATCH_REVIEWING, MATCH_CONFIRMED, MATCH_NOT}:
        status = MATCH_POSSIBLE
    entry = {
        "kind": "M3EntityMatchCandidate",
        "entity_a": payload.get("entity_a") or "UNKNOWN",
        "entity_b": payload.get("entity_b") or "UNKNOWN",
        "potential_relationship": payload.get("potential_relationship") or "possible_duplicate",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "review_status": status,
        "decision": payload.get("decision") or "UNKNOWN",
        "recorded_at": _utc(),
        "auto_merged": False,
        "note": "Do not auto merge important entities",
    }
    if persist:
        _append(MATCH_INDEX_KEY, entry, by=f"{entry['entity_a']}|{entry['entity_b']}")
    return entry


def find_match_candidates(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Heuristic possible matches — never auto-merge."""
    candidates = []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in entities:
        if not isinstance(e, dict):
            continue
        by_type.setdefault(str(e.get("entity_type")), []).append(e)
    for et, group in by_type.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                na = str(a.get("canonical_name") or "").lower().strip()
                nb = str(b.get("canonical_name") or "").lower().strip()
                if not na or not nb or na == "unknown" or nb == "unknown":
                    continue
                evidence = []
                if na == nb:
                    evidence.append("identical_canonical_name")
                elif na in nb or nb in na:
                    evidence.append("substring_name_overlap")
                # identifier overlap
                ids_a = {f"{i.get('identifier_type')}|{i.get('identifier_value')}" for i in (a.get("identifiers") or []) if isinstance(i, dict)}
                ids_b = {f"{i.get('identifier_type')}|{i.get('identifier_value')}" for i in (b.get("identifiers") or []) if isinstance(i, dict)}
                overlap = ids_a & ids_b
                if overlap:
                    evidence.append(f"shared_identifiers:{','.join(list(overlap)[:3])}")
                if evidence:
                    candidates.append(
                        record_entity_match(
                            {
                                "entity_a": a.get("entity_id"),
                                "entity_b": b.get("entity_id"),
                                "potential_relationship": f"possible_duplicate_{et}",
                                "evidence": "; ".join(evidence),
                                "review_status": MATCH_POSSIBLE,
                            },
                            persist=False,
                        )
                    )
    return candidates


# ---------------------------------------------------------------------------
# BUILD 5 — Merge Workflow
# ---------------------------------------------------------------------------
def record_entity_merge(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "kind": "M3EntityMerge",
        "entities_merged": payload.get("entities_merged") or [payload.get("entity_a"), payload.get("entity_b")],
        "surviving_entity": payload.get("surviving_entity") or "UNKNOWN",
        "reason": payload.get("reason") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "reviewer": payload.get("reviewer") or "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "previous_identities_preserved": True,
        "history_deleted": False,
        "note": "Never delete history — merged records remain traceable",
    }
    if persist:
        _append(MERGE_INDEX_KEY, entry, by=str(entry["surviving_entity"]))
    return entry


def build_merge_history(*, entity_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    entries = [e for e in (_load_index(MERGE_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if entity_id:
        entries = [
            e
            for e in entries
            if e.get("surviving_entity") == entity_id or entity_id in (e.get("entities_merged") or [])
        ]
    return {
        "kind": "M3EntityMergeSet",
        "records": entries[-limit:],
        "history_immutable": True,
        "auto_merge_forbidden": True,
    }


# ---------------------------------------------------------------------------
# BUILD 6 — Identifier Management
# ---------------------------------------------------------------------------
def record_identifier(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    status = str(payload.get("validation_status") or SRC_OBSERVED).upper()
    if status not in {SRC_OBSERVED, SRC_EXTRACTED, SRC_REVIEW, SRC_VALIDATED, SRC_REJECTED}:
        status = SRC_OBSERVED
    entry = {
        "kind": "M3IdentifierRecord",
        "entity": payload.get("entity") or "UNKNOWN",
        "identifier_type": payload.get("identifier_type") or "UNKNOWN",
        "identifier_value": payload.get("identifier_value") if payload.get("identifier_value") is not None else "UNKNOWN",
        "source": payload.get("source") or "UNKNOWN",
        "date_observed": payload.get("date_observed") or _utc(),
        "validation_status": status,
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
    }
    if persist:
        _append(IDENTIFIER_INDEX_KEY, entry, by=str(entry["entity"]))
    return entry


def build_identifiers(*, entity_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    entries = [e for e in (_load_index(IDENTIFIER_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if entity_id:
        entries = [e for e in entries if e.get("entity") == entity_id]
    return {
        "kind": "M3IdentifierRecordSet",
        "records": entries[-limit:],
        "examples": {
            "Company": ["UEI", "CAGE", "Tax identifier", "Website"],
            "Product": ["NSN", "Part number", "UPC", "Manufacturer model"],
        },
    }


# ---------------------------------------------------------------------------
# BUILD 7 — Conflict Management
# ---------------------------------------------------------------------------
def record_data_conflict(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "kind": "M3DataConflict",
        "conflict_type": payload.get("conflict_type") or "UNKNOWN",
        "entities_affected": payload.get("entities_affected") or ["UNKNOWN"],
        "conflicting_values": payload.get("conflicting_values") or ["UNKNOWN"],
        "sources": payload.get("sources") or ["UNKNOWN"],
        "impact": payload.get("impact") or "UNKNOWN",
        "resolution": payload.get("resolution") or "UNKNOWN",
        "owner": payload.get("owner") or "UNKNOWN",
        "status": payload.get("status") or "OPEN",
        "recorded_at": _utc(),
        "auto_winner_selected": False,
        "note": "Do not automatically select a winner",
    }
    if persist:
        _append(CONFLICT_INDEX_KEY, entry, by=str((entry["entities_affected"] or ["UNKNOWN"])[0]))
    return entry


def detect_field_conflicts(authority_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surface conflicts from multiple previous values — no auto-resolution."""
    conflicts = []
    for rec in authority_records:
        if not isinstance(rec, dict):
            continue
        prev = rec.get("previous_values") or []
        if len(prev) >= 1 and _known(rec.get("current_value")):
            vals = [rec.get("current_value")] + [p.get("value") for p in prev if isinstance(p, dict)]
            unique = list({str(v) for v in vals if v not in (None, "", "UNKNOWN")})
            if len(unique) >= 2:
                conflicts.append(
                    record_data_conflict(
                        {
                            "conflict_type": f"field:{rec.get('field')}",
                            "entities_affected": [rec.get("entity")],
                            "conflicting_values": unique,
                            "sources": [rec.get("source")] + [p.get("source") for p in prev if isinstance(p, dict)],
                            "impact": "Authoritative value ambiguous until reviewed",
                            "owner": "MANAGER",
                            "status": "OPEN",
                        },
                        persist=False,
                    )
                )
    return conflicts


# ---------------------------------------------------------------------------
# BUILD 8 — Record Lineage
# ---------------------------------------------------------------------------
def record_lineage(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "kind": "M3RecordLineage",
        "record": payload.get("record") or "UNKNOWN",
        "parent_source": payload.get("parent_source") or "UNKNOWN",
        "transformation": payload.get("transformation") or "UNKNOWN",
        "created_by": payload.get("created_by") or "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "version": payload.get("version") or "1",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
    }
    if persist:
        _append(LINEAGE_INDEX_KEY, entry, by=str(entry["record"]))
    return entry


def build_lineage(*, record_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    entries = [e for e in (_load_index(LINEAGE_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if record_id:
        entries = [e for e in entries if e.get("record") == record_id]
    return {
        "kind": "M3RecordLineageSet",
        "records": entries[-limit:],
        "question": "Where did this come from?",
    }


# ---------------------------------------------------------------------------
# BUILD 9 — Fact vs Observation Separation
# ---------------------------------------------------------------------------
def record_lifecycle_transition(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    to_state = str(payload.get("to_state") or payload.get("state") or LIFE_OBS).upper()
    valid = {LIFE_RAW, LIFE_OBS, LIFE_REVIEWED, LIFE_VERIFIED, LIFE_HISTORICAL}
    if to_state not in valid:
        to_state = LIFE_OBS
    entry = {
        "kind": "M3FactLifecycleTransition",
        "record": payload.get("record") or "UNKNOWN",
        "from_state": payload.get("from_state") or "UNKNOWN",
        "to_state": to_state,
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "timestamp": payload.get("timestamp") or _utc(),
        "actor": payload.get("actor") or payload.get("system") or "UNKNOWN",
        "note": "AI assumptions cannot become VERIFIED_FACT without evidence + actor",
    }
    if persist:
        _append(LIFECYCLE_INDEX_KEY, entry, by=str(entry["record"]))
    return entry


def build_fact_lifecycle(*, record_id: str | None = None, limit: int = 40) -> dict[str, Any]:
    entries = [e for e in (_load_index(LIFECYCLE_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if record_id:
        entries = [e for e in entries if e.get("record") == record_id]
    return {
        "kind": "M3FactLifecycle",
        "states": [LIFE_RAW, LIFE_OBS, LIFE_REVIEWED, LIFE_VERIFIED, LIFE_HISTORICAL],
        "transitions": entries[-limit:],
        "path": "RAW_INPUT → OBSERVATION → REVIEWED_INFORMATION → VERIFIED_FACT → HISTORICAL_RECORD",
        "ai_assumption_forbidden_as_fact": True,
    }


# ---------------------------------------------------------------------------
# BUILD 10 — Change History
# ---------------------------------------------------------------------------
def record_change(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "kind": "M3ChangeRecord",
        "object": payload.get("object") or "UNKNOWN",
        "field_changed": payload.get("field_changed") or payload.get("field") or "UNKNOWN",
        "previous_value": payload.get("previous_value") if payload.get("previous_value") is not None else "UNKNOWN",
        "new_value": payload.get("new_value") if payload.get("new_value") is not None else "UNKNOWN",
        "reason": payload.get("reason") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "changed_by": payload.get("changed_by") or "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "silent_edit": False,
    }
    if persist:
        _append(CHANGE_INDEX_KEY, entry, by=str(entry["object"]))
    return entry


def build_change_history(*, object_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    entries = [e for e in (_load_index(CHANGE_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if object_id:
        entries = [e for e in entries if e.get("object") == object_id]
    return {
        "kind": "M3ChangeRecordSet",
        "records": entries[-limit:],
        "silent_edits_forbidden": True,
        "history_immutable": True,
    }


# ---------------------------------------------------------------------------
# BUILD 11 — Master Record View
# ---------------------------------------------------------------------------
def get_master_entity(entity_id: str) -> dict[str, Any] | None:
    by = _as_dict(_load_index(ENTITY_INDEX_KEY).get("by_key"))
    ent = by.get(entity_id)
    return ent if isinstance(ent, dict) else None


def build_master_record_view(entity_id: str, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Authoritative master-record API payload."""
    entity = get_master_entity(entity_id)
    if not entity:
        # Try resolve from derived pipeline entities without persisting
        if rows:
            for r in rows:
                for e in build_master_entities_from_row(r, persist=False):
                    if e.get("entity_id") == entity_id:
                        entity = e
                        break
                if entity:
                    break
    if not entity:
        return {
            "kind": "M3MasterRecordView",
            "build": BUILD_TAG,
            "entity_id": entity_id,
            "error": "entity_not_found",
            "canonical_entity": None,
            "unknowns": ["entity_not_found"],
            "read_only": True,
        }

    sources = build_source_records(entity_id=entity_id, limit=30).get("records") or []
    authority = build_field_authority(entity_id=entity_id, limit=30).get("records") or []
    conflicts = [c for c in detect_field_conflicts(authority)] + [
        e
        for e in (_load_index(CONFLICT_INDEX_KEY).get("entries") or [])
        if isinstance(e, dict) and entity_id in (e.get("entities_affected") or [])
    ]
    history = build_change_history(object_id=entity_id, limit=40).get("records") or []
    lineage = build_lineage(record_id=entity_id, limit=20).get("records") or []
    identifiers = build_identifiers(entity_id=entity_id, limit=40).get("records") or list(entity.get("identifiers") or [])
    matches = [
        e
        for e in (_load_index(MATCH_INDEX_KEY).get("entries") or [])
        if isinstance(e, dict) and entity_id in {e.get("entity_a"), e.get("entity_b")}
    ]
    merges = build_merge_history(entity_id=entity_id, limit=20).get("records") or []
    relationships = build_master_graph_connections(entity, rows=rows or [])

    unknowns = []
    if entity.get("status") in {ST_UNKNOWN, SRC_OBSERVED, SRC_REVIEW}:
        unknowns.append("entity_not_validated")
    if not identifiers:
        unknowns.append("identifiers_unknown")
    if conflicts:
        unknowns.append("open_conflicts")

    return {
        "kind": "M3MasterRecordView",
        "build": BUILD_TAG,
        "entity_id": entity_id,
        "canonical_entity": entity,
        "aliases": entity.get("aliases") or [],
        "identifiers": identifiers,
        "evidence": entity.get("evidence_links") or ["UNKNOWN"],
        "sources": sources,
        "conflicts": conflicts[:20],
        "history": history,
        "lineage": lineage,
        "relationships": relationships,
        "match_candidates": matches[-20:],
        "merges": merges,
        "field_authority": authority,
        "unknowns": unknowns or [],
        "questions": [
            "Where did this fact come from?",
            "Why is this the current value?",
            "What changed?",
            "Who changed it?",
            "What evidence supports it?",
            "What remains unknown?",
        ],
        "facts_only": True,
        "history_immutable": True,
        "no_numeric_confidence_scores": True,
        "ai_assumption_as_fact_forbidden": True,
        "generated_at": _utc(),
        "read_only": True,
    }


# ---------------------------------------------------------------------------
# BUILD 12 — Intelligence Graph Integration
# ---------------------------------------------------------------------------
def build_master_graph_connections(
    entity: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    eid = entity.get("entity_id") or "UNKNOWN"
    et = entity.get("entity_type")
    name = str(entity.get("canonical_name") or "").lower()
    nodes = [{"id": eid, "type": et, "label": entity.get("canonical_name")}]
    edges = []

    def _edge(to_id: str, to_type: str, label: str, rel: str, evidence: Any) -> None:
        if not any(n["id"] == to_id for n in nodes):
            nodes.append({"id": to_id, "type": to_type, "label": label})
        edges.append(
            {
                "from": eid,
                "to": to_id,
                "relationship": rel,
                "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
            }
        )

    for r in rows or []:
        if not isinstance(r, dict):
            continue
        # Link product/supplier/agency/contract when names match
        if et == "Product":
            for edge in _as_dict(r.get("supplier_product_graph")).get("edges") or []:
                if isinstance(edge, dict) and edge.get("supplier_name"):
                    _edge(
                        f"SUPPLIER:{edge.get('supplier_name')}",
                        "Supplier",
                        str(edge.get("supplier_name")),
                        "SUPPLIED_BY",
                        edge.get("source") or "supplier_product_graph",
                    )
            if r.get("agency"):
                _edge(f"AGENCY:{r.get('agency')}", "Agency", str(r.get("agency")), "PROCURED_BY", "pipeline.agency")
            if r.get("award_number") or r.get("contract_number"):
                cn = r.get("contract_number") or r.get("award_number")
                _edge(f"CONTRACT:{cn}", "Contract", str(cn), "ON_CONTRACT", "pipeline.award")
            if r.get("canonical_id"):
                # economic / lessons via opportunity
                _edge(f"OPP:{r.get('canonical_id')}", "Opportunity", str(r.get("canonical_id")), "RELATED_OPPORTUNITY", "pipeline")
        elif et in {"Supplier", "Manufacturer", "Distributor"} and name:
            for edge in _as_dict(r.get("supplier_product_graph")).get("edges") or []:
                if isinstance(edge, dict) and str(edge.get("supplier_name") or "").lower() == name:
                    struct = _as_dict(r.get("dla_product_structure"))
                    fields = _as_dict(struct.get("fields"))
                    nsn = _unwrap(fields.get("nsn")) or r.get("canonical_id")
                    if _known(nsn):
                        _edge(f"PRODUCT:{nsn}", "Product", str(nsn), "SUPPLIES_PRODUCT", edge.get("source") or "graph")
        elif et == "Contract":
            try:
                from m3_contract_lifecycle_read import build_contract_knowledge_graph

                g = build_contract_knowledge_graph(r)
                for rel in g.get("relationships") or []:
                    edges.append({**rel, "via": "contract_knowledge_graph"})
                for n in g.get("nodes") or []:
                    if not any(x["id"] == n.get("id") for x in nodes):
                        nodes.append(n)
            except Exception:
                pass
        elif et == "Document":
            for d in r.get("documents") or []:
                if isinstance(d, dict) and str(d.get("filename") or "").lower() == name:
                    _edge(f"OPP:{r.get('canonical_id')}", "Opportunity", str(r.get("canonical_id")), "ATTACHED_TO", "documents")

        # Lessons / economic when opportunity linked
        if r.get("canonical_id") and any(e.get("to", "").startswith("OPP:") for e in edges):
            try:
                from m3_economic_learning_read import build_operational_learning, build_economic_profile

                econ = build_economic_profile(r)
                _edge("ECON:profile", "EconomicRecord", f"status:{econ.get('status')}", "HAS_ECONOMICS", "economic_profile")
                for lesson in build_operational_learning(r, limit=3).get("entries") or []:
                    _edge(
                        f"LESSON:{lesson.get('recorded_at')}",
                        "Lesson",
                        str((lesson.get("reusable_knowledge") or ["UNKNOWN"])[0])[:40],
                        "HAS_LESSON",
                        lesson.get("evidence") or "operational_learning",
                    )
            except Exception:
                pass

    return {
        "kind": "M3MasterEntityGraph",
        "entity_id": eid,
        "nodes": nodes,
        "relationships": edges,
        "note": "Relationships require evidence",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# Opportunity / deal-room assembly
# ---------------------------------------------------------------------------
def build_governance_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    entities = build_master_entities_from_row(row, persist=False)
    matches = find_match_candidates(entities)
    authority = []
    for e in entities[:8]:
        # Seed observed field authority without claiming validated
        if e.get("entity_type") == "Product" and e.get("canonical_name"):
            authority.append(
                record_field_authority(
                    {
                        "entity": e["entity_id"],
                        "field": "canonical_name",
                        "current_value": e["canonical_name"],
                        "source": "pipeline",
                        "authority_status": SRC_OBSERVED,
                        "evidence": (e.get("evidence_links") or ["UNKNOWN"])[0],
                        "actor": "SYSTEM",
                        "reason": "Derived from opportunity row",
                    },
                    persist=False,
                )
            )
    conflicts = detect_field_conflicts(authority)
    return {
        "kind": "M3DataGovernanceProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "entities": entities,
        "match_candidates": matches,
        "field_authority": authority,
        "conflicts": conflicts,
        "sources": build_source_records(limit=10),
        "fact_lifecycle": build_fact_lifecycle(limit=10),
        "questions": [
            "What is the authoritative representation of this entity?",
            "Where did this information come from?",
            "What changed?",
            "Who approved the change?",
        ],
        "facts_only": True,
        "unknown_preserved": True,
        "history_immutable": True,
        "no_numeric_confidence_scores": True,
        "ai_assumption_as_fact_forbidden": True,
        "auto_merge_forbidden": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_governance_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["data_governance"] = build_governance_profile(row or {"canonical_id": deal.get("canonical_id")})
    except Exception:
        out["data_governance"] = {
            "kind": "M3DataGovernanceProfile",
            "build": BUILD_TAG,
            "error": "data_governance_unavailable",
            "read_only": True,
        }
    return out


# ---------------------------------------------------------------------------
# BUILD 13 — Command Center
# ---------------------------------------------------------------------------
def enrich_command_center_governance(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    conflicts = []
    duplicates = []
    stale = []
    missing_validation = []
    resolved = []
    merged = []
    verified = []
    changed = []

    # Index-backed governance signals
    for c in (_load_index(CONFLICT_INDEX_KEY).get("entries") or [])[-40:]:
        if isinstance(c, dict) and str(c.get("status") or "OPEN").upper() == "OPEN":
            conflicts.append(c)
        elif isinstance(c, dict):
            resolved.append(c)

    for m in (_load_index(MATCH_INDEX_KEY).get("entries") or [])[-40:]:
        if isinstance(m, dict) and m.get("review_status") in {MATCH_POSSIBLE, MATCH_REVIEWING}:
            duplicates.append(m)

    for e in (_load_index(MERGE_INDEX_KEY).get("entries") or [])[-20:]:
        if isinstance(e, dict):
            merged.append(e)

    for t in (_load_index(LIFECYCLE_INDEX_KEY).get("entries") or [])[-30:]:
        if isinstance(t, dict) and t.get("to_state") == LIFE_VERIFIED:
            verified.append(t)

    for ch in (_load_index(CHANGE_INDEX_KEY).get("entries") or [])[-30:]:
        if isinstance(ch, dict):
            changed.append(ch)

    # Row-derived
    for r in rows[:40]:
        if not isinstance(r, dict):
            continue
        ents = build_master_entities_from_row(r, persist=False)
        for e in ents:
            if e.get("status") in {SRC_OBSERVED, SRC_REVIEW, ST_UNKNOWN}:
                missing_validation.append(
                    {
                        "entity_id": e.get("entity_id"),
                        "name": e.get("canonical_name"),
                        "type": e.get("entity_type"),
                        "action": "Validate entity with evidence",
                        "opportunity_id": r.get("canonical_id"),
                    }
                )
        dups = find_match_candidates(ents)
        duplicates.extend(dups)
        # Stale: awarded contracts without recent update markers
        if r.get("award_number") and not r.get("updated_at") and not r.get("last_activity_at"):
            stale.append(
                {
                    "opportunity_id": r.get("canonical_id"),
                    "what": "possible_stale_record",
                    "action": "Confirm master record freshness",
                }
            )

    if period == "morning":
        out["unresolved_entity_conflicts"] = conflicts[:15]
        out["duplicate_candidates"] = duplicates[:15]
        out["stale_records"] = stale[:15]
        out["missing_validation"] = missing_validation[:15]
    else:
        out["resolved_conflicts"] = resolved[:15]
        out["merged_entities"] = merged[:15]
        out["new_verified_facts"] = verified[:15]
        out["changed_master_records"] = changed[:15]
    return out
