"""BUILD 5 — Award/product fact projection (back-path foundation).

Projects validated historical award facts onto reusable KnowledgeProduct /
KnowledgeSupplier relationships. Does not call USAspending, replace historical
engines, change discovery, or invent product identities.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from m3_product_fact_projection import (
    INTEL_UNKNOWN,
    INTEL_VALIDATED,
    InMemoryProjectionBackend,
    extract_promotable_facts,
    map_to_intelligence_state,
)

BUILD_TAG = "20260918-m3-award-product-projection-1"
AWARD_PROJECTION_INDEX_KEY = "m3_award_product_projection_index_v1"

_NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
_CAGE_RE = re.compile(r"\b([A-Z0-9]{5})\b")


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Award normalization (read existing shapes only)
# ---------------------------------------------------------------------------

def collect_award_records(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather award-like dicts already on the pipeline row — no live fetch."""
    out: list[dict[str, Any]] = []
    for key in ("historical_awards", "award_history", "usaspending_awards", "government_awards"):
        blob = row.get(key)
        if isinstance(blob, list):
            out.extend(a for a in blob if isinstance(a, dict))
    # Single scalar historical award → synthetic record only if amount present
    amt = _num(row.get("historical_award_amount"))
    if amt is not None and not out:
        out.append(
            {
                "award_id": row.get("historical_award_id") or f"row-hist:{row.get('canonical_id')}",
                "award_amount": amt,
                "unit_price": row.get("historical_unit_price"),
                "agency": row.get("agency") or row.get("buyer"),
                "award_date": row.get("historical_award_date") or row.get("award_date"),
                "awardee": row.get("historical_awardee") or row.get("incumbent"),
                "source": "pipeline_row.historical_award_amount",
                "confidence": "HIGH" if row.get("historical_award_amount") else "LOW",
            }
        )
    # Watchlist-style meta on row
    wl = row.get("govspend_watchlist") or row.get("watchlist_match")
    if isinstance(wl, dict) and (wl.get("award_id") or wl.get("award_amount")):
        out.append(
            {
                "award_id": wl.get("award_id") or wl.get("matched_award_id"),
                "award_amount": wl.get("award_amount") or wl.get("estimated_annual_value"),
                "agency": wl.get("agency") or wl.get("contracting_office"),
                "awardee": wl.get("incumbent_name") or wl.get("incumbent"),
                "award_date": wl.get("award_date"),
                "source": "govspend_watchlist",
                "confidence": "HIGH" if wl.get("award_id") else "MEDIUM",
                "naics": wl.get("naics_code"),
                "description": wl.get("contract_name"),
            }
        )
    return out


def normalize_award(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize USAspending / historical_awards / watchlist shapes."""
    award_id = (
        _clean(raw.get("award_id"))
        or _clean(raw.get("Award ID"))
        or _clean(raw.get("piid"))
        or _clean(raw.get("id"))
    )
    amount = _num(
        raw.get("award_amount")
        or raw.get("Award Amount")
        or raw.get("amount")
        or raw.get("value")
        or raw.get("Total_value")
    )
    agency = (
        _clean(raw.get("agency"))
        or _clean(raw.get("Awarding Agency"))
        or _clean(raw.get("buyer"))
        or _clean(raw.get("contracting_office"))
    )
    buyer = _clean(raw.get("buyer")) or _clean(raw.get("contracting_office")) or agency
    awardee = (
        _clean(raw.get("awardee"))
        or _clean(raw.get("winner"))
        or _clean(raw.get("vendor"))
        or _clean(raw.get("Recipient Name"))
        or _clean(raw.get("incumbent_name"))
        or _clean(raw.get("incumbent"))
    )
    date = (
        _clean(raw.get("award_date"))
        or _clean(raw.get("Start Date"))
        or _clean(raw.get("date"))
        or _clean(raw.get("Evidence_date"))
    )
    qty = raw.get("quantity") if raw.get("quantity") not in (None, "", "UNKNOWN") else None
    unit_price = _num(raw.get("unit_price") or raw.get("Unit_price"))
    source = (
        _clean(raw.get("source"))
        or _clean(raw.get("Source"))
        or _clean(raw.get("Source_URL"))
        or ("usaspending:" + award_id if award_id else "historical_award")
    )
    conf = str(raw.get("confidence") or raw.get("Confidence") or "HIGH").upper()
    return {
        "award_id": award_id,
        "award_amount": amount,
        "agency": agency,
        "buyer": buyer,
        "awardee": awardee,
        "award_date": date,
        "quantity": qty,
        "unit_price": unit_price,
        "source": source,
        "confidence": conf if conf in {"HIGH", "VALIDATED", "MEDIUM", "LOW"} else "MEDIUM",
        "description": _clean(raw.get("description") or raw.get("Description") or raw.get("Product_requirement")),
        "raw_keys": sorted(raw.keys()),
    }


# ---------------------------------------------------------------------------
# Product identity extraction — exact only
# ---------------------------------------------------------------------------

def extract_award_product_identity(
    award: dict[str, Any],
    *,
    opportunity_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Exact identity only.

    PROMOTE: explicit NSN / PN+CAGE / validated OEM+PN on the award, OR
    validated opportunity product identity when projecting awards for that opp.

    DO NOT PROMOTE: description-only guesses, category assumptions, weak AI.
    """
    out: dict[str, Any] = {
        "promotable": False,
        "reason": "no_exact_identity",
        "nsn": None,
        "part_number": None,
        "cage": None,
        "manufacturer": None,
        "confidence": "UNKNOWN",
        "evidence_source": None,
        "evidence_snippet": None,
        "dedupe_key": None,
    }

    # Explicit fields on award
    nsn = _clean(award.get("nsn") or award.get("NSN") or award.get("niin"))
    pn = _clean(award.get("part_number") or award.get("Part Number") or award.get("pn"))
    cage = _clean(award.get("cage") or award.get("CAGE"))
    mfr = _clean(award.get("manufacturer") or award.get("oem") or award.get("OEM"))
    conf = str(award.get("identity_confidence") or award.get("confidence") or "").upper()

    # Reject NSN scraped from free-text description unless explicitly flagged
    desc = (
        award.get("description")
        or award.get("Description")
        or award.get("Product_requirement")
        or ""
    )
    if not nsn and award.get("allow_nsn_from_description") is True:
        m = _NSN_RE.search(str(desc))
        if m and conf in {"HIGH", "VALIDATED"}:
            nsn = m.group(1)
            out["evidence_snippet"] = m.group(0)

    if nsn and conf in {"", "HIGH", "VALIDATED", "MEDIUM"}:
        # Explicit nsn field always OK; description scrape only with allow flag (handled above)
        if award.get("nsn") or award.get("NSN") or award.get("niin") or award.get("allow_nsn_from_description"):
            if conf in {"", "MEDIUM"}:
                conf = "HIGH"
            out.update(
                {
                    "promotable": True,
                    "reason": "exact_nsn",
                    "nsn": nsn,
                    "part_number": pn,
                    "cage": cage.upper() if cage else None,
                    "manufacturer": mfr,
                    "confidence": "HIGH" if conf in {"", "HIGH", "VALIDATED", "MEDIUM"} else conf,
                    "evidence_source": award.get("source") or "award.nsn",
                    "evidence_snippet": out.get("evidence_snippet") or f"NSN {nsn}",
                    "dedupe_key": f"nsn:{nsn.upper()}",
                }
            )
            return out

    if pn and cage and conf in {"", "HIGH", "VALIDATED"}:
        out.update(
            {
                "promotable": True,
                "reason": "exact_pn_cage",
                "nsn": None,
                "part_number": pn,
                "cage": cage.upper(),
                "manufacturer": mfr,
                "confidence": "HIGH",
                "evidence_source": award.get("source") or "award.pn_cage",
                "evidence_snippet": f"P/N {pn} CAGE {cage}",
                "dedupe_key": f"pn_cage:{pn.upper()}|{cage.upper()}",
            }
        )
        return out

    if pn and mfr and conf in {"HIGH", "VALIDATED"}:
        out.update(
            {
                "promotable": True,
                "reason": "exact_pn_oem",
                "part_number": pn,
                "manufacturer": mfr,
                "cage": cage.upper() if cage else None,
                "confidence": "HIGH",
                "evidence_source": award.get("source") or "award.pn_oem",
                "evidence_snippet": f"P/N {pn} OEM {mfr}",
                "dedupe_key": f"pn_mfr:{pn.upper()}|{mfr.upper()[:40]}",
            }
        )
        return out

    # Opportunity-linked: use already-validated product facts from BUILD 2 extract
    if opportunity_row:
        facts = extract_promotable_facts(opportunity_row)
        by = {f["fact_type"]: f for f in facts}
        if by.get("nsn"):
            nsn_v = str(by["nsn"]["value"])
            out.update(
                {
                    "promotable": True,
                    "reason": "opportunity_validated_nsn",
                    "nsn": nsn_v,
                    "part_number": (by.get("part_number") or {}).get("value"),
                    "cage": (by.get("cage") or {}).get("value"),
                    "manufacturer": (by.get("manufacturer") or {}).get("value"),
                    "confidence": "HIGH",
                    "evidence_source": "opportunity.product_identity+award",
                    "evidence_snippet": by["nsn"].get("evidence_snippet") or f"NSN {nsn_v}",
                    "dedupe_key": f"nsn:{nsn_v.upper()}",
                }
            )
            return out
        if by.get("part_number") and (by.get("cage") or by.get("manufacturer")):
            pn_v = str(by["part_number"]["value"])
            cage_v = (by.get("cage") or {}).get("value")
            mfr_v = (by.get("manufacturer") or {}).get("value")
            key = (
                f"pn_cage:{pn_v.upper()}|{str(cage_v).upper()}"
                if cage_v
                else f"pn_mfr:{pn_v.upper()}|{str(mfr_v).upper()[:40]}"
            )
            out.update(
                {
                    "promotable": True,
                    "reason": "opportunity_validated_pn",
                    "part_number": pn_v,
                    "cage": cage_v,
                    "manufacturer": mfr_v,
                    "confidence": "HIGH",
                    "evidence_source": "opportunity.product_identity+award",
                    "evidence_snippet": by["part_number"].get("evidence_snippet"),
                    "dedupe_key": key,
                }
            )
            return out

    # Description-only → explicitly not promotable
    if desc and not nsn and not pn:
        out["reason"] = "description_only_not_promoted"
        out["confidence"] = "UNKNOWN"
    return out


def demand_evidence_record(
    *,
    product_id: Any,
    award: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "ProductDemandEvidence",
        "product_id": product_id,
        "agency": award.get("agency") or "UNKNOWN",
        "buyer": award.get("buyer") or award.get("agency") or "UNKNOWN",
        "award_source": award.get("source") or "UNKNOWN",
        "award_id": award.get("award_id"),
        "quantity": award.get("quantity") if award.get("quantity") is not None else "UNKNOWN",
        "date": award.get("award_date") or "UNKNOWN",
        "value": award.get("award_amount") if award.get("award_amount") is not None else "UNKNOWN",
        "unit_price": award.get("unit_price") if award.get("unit_price") is not None else "UNKNOWN",
        "evidence": {
            "source": award.get("source"),
            "snippet": identity.get("evidence_snippet"),
            "identity_reason": identity.get("reason"),
            "nsn": identity.get("nsn"),
            "part_number": identity.get("part_number"),
        },
        "confidence": identity.get("confidence") or award.get("confidence") or "HIGH",
        "projected_at": _utc(),
        "build": BUILD_TAG,
    }


def supplier_relationship_record(
    *,
    product_id: Any,
    supplier_id: Any,
    supplier_name: str,
    award: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "HistoricalSupplierRelationship",
        "product_id": product_id,
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "relationship": "HISTORICAL_AWARDEE",
        "evidence": {
            "award_id": award.get("award_id"),
            "source": award.get("source"),
            "award_date": award.get("award_date"),
            "award_amount": award.get("award_amount"),
        },
        "confidence": "HIGH" if award.get("award_id") else "MEDIUM",
        "projected_at": _utc(),
        "build": BUILD_TAG,
    }


def award_dedupe_key(award: dict[str, Any], product_dedupe: str) -> str:
    aid = award.get("award_id") or f"amt:{award.get('award_amount')}|{award.get('award_date')}|{award.get('agency')}"
    return f"{product_dedupe}|award:{aid}"


# ---------------------------------------------------------------------------
# Backends — extend product projection with supplier + demand index
# ---------------------------------------------------------------------------

class InMemoryAwardBackend(InMemoryProjectionBackend):
    def __init__(self) -> None:
        super().__init__()
        self.suppliers: dict[int, dict[str, Any]] = {}
        self.offers: list[dict[str, Any]] = []
        self.award_keys: set[str] = set()
        self.index.setdefault("by_award_key", {})

    def upsert_supplier(self, name: str, *, source: str | None = None) -> tuple[dict[str, Any], bool]:
        for s in self.suppliers.values():
            if str(s.get("name") or "").strip().lower() == name.strip().lower():
                return deepcopy(s), False
        sid = len(self.suppliers) + 1
        row = {
            "id": sid,
            "name": name[:512],
            "source": source or "historical_award",
            "verification_status": "VALIDATED",
            "created_at": _utc(),
        }
        self.suppliers[sid] = row
        return deepcopy(row), True

    def add_historical_offer(
        self,
        *,
        product_id: int,
        supplier_id: int,
        award: dict[str, Any],
    ) -> dict[str, Any]:
        ref = str(award.get("award_id") or "")
        for o in self.offers:
            if o.get("product_id") == product_id and o.get("supplier_id") == supplier_id and o.get("source_reference") == ref:
                return deepcopy(o)
        row = {
            "id": len(self.offers) + 1,
            "product_id": product_id,
            "supplier_id": supplier_id,
            "extended_price": award.get("award_amount"),
            "unit_price": award.get("unit_price"),
            "source_type": "HISTORICAL_PRICE",
            "temporal_class": "HISTORICAL",
            "verification_status": "VALIDATED",
            "source_reference": ref or None,
            "source_url": award.get("source"),
            "notes": json.dumps({"agency": award.get("agency"), "date": award.get("award_date")}),
            "created_at": _utc(),
        }
        self.offers.append(row)
        return deepcopy(row)

    def has_award_key(self, key: str) -> bool:
        return key in self.award_keys or key in (self.index.get("by_award_key") or {})

    def mark_award_key(self, key: str, product_id: Any) -> None:
        self.award_keys.add(key)
        self.index.setdefault("by_award_key", {})[key] = product_id


class SqlAwardBackend:
    """Uses KnowledgeProduct / KnowledgeSupplier / SupplierOffer — no new tables."""

    def __init__(self, session: Any, index: dict[str, Any] | None = None) -> None:
        from m3_product_fact_projection import SqlProjectionBackend

        self._products = SqlProjectionBackend(session, index=index)
        self.session = session
        self.index = self._products.index
        self.index.setdefault("by_award_key", {})

    def find_product_by_dedupe_key(self, key: str) -> dict[str, Any] | None:
        return self._products.find_product_by_dedupe_key(key)

    def upsert_knowledge_product(self, payload: dict[str, Any], *, dedupe_key: str) -> tuple[dict[str, Any], bool]:
        return self._products.upsert_knowledge_product(payload, dedupe_key=dedupe_key)

    def upsert_supplier(self, name: str, *, source: str | None = None) -> tuple[dict[str, Any], bool]:
        from models import KnowledgeSupplier

        existing = (
            self.session.query(KnowledgeSupplier)
            .filter(KnowledgeSupplier.name.ilike(name[:512]))
            .order_by(KnowledgeSupplier.id.asc())
            .first()
        )
        if existing:
            return {
                "id": existing.id,
                "name": existing.name,
                "verification_status": existing.verification_status,
            }, False
        row = KnowledgeSupplier(
            name=name[:512],
            source=source or "historical_award",
            verification_status="VALIDATED",
            last_verified_at=now_utc(),
            notes="Projected from historical award awardee — BUILD 5",
        )
        self.session.add(row)
        self.session.flush()
        return {"id": row.id, "name": row.name, "verification_status": row.verification_status}, True

    def add_historical_offer(
        self,
        *,
        product_id: int,
        supplier_id: int,
        award: dict[str, Any],
    ) -> dict[str, Any]:
        from decimal import Decimal

        from models import SupplierOffer

        ref = str(award.get("award_id") or "")[:512]
        existing = (
            self.session.query(SupplierOffer)
            .filter_by(product_id=int(product_id), supplier_id=int(supplier_id), source_reference=ref or None)
            .first()
        )
        if existing and ref:
            return {"id": existing.id, "product_id": product_id, "supplier_id": supplier_id}
        # Also match empty ref by amount+date notes soft-dedupe skipped — create
        amt = award.get("award_amount")
        up = award.get("unit_price")
        row = SupplierOffer(
            product_id=int(product_id),
            supplier_id=int(supplier_id),
            extended_price=Decimal(str(amt)) if amt is not None else None,
            unit_price=Decimal(str(up)) if up is not None else None,
            source_type="HISTORICAL_PRICE",
            temporal_class="HISTORICAL",
            verification_status="VALIDATED",
            source_reference=ref or None,
            source_url=(award.get("source") or "")[:1024] or None,
            notes=json.dumps({"agency": award.get("agency"), "date": award.get("award_date"), "build": BUILD_TAG}),
            last_verified_at=now_utc(),
        )
        self.session.add(row)
        self.session.flush()
        return {"id": row.id, "product_id": product_id, "supplier_id": supplier_id}

    def has_award_key(self, key: str) -> bool:
        return key in (self.index.get("by_award_key") or {})

    def mark_award_key(self, key: str, product_id: Any) -> None:
        self.index.setdefault("by_award_key", {})[key] = product_id


def load_award_projection_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == AWARD_PROJECTION_INDEX_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_award_key", {})
                    data.setdefault("by_dedupe_key", {})
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"kind": "M3AwardProductProjectionIndex", "by_award_key": {}, "by_dedupe_key": {}, "build": BUILD_TAG}


def save_award_projection_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == AWARD_PROJECTION_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=AWARD_PROJECTION_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def _merge_demand_into_specs(specs: dict[str, Any], demand: dict[str, Any]) -> dict[str, Any]:
    specs = dict(specs or {})
    demands = list(specs.get("demand_evidence") or [])
    key = f"{demand.get('award_id')}|{demand.get('agency')}|{demand.get('value')}|{demand.get('date')}"
    existing_keys = {
        f"{d.get('award_id')}|{d.get('agency')}|{d.get('value')}|{d.get('date')}"
        for d in demands
        if isinstance(d, dict)
    }
    if key not in existing_keys:
        demands.append(demand)
    specs["demand_evidence"] = demands[-50:]  # bound growth
    # Agency rollup
    agencies = list(specs.get("buying_agencies") or [])
    ag = demand.get("agency")
    if ag and ag != "UNKNOWN" and ag not in agencies:
        agencies.append(ag)
    specs["buying_agencies"] = agencies[:40]
    specs["award_projection_build"] = BUILD_TAG
    specs["last_award_projected_at"] = _utc()
    return specs


def _merge_supplier_into_specs(specs: dict[str, Any], rel: dict[str, Any]) -> dict[str, Any]:
    specs = dict(specs or {})
    rels = list(specs.get("historical_supplier_relationships") or [])
    key = f"{rel.get('supplier_name')}|{rel.get('evidence', {}).get('award_id')}"
    existing = {
        f"{r.get('supplier_name')}|{(r.get('evidence') or {}).get('award_id')}"
        for r in rels
        if isinstance(r, dict)
    }
    if key not in existing:
        rels.append(rel)
    specs["historical_supplier_relationships"] = rels[-40:]
    return specs


def project_award_product_facts(
    row: dict[str, Any] | None = None,
    *,
    awards: list[dict[str, Any]] | None = None,
    backend: Any | None = None,
    force: bool = False,
    merge_durable_index: bool = False,
    persist_index: bool = False,
) -> dict[str, Any]:
    """
    Project validated award→product→agency/supplier facts.

    Idempotent. Description-only awards are skipped. No live USAspending calls.
    """
    row = row or {}
    backend = backend or InMemoryAwardBackend()
    if merge_durable_index and hasattr(backend, "index"):
        durable = load_award_projection_index()
        backend.index.setdefault("by_award_key", {}).update(durable.get("by_award_key") or {})
        backend.index.setdefault("by_dedupe_key", {}).update(durable.get("by_dedupe_key") or {})

    raw_awards = awards if awards is not None else collect_award_records(row)
    out: dict[str, Any] = {
        "kind": "M3AwardProductProjection",
        "build": BUILD_TAG,
        "projected": False,
        "opportunity_id": row.get("canonical_id"),
        "awards_seen": len(raw_awards),
        "awards_projected": 0,
        "awards_skipped": 0,
        "skipped": [],
        "demand_evidence": [],
        "supplier_relationships": [],
        "knowledge_product_ids": [],
        "intelligence_state": INTEL_UNKNOWN,
        "pipeline_json_preserved": True,
        "pipeline_annotation": None,
    }

    if not raw_awards and not force:
        out["skipped"].append({"reason": "no_awards_on_row"})
        return out

    for raw in raw_awards:
        award = normalize_award(raw)
        # Require some award substance
        if award.get("award_amount") is None and not award.get("award_id") and award.get("unit_price") is None:
            out["awards_skipped"] += 1
            out["skipped"].append({"reason": "empty_award", "award": award})
            continue

        identity = extract_award_product_identity(raw if isinstance(raw, dict) else award, opportunity_row=row or None)
        # Re-normalize identity fields onto award for evidence helpers
        if identity.get("promotable"):
            # Prefer normalized award for amounts; merge identity fields
            pass
        else:
            out["awards_skipped"] += 1
            out["skipped"].append(
                {
                    "reason": identity.get("reason") or "not_promotable",
                    "award_id": award.get("award_id"),
                    "description_only": identity.get("reason")
                    in {"description_only_not_promoted", "no_exact_identity"}
                    and bool(
                        (raw.get("description") if isinstance(raw, dict) else None)
                        or (raw.get("Description") if isinstance(raw, dict) else None)
                        or award.get("description")
                    ),
                }
            )
            continue

        dedupe = identity["dedupe_key"]
        assert dedupe
        akey = award_dedupe_key(award, dedupe)
        if backend.has_award_key(akey) and not force:
            out["awards_skipped"] += 1
            out["skipped"].append({"reason": "duplicate_award_product", "award_key": akey})
            # Still surface existing product id if known
            pid = (backend.index.get("by_award_key") or {}).get(akey)
            if pid and pid not in out["knowledge_product_ids"]:
                out["knowledge_product_ids"].append(pid)
            continue

        specs: dict[str, Any] = {
            "dedupe_key": dedupe,
            "nsn": identity.get("nsn"),
            "cage": identity.get("cage"),
            "projection_build": BUILD_TAG,
            "projection_source": "award_product",
        }
        payload = {
            "manufacturer": identity.get("manufacturer"),
            "part_number": identity.get("part_number"),
            "description": (award.get("description") or row.get("title") or "")[:500] or None,
            "specifications_json": specs,
            "source": award.get("source") or "historical_award",
            "source_url": award.get("source") if str(award.get("source") or "").startswith("http") else None,
            "verification_status": "VALIDATED",
            "last_verified_at": _utc(),
        }
        product, created = backend.upsert_knowledge_product(payload, dedupe_key=dedupe)
        pid = product.get("id")

        demand = demand_evidence_record(product_id=pid, award=award, identity=identity)
        specs_now = dict(product.get("specifications_json") or specs)
        specs_now = _merge_demand_into_specs(specs_now, demand)
        rel = None
        supplier_created = False
        if award.get("awardee"):
            supplier, supplier_created = backend.upsert_supplier(
                str(award["awardee"]), source=award.get("source")
            )
            rel = supplier_relationship_record(
                product_id=pid,
                supplier_id=supplier.get("id"),
                supplier_name=str(award["awardee"]),
                award=award,
            )
            specs_now = _merge_supplier_into_specs(specs_now, rel)
            backend.add_historical_offer(
                product_id=int(pid),
                supplier_id=int(supplier["id"]),
                award=award,
            )
            out["supplier_relationships"].append(rel)

        # Persist merged specs
        payload["specifications_json"] = specs_now
        product, _ = backend.upsert_knowledge_product(payload, dedupe_key=dedupe)
        backend.mark_award_key(akey, pid)

        out["demand_evidence"].append(demand)
        out["awards_projected"] += 1
        out["projected"] = True
        if pid not in out["knowledge_product_ids"]:
            out["knowledge_product_ids"].append(pid)
        out.setdefault("products", []).append(
            {"id": pid, "created": created, "dedupe_key": dedupe, "supplier_created": supplier_created}
        )

    if out["projected"]:
        out["intelligence_state"] = INTEL_VALIDATED
        out["pipeline_annotation"] = {
            "award_product_projection": {
                "build": BUILD_TAG,
                "projected_at": _utc(),
                "knowledge_product_ids": out["knowledge_product_ids"],
                "demand_count": len(out["demand_evidence"]),
                "supplier_link_count": len(out["supplier_relationships"]),
                "demand_evidence": out["demand_evidence"][:10],
                "supplier_relationships": out["supplier_relationships"][:10],
                "buying_agencies": sorted(
                    {
                        d.get("agency")
                        for d in out["demand_evidence"]
                        if d.get("agency") and d.get("agency") != "UNKNOWN"
                    }
                ),
            },
            "knowledge_product_id": out["knowledge_product_ids"][0] if out["knowledge_product_ids"] else None,
        }

    # Persist index only when caller asks (API / SQL path)
    if persist_index and hasattr(backend, "index") and out["projected"]:
        try:
            save_award_projection_index(backend.index)
        except Exception:
            pass

    return out


def project_opportunity_awards(
    row: dict[str, Any],
    *,
    backend: Any | None = None,
    force: bool = False,
    merge_durable_index: bool = True,
    persist_index: bool = True,
) -> dict[str, Any]:
    """Convenience: project awards found on one opportunity row."""
    return project_award_product_facts(
        row,
        backend=backend,
        force=force,
        merge_durable_index=merge_durable_index,
        persist_index=persist_index,
    )
