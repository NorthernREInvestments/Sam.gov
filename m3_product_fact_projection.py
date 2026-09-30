"""BUILD 2 — Product fact projection: pipeline JSON → reusable Knowledge* intelligence.

Does not rewrite identity engines. Does not remove pipeline JSON.
Projects only validated / research-ready facts. Idempotent. No bulk historical sweep.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from federal_dla_product_constants import (
    ID_AMBIGUOUS,
    ID_CATEGORY_IDENTIFIED,
    ID_EXACT_APPROVED_SOURCE_PART,
    ID_EXACT_NSN,
    ID_EXACT_OEM_PART,
    ID_UNKNOWN,
    READY_COMMERCIAL_RESEARCH,
    READY_HISTORICAL_RESEARCH,
    READY_PRODUCT_IDENTITY_EXACT,
    READY_TRANSACTION_STRUCTURE_COMPLETE,
)
from m3_opportunity_identity import OpportunityIdentityResolver, load_identity_resolver

BUILD_TAG = "20260918-m3-product-fact-projection-1"
PROJECTION_INDEX_KEY = "m3_product_fact_projection_index_v1"

# M3 intelligence states (mapping layer — do not delete engine-specific states)
INTEL_UNKNOWN = "UNKNOWN"
INTEL_RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
INTEL_POSSIBLE = "POSSIBLE"
INTEL_VALIDATED = "VALIDATED"
INTEL_AVAILABLE = "AVAILABLE"
INTEL_EXECUTION_READY = "EXECUTION_READY"

PROMOTABLE_IDENTITY_STATES = {
    ID_EXACT_NSN,
    ID_EXACT_OEM_PART,
    ID_EXACT_APPROVED_SOURCE_PART,
}

# Weak / non-promotable
BLOCKED_IDENTITY_STATES = {
    ID_UNKNOWN,
    ID_AMBIGUOUS,
    ID_CATEGORY_IDENTIFIED,
    "SPEC_IDENTIFIED",
    "CATEGORY_IDENTIFIED",
}


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def map_to_intelligence_state(raw: Any) -> str:
    """Map existing engine vocabularies → M3 intelligence states (additive mapping only)."""
    s = str(raw or "").strip().upper()
    if not s or s in {"UNKNOWN", "NONE", "NULL"}:
        return INTEL_UNKNOWN
    if s in {
        ID_UNKNOWN,
        "DESCRIPTION_EMPTY",
        "DISCOVERED_ONLY",
        "BLOCKED_BY_MISSING_PUBLIC_PACKAGE",
    }:
        return INTEL_UNKNOWN
    if s in {
        "RESEARCH_REQUIRED",
        "PRODUCT_IDENTITY_PARTIAL",
        "PACKAGE_PARTIAL",
        "DESCRIPTION_RECOVERED",
        "TRANSACTION_STRUCTURE_PARTIAL",
        "HUMAN_DOCUMENT_ACCESS_EVENTUALLY_REQUIRED",
        "ECONOMIC_RESEARCH_POTENTIAL_DOCUMENT_ACCESS_REQUIRED",
    }:
        return INTEL_RESEARCH_REQUIRED
    if s in {
        "POSSIBLE",
        ID_AMBIGUOUS,
        ID_CATEGORY_IDENTIFIED,
        "SPEC_IDENTIFIED",
        "MEDIUM",
        "LOW",
    }:
        return INTEL_POSSIBLE
    if s in {
        "VALIDATED",
        "HIGH",
        ID_EXACT_NSN,
        ID_EXACT_OEM_PART,
        ID_EXACT_APPROVED_SOURCE_PART,
        READY_PRODUCT_IDENTITY_EXACT,
        READY_HISTORICAL_RESEARCH,
        "HISTORICAL_RESEARCH_READY",
    }:
        return INTEL_VALIDATED
    if s in {
        "AVAILABLE",
        READY_COMMERCIAL_RESEARCH,
        "COMMERCIAL_RESEARCH_READY",
        READY_TRANSACTION_STRUCTURE_COMPLETE,
        "PACKAGE_RECOVERED",
    }:
        return INTEL_AVAILABLE
    if s in {
        "EXECUTION_READY",
        "READY_FOR_BID_PREPARATION",
        "OPERATOR_READY",
    }:
        return INTEL_EXECUTION_READY
    if "READY" in s and "RESEARCH" in s:
        return INTEL_AVAILABLE
    if "EXACT" in s:
        return INTEL_VALIDATED
    return INTEL_POSSIBLE


def _struct(row: dict[str, Any]) -> dict[str, Any]:
    s = row.get("dla_product_structure")
    if isinstance(s, dict) and s:
        return s
    return {}


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    pi = row.get("product_identity")
    if isinstance(pi, dict) and pi:
        return pi
    return {}


def _field_value(struct: dict[str, Any], key: str) -> Any:
    """Prefer flat struct value; fall back to fields[key].value evidence wrapper."""
    if struct.get(key) not in (None, ""):
        return struct.get(key)
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}
    wrap = fields.get(key)
    if isinstance(wrap, dict) and wrap.get("value") not in (None, ""):
        return wrap.get("value")
    return None


def _field_evidence(struct: dict[str, Any], key: str) -> dict[str, Any]:
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}
    wrap = fields.get(key)
    if isinstance(wrap, dict):
        return {
            "confidence": wrap.get("confidence") or "HIGH",
            "evidence_source": wrap.get("evidence_source") or wrap.get("source"),
            "evidence_snippet": wrap.get("evidence_snippet") or wrap.get("snippet"),
            "extraction_method": wrap.get("extraction_method") or "regex",
        }
    return {"confidence": "HIGH", "evidence_source": "dla_product_structure", "evidence_snippet": None, "extraction_method": "structure"}


def is_eligible_for_projection(row: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Only newly enriched / research-ready / explicit force — never bulk historical."""
    if force:
        return {"eligible": True, "reason": "force"}
    readiness = str(row.get("readiness_state") or (row.get("product_transaction_readiness") or {}).get("readiness_state") or "")
    ident = _identity(row)
    state = str(ident.get("identity_state") or "")
    struct = _struct(row)
    if readiness in {
        READY_COMMERCIAL_RESEARCH,
        READY_HISTORICAL_RESEARCH,
        READY_PRODUCT_IDENTITY_EXACT,
        READY_TRANSACTION_STRUCTURE_COMPLETE,
        "COMMERCIAL_RESEARCH_READY",
        "HISTORICAL_RESEARCH_READY",
    }:
        return {"eligible": True, "reason": f"readiness:{readiness}"}
    if state in PROMOTABLE_IDENTITY_STATES:
        return {"eligible": True, "reason": f"identity:{state}"}
    if struct.get("has_exact_nsn") or (struct.get("has_exact_pn") and _field_evidence(struct, "part_number").get("confidence") == "HIGH"):
        return {"eligible": True, "reason": "exact_structure_flags"}
    if row.get("enrichment") and (struct.get("has_exact_nsn") or struct.get("has_exact_pn")):
        return {"eligible": True, "reason": "enrichment_with_exact_facts"}
    return {"eligible": False, "reason": "not_research_ready_or_exact"}


def extract_promotable_facts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic promotion candidates — no guesses, no category-only identities."""
    struct = _struct(row)
    ident = _identity(row)
    state = str(ident.get("identity_state") or "")
    facts: list[dict[str, Any]] = []

    if state in BLOCKED_IDENTITY_STATES and not struct.get("has_exact_nsn") and not struct.get("has_exact_pn"):
        return []

    nsn = _clean(_field_value(struct, "nsn") or row.get("exact_nsn"))
    if nsn and (struct.get("has_exact_nsn") or state == ID_EXACT_NSN):
        ev = _field_evidence(struct, "nsn")
        if str(ev.get("confidence") or "HIGH").upper() in {"HIGH", "VALIDATED"}:
            facts.append(
                {
                    "fact_type": "nsn",
                    "value": nsn,
                    "intelligence_state": INTEL_VALIDATED,
                    **ev,
                }
            )

    pn = _clean(_field_value(struct, "part_number") or row.get("exact_part_number"))
    pn_ev = _field_evidence(struct, "part_number")
    # Reject weak title-token PNs (MEDIUM without PART/P/N context already marked in extract)
    if pn and (struct.get("has_exact_pn") or state in {ID_EXACT_OEM_PART, ID_EXACT_APPROVED_SOURCE_PART}):
        if str(pn_ev.get("confidence") or "").upper() == "HIGH" or state in {
            ID_EXACT_OEM_PART,
            ID_EXACT_APPROVED_SOURCE_PART,
        }:
            # Extra guard: skip obvious false tokens
            if pn.upper() not in {"NUMBER", "PART", "NSN", "NONE", "UNKNOWN"} and len(pn) >= 3:
                facts.append(
                    {
                        "fact_type": "part_number",
                        "value": pn,
                        "intelligence_state": INTEL_VALIDATED,
                        **pn_ev,
                    }
                )

    cage = _clean(_field_value(struct, "cage") or row.get("cage"))
    if cage and struct.get("has_cage"):
        ev = _field_evidence(struct, "cage")
        if str(ev.get("confidence") or "HIGH").upper() in {"HIGH", "VALIDATED"}:
            facts.append(
                {
                    "fact_type": "cage",
                    "value": cage.upper(),
                    "intelligence_state": INTEL_VALIDATED,
                    **ev,
                }
            )

    oem = _clean(_field_value(struct, "oem") or row.get("oem") or row.get("manufacturer"))
    if oem and len(oem) >= 2:
        ev = _field_evidence(struct, "oem")
        # OEM from regex is MEDIUM — only promote with CAGE or exact approved-source identity
        if state == ID_EXACT_APPROVED_SOURCE_PART or cage:
            facts.append(
                {
                    "fact_type": "manufacturer",
                    "value": oem[:256],
                    "intelligence_state": INTEL_VALIDATED if cage else INTEL_POSSIBLE,
                    **ev,
                }
            )

    qty = _field_value(struct, "quantity")
    if qty is None:
        qty = row.get("quantity")
    if qty is not None and struct.get("has_quantity"):
        ev = _field_evidence(struct, "quantity")
        facts.append(
            {
                "fact_type": "quantity",
                "value": qty,
                "intelligence_state": INTEL_VALIDATED,
                **ev,
            }
        )

    uoi = _clean(_field_value(struct, "unit_of_issue") or row.get("unit_of_issue"))
    if uoi and struct.get("has_uoi"):
        ev = _field_evidence(struct, "unit_of_issue")
        facts.append(
            {
                "fact_type": "unit_of_issue",
                "value": uoi.upper(),
                "intelligence_state": INTEL_VALIDATED,
                **ev,
            }
        )

    return facts


def _dedupe_key_for_product(facts: list[dict[str, Any]]) -> str | None:
    by = {f["fact_type"]: f["value"] for f in facts}
    if by.get("nsn"):
        return f"nsn:{str(by['nsn']).upper()}"
    if by.get("part_number") and by.get("cage"):
        return f"pn_cage:{str(by['part_number']).upper()}|{str(by['cage']).upper()}"
    if by.get("part_number") and by.get("manufacturer"):
        return f"pn_mfr:{str(by['part_number']).upper()}|{str(by['manufacturer']).upper()[:40]}"
    if by.get("part_number"):
        return f"pn:{str(by['part_number']).upper()}"
    return None


class InMemoryProjectionBackend:
    """Test / offline backend — no SQL required."""

    def __init__(self) -> None:
        self.products: dict[int, dict[str, Any]] = {}
        self.requirements: list[dict[str, Any]] = []
        self._next_id = 1
        self.index: dict[str, Any] = {"by_dedupe_key": {}, "by_opportunity_uid": {}}

    def find_product_by_dedupe_key(self, key: str) -> dict[str, Any] | None:
        pid = self.index.get("by_dedupe_key", {}).get(key)
        if pid is None:
            return None
        return deepcopy(self.products.get(int(pid)))

    def upsert_knowledge_product(self, payload: dict[str, Any], *, dedupe_key: str) -> tuple[dict[str, Any], bool]:
        existing = self.find_product_by_dedupe_key(dedupe_key)
        if existing:
            specs = dict(existing.get("specifications_json") or {})
            specs.update(payload.get("specifications_json") or {})
            existing["specifications_json"] = specs
            for k in ("manufacturer", "part_number", "description", "source", "source_url", "verification_status"):
                if payload.get(k) not in (None, ""):
                    existing[k] = payload[k]
            existing["last_verified_at"] = payload.get("last_verified_at") or existing.get("last_verified_at")
            existing["updated_at"] = _utc()
            self.products[int(existing["id"])] = existing
            return deepcopy(existing), False
        pid = self._next_id
        self._next_id += 1
        row = {
            "id": pid,
            **payload,
            "created_at": _utc(),
            "updated_at": _utc(),
        }
        self.products[pid] = row
        self.index.setdefault("by_dedupe_key", {})[dedupe_key] = pid
        return deepcopy(row), True

    def add_requirement(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Dedup by contract_id+field_key+notice when present
        for r in self.requirements:
            if (
                r.get("contract_id") == payload.get("contract_id")
                and r.get("field_key") == payload.get("field_key")
                and r.get("notice_id") == payload.get("notice_id")
            ):
                r.update({k: v for k, v in payload.items() if v is not None})
                r["updated_at"] = _utc()
                return deepcopy(r)
        row = {**payload, "id": len(self.requirements) + 1, "created_at": _utc(), "updated_at": _utc()}
        self.requirements.append(row)
        return deepcopy(row)


class SqlProjectionBackend:
    """Postgres-backed projection using existing KnowledgeProduct / ProductRequirement."""

    def __init__(self, session: Any, index: dict[str, Any] | None = None) -> None:
        self.session = session
        self.index = index or {"by_dedupe_key": {}, "by_opportunity_uid": {}}

    def find_product_by_dedupe_key(self, key: str) -> dict[str, Any] | None:
        from models import KnowledgeProduct

        pid = self.index.get("by_dedupe_key", {}).get(key)
        if pid is not None:
            row = self.session.query(KnowledgeProduct).filter_by(id=int(pid)).one_or_none()
            if row:
                return _kp_to_dict(row)
        # Fallback scan by part_number / specs nsn
        if key.startswith("nsn:"):
            nsn = key.split(":", 1)[1]
            rows = self.session.query(KnowledgeProduct).all()
            for row in rows:
                specs = row.specifications_json or {}
                if str(specs.get("nsn") or "").upper() == nsn.upper():
                    self.index.setdefault("by_dedupe_key", {})[key] = row.id
                    return _kp_to_dict(row)
        if key.startswith("pn:"):
            pn = key.split(":", 1)[1]
            row = (
                self.session.query(KnowledgeProduct)
                .filter(KnowledgeProduct.part_number == pn)
                .order_by(KnowledgeProduct.id.asc())
                .first()
            )
            if row:
                self.index.setdefault("by_dedupe_key", {})[key] = row.id
                return _kp_to_dict(row)
        if key.startswith("pn_cage:") or key.startswith("pn_mfr:"):
            rest = key.split(":", 1)[1]
            pn, _, other = rest.partition("|")
            q = self.session.query(KnowledgeProduct).filter(KnowledgeProduct.part_number == pn)
            for row in q.all():
                specs = row.specifications_json or {}
                if key.startswith("pn_cage:") and str(specs.get("cage") or "").upper() == other.upper():
                    self.index.setdefault("by_dedupe_key", {})[key] = row.id
                    return _kp_to_dict(row)
                if key.startswith("pn_mfr:") and str(row.manufacturer or "").upper()[:40] == other.upper()[:40]:
                    self.index.setdefault("by_dedupe_key", {})[key] = row.id
                    return _kp_to_dict(row)
        return None

    def upsert_knowledge_product(self, payload: dict[str, Any], *, dedupe_key: str) -> tuple[dict[str, Any], bool]:
        from models import KnowledgeProduct

        existing = self.find_product_by_dedupe_key(dedupe_key)
        if existing:
            row = self.session.query(KnowledgeProduct).filter_by(id=int(existing["id"])).one()
            specs = dict(row.specifications_json or {})
            specs.update(payload.get("specifications_json") or {})
            row.specifications_json = specs
            for attr in ("manufacturer", "part_number", "description", "source", "source_url", "verification_status"):
                if payload.get(attr) not in (None, ""):
                    setattr(row, attr, payload[attr])
            if payload.get("last_verified_at"):
                row.last_verified_at = _parse_dt(payload["last_verified_at"])
            self.session.flush()
            self.index.setdefault("by_dedupe_key", {})[dedupe_key] = row.id
            return _kp_to_dict(row), False

        row = KnowledgeProduct(
            manufacturer=payload.get("manufacturer"),
            part_number=payload.get("part_number"),
            description=payload.get("description"),
            specifications_json=payload.get("specifications_json") or {},
            source=payload.get("source"),
            source_url=payload.get("source_url"),
            verification_status=payload.get("verification_status") or "VALIDATED",
            last_verified_at=_parse_dt(payload.get("last_verified_at")),
        )
        self.session.add(row)
        self.session.flush()
        self.index.setdefault("by_dedupe_key", {})[dedupe_key] = row.id
        return _kp_to_dict(row), True

    def add_requirement(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        from models import ProductRequirement

        if payload.get("contract_id") is None:
            return None  # FK required — skip without inventing Contract
        existing = (
            self.session.query(ProductRequirement)
            .filter_by(
                contract_id=int(payload["contract_id"]),
                field_key=str(payload["field_key"]),
            )
            .one_or_none()
        )
        if existing:
            existing.value_json = payload.get("value_json")
            existing.status = payload.get("status") or existing.status
            existing.evidence_text = payload.get("evidence_text") or existing.evidence_text
            existing.evidence_ref = payload.get("evidence_ref") or existing.evidence_ref
            existing.source_type = payload.get("source_type") or existing.source_type
            existing.notice_id = payload.get("notice_id") or existing.notice_id
            existing.solicitation_number = payload.get("solicitation_number") or existing.solicitation_number
            existing.extracted_at = _parse_dt(payload.get("extracted_at")) or existing.extracted_at
            self.session.flush()
            return _req_to_dict(existing)
        row = ProductRequirement(
            contract_id=int(payload["contract_id"]),
            notice_id=payload.get("notice_id"),
            solicitation_number=payload.get("solicitation_number"),
            field_key=str(payload["field_key"]),
            field_type=payload.get("field_type"),
            value_json=payload.get("value_json"),
            status=payload.get("status") or "VALIDATED",
            evidence_text=payload.get("evidence_text"),
            evidence_ref=payload.get("evidence_ref"),
            source_type=payload.get("source_type"),
            schema_version=BUILD_TAG,
            extracted_at=_parse_dt(payload.get("extracted_at")),
        )
        self.session.add(row)
        self.session.flush()
        return _req_to_dict(row)


def _parse_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return now_utc()


def _kp_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "manufacturer": row.manufacturer,
        "part_number": row.part_number,
        "description": row.description,
        "specifications_json": dict(row.specifications_json or {}),
        "source": row.source,
        "source_url": row.source_url,
        "verification_status": row.verification_status,
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
    }


def _req_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "contract_id": row.contract_id,
        "notice_id": row.notice_id,
        "field_key": row.field_key,
        "value_json": row.value_json,
        "status": row.status,
        "evidence_text": row.evidence_text,
        "evidence_ref": row.evidence_ref,
        "source_type": row.source_type,
    }


def load_projection_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PROJECTION_INDEX_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"kind": "M3ProductFactProjectionIndex", "by_dedupe_key": {}, "by_opportunity_uid": {}, "build": BUILD_TAG}


def save_projection_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PROJECTION_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=PROJECTION_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def project_opportunity_product_facts(
    row: dict[str, Any],
    *,
    backend: Any | None = None,
    identity_resolver: OpportunityIdentityResolver | None = None,
    force: bool = False,
    persist_index: bool = False,
) -> dict[str, Any]:
    """
    Project validated product facts from one pipeline/enriched opportunity.

    Idempotent. Does not invent identities. Leaves pipeline JSON intact.
    """
    eligibility = is_eligible_for_projection(row, force=force)
    out: dict[str, Any] = {
        "kind": "M3ProductFactProjection",
        "build": BUILD_TAG,
        "eligible": eligibility["eligible"],
        "eligibility_reason": eligibility["reason"],
        "projected": False,
        "skipped_reason": None,
        "opportunity_uid": None,
        "knowledge_product_id": None,
        "knowledge_product_created": False,
        "facts": [],
        "requirements": [],
        "intelligence_state": INTEL_UNKNOWN,
        "pipeline_json_preserved": True,
    }

    if not eligibility["eligible"]:
        out["skipped_reason"] = eligibility["reason"]
        return out

    facts = extract_promotable_facts(row)
    out["facts"] = facts
    if not facts:
        out["skipped_reason"] = "no_promotable_facts"
        out["intelligence_state"] = map_to_intelligence_state(
            (_identity(row).get("identity_state") or row.get("readiness_state") or INTEL_UNKNOWN)
        )
        return out

    # Identity bridge (BUILD 1)
    resolver = identity_resolver or OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    out["opportunity_uid"] = ident.get("opportunity_uid")

    dedupe_key = _dedupe_key_for_product(facts)
    if not dedupe_key:
        out["skipped_reason"] = "no_dedupe_key"
        return out

    backend = backend or InMemoryProjectionBackend()
    if hasattr(backend, "index") and persist_index:
        # merge durable index
        durable = load_projection_index()
        backend.index.setdefault("by_dedupe_key", {}).update(durable.get("by_dedupe_key") or {})
        backend.index.setdefault("by_opportunity_uid", {}).update(durable.get("by_opportunity_uid") or {})

    by_type = {f["fact_type"]: f for f in facts}
    now = _utc()
    specs: dict[str, Any] = {
        "dedupe_key": dedupe_key,
        "opportunity_uids": [ident.get("opportunity_uid")] if ident.get("opportunity_uid") else [],
        "projected_at": now,
        "projection_build": BUILD_TAG,
    }
    if by_type.get("nsn"):
        specs["nsn"] = by_type["nsn"]["value"]
        specs["nsn_evidence"] = {
            "source": by_type["nsn"].get("evidence_source"),
            "snippet": by_type["nsn"].get("evidence_snippet"),
            "confidence": by_type["nsn"].get("confidence"),
            "method": by_type["nsn"].get("extraction_method"),
            "timestamp": now,
        }
    if by_type.get("cage"):
        specs["cage"] = by_type["cage"]["value"]
        specs["cage_evidence"] = {
            "source": by_type["cage"].get("evidence_source"),
            "snippet": by_type["cage"].get("evidence_snippet"),
            "confidence": by_type["cage"].get("confidence"),
            "timestamp": now,
        }
    if by_type.get("quantity"):
        specs["quantity"] = by_type["quantity"]["value"]
    if by_type.get("unit_of_issue"):
        specs["unit_of_issue"] = by_type["unit_of_issue"]["value"]

    payload = {
        "manufacturer": (by_type.get("manufacturer") or {}).get("value"),
        "part_number": (by_type.get("part_number") or {}).get("value"),
        "description": (row.get("title") or "")[:500] or None,
        "specifications_json": specs,
        "source": row.get("source_id") or "pipeline_projection",
        "source_url": row.get("detail_url")
        or ((row.get("raw_metadata") or {}).get("description_recovery") or {}).get("source_url"),
        "verification_status": "VALIDATED",
        "last_verified_at": now,
    }

    product, created = backend.upsert_knowledge_product(payload, dedupe_key=dedupe_key)
    # Ensure opportunity uid recorded on product specs
    if ident.get("opportunity_uid"):
        specs_now = dict(product.get("specifications_json") or {})
        uids = list(specs_now.get("opportunity_uids") or [])
        if ident["opportunity_uid"] not in uids:
            uids.append(ident["opportunity_uid"])
            specs_now["opportunity_uids"] = uids
            payload["specifications_json"] = specs_now
            product, created = backend.upsert_knowledge_product(payload, dedupe_key=dedupe_key)
            # second call is always an update
            created = False if product.get("id") else created

    out["knowledge_product_id"] = product.get("id")
    out["knowledge_product_created"] = created
    out["projected"] = True
    out["intelligence_state"] = INTEL_VALIDATED
    if row.get("readiness_state") in {READY_COMMERCIAL_RESEARCH, "COMMERCIAL_RESEARCH_READY"}:
        out["intelligence_state"] = INTEL_AVAILABLE

    # ProductRequirement rows only when Contract exists
    contract_id = row.get("contract_id") or ident.get("contract_id")
    req_keys = ["nsn", "part_number", "cage", "manufacturer", "quantity", "unit_of_issue"]
    for f in facts:
        if f["fact_type"] not in req_keys:
            continue
        req_payload = {
            "contract_id": contract_id,
            "notice_id": row.get("notice_id") or ident.get("notice_id"),
            "solicitation_number": row.get("solicitation_number"),
            "field_key": f["fact_type"],
            "field_type": f["fact_type"],
            "value_json": f["value"],
            "status": "VALIDATED",
            "evidence_text": f.get("evidence_snippet"),
            "evidence_ref": f.get("evidence_source"),
            "source_type": "pipeline_projection",
            "extracted_at": now,
        }
        if contract_id is not None:
            saved = backend.add_requirement(req_payload)
            if saved:
                out["requirements"].append(saved)
        else:
            out["requirements"].append(
                {
                    **req_payload,
                    "persisted": False,
                    "note": "ProductRequirement requires contract_id — fact kept on KnowledgeProduct.specifications_json",
                }
            )

    if hasattr(backend, "index"):
        if ident.get("opportunity_uid"):
            backend.index.setdefault("by_opportunity_uid", {})[ident["opportunity_uid"]] = {
                "knowledge_product_id": product.get("id"),
                "dedupe_key": dedupe_key,
                "projected_at": now,
            }
        if persist_index:
            save_projection_index(backend.index)

    # Annotate pipeline row copy hint (caller may persist) — never strip existing JSON
    out["pipeline_annotation"] = {
        "knowledge_product_id": product.get("id"),
        "product_fact_projection": {
            "build": BUILD_TAG,
            "projected_at": now,
            "dedupe_key": dedupe_key,
            "opportunity_uid": ident.get("opportunity_uid"),
            "intelligence_state": out["intelligence_state"],
        },
    }
    return out


def project_research_ready_batch(
    rows: list[dict[str, Any]],
    *,
    backend: Any | None = None,
    limit: int = 50,
    force: bool = False,
) -> dict[str, Any]:
    """Bounded batch — newly enriched / research-ready only (unless force)."""
    backend = backend or InMemoryProjectionBackend()
    resolver = OpportunityIdentityResolver()
    results = []
    scanned = 0
    projected = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned += 1
        if len(results) >= limit:
            break
        elig = is_eligible_for_projection(row, force=force)
        if not elig["eligible"]:
            continue
        res = project_opportunity_product_facts(
            row, backend=backend, identity_resolver=resolver, force=force
        )
        results.append(res)
        if res.get("projected"):
            projected += 1
    return {
        "kind": "M3ProductFactProjectionBatch",
        "build": BUILD_TAG,
        "scanned": scanned,
        "attempted": len(results),
        "projected": projected,
        "results": results,
    }
