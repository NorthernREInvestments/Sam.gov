"""BUILD 8 — Supplier–product graph hardening.

Durable Product→Supplier relationships with evidence, using existing
KnowledgeSupplier / SupplierOffer / KnowledgeProduct. Does not replace
supplier intelligence engines, create a new supplier DB, or trigger outreach.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from decimal import Decimal
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-supplier-product-graph-1"
GRAPH_INDEX_KEY = "m3_supplier_product_graph_index_v1"

# Relationship types
MANUFACTURER = "MANUFACTURER"
AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
DISTRIBUTOR = "DISTRIBUTOR"
RESELLER = "RESELLER"
HISTORICAL_GOVERNMENT_SUPPLIER = "HISTORICAL_GOVERNMENT_SUPPLIER"
SURPLUS_SOURCE = "SURPLUS_SOURCE"
UNKNOWN = "UNKNOWN"

RELATIONSHIP_TYPES = (
    MANUFACTURER,
    AUTHORIZED_DISTRIBUTOR,
    DISTRIBUTOR,
    RESELLER,
    HISTORICAL_GOVERNMENT_SUPPLIER,
    SURPLUS_SOURCE,
    UNKNOWN,
)

# Edge confidence / status
EDGE_VALIDATED = "VALIDATED"
EDGE_POSSIBLE = "POSSIBLE"
EDGE_UNKNOWN = "UNKNOWN"

_ROLE_MAP = {
    "OEM": MANUFACTURER,
    "MANUFACTURER": MANUFACTURER,
    "AUTHORIZED_DISTRIBUTOR": AUTHORIZED_DISTRIBUTOR,
    "AUTHORIZED": AUTHORIZED_DISTRIBUTOR,
    "DISTRIBUTOR": DISTRIBUTOR,
    "RESELLER": RESELLER,
    "SURPLUS": SURPLUS_SOURCE,
    "SURPLUS_SOURCE": SURPLUS_SOURCE,
    "HISTORICAL_AWARDEE": HISTORICAL_GOVERNMENT_SUPPLIER,
    "HISTORICAL_GOVERNMENT_SUPPLIER": HISTORICAL_GOVERNMENT_SUPPLIER,
    "AWARDEE": HISTORICAL_GOVERNMENT_SUPPLIER,
    "WINNER": HISTORICAL_GOVERNMENT_SUPPLIER,
}


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_name(name: str) -> str:
    s = re.sub(r"\s+", " ", name.strip().lower())
    s = re.sub(r"[^\w\s&.-]", "", s)
    return s[:200]


def _map_relationship(raw: Any) -> str:
    if raw is None or raw == "":
        return UNKNOWN
    key = str(raw).strip().upper().replace(" ", "_")
    return _ROLE_MAP.get(key, UNKNOWN if key not in RELATIONSHIP_TYPES else key)


def edge_key(*, product_key: str, supplier_key: str, relationship: str) -> str:
    return f"{product_key}|{supplier_key}|{relationship}"


def empty_edge(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "M3SupplierProductEdge",
        "build": BUILD_TAG,
        "edge_id": None,
        "product_id": None,
        "product_dedupe_key": None,
        "product_nsn": None,
        "supplier_id": None,
        "supplier_name": None,
        "relationship_type": UNKNOWN,
        "source": None,
        "evidence": [],
        "confidence": EDGE_UNKNOWN,
        "pricing_evidence": None,
        "availability": UNKNOWN,
        "timestamp": _utc(),
        "created_at": _utc(),
        "updated_at": _utc(),
        "outreach_triggered": False,
    }
    base.update(overrides)
    if base.get("edge_id") is None and base.get("product_dedupe_key") and base.get("supplier_name"):
        base["edge_id"] = edge_key(
            product_key=str(base["product_dedupe_key"]),
            supplier_key=_norm_name(str(base["supplier_name"])),
            relationship=str(base["relationship_type"]),
        )
    return base


def _evidence(source: Any, snippet: Any = None, confidence: Any = None, field: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "source": source or "UNKNOWN",
        "snippet": snippet,
        "confidence": confidence or EDGE_UNKNOWN,
    }


def _product_keys_from_row(row: dict[str, Any]) -> dict[str, Any]:
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}
    nsn = struct.get("nsn")
    if isinstance(fields.get("nsn"), dict):
        nsn = nsn or fields["nsn"].get("value")
    pn = struct.get("part_number")
    if isinstance(fields.get("part_number"), dict):
        pn = pn or fields["part_number"].get("value")
    proj = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    fact = row.get("product_fact_projection") if isinstance(row.get("product_fact_projection"), dict) else {}
    pid = (
        row.get("knowledge_product_id")
        or proj.get("knowledge_product_id")
        or (proj.get("knowledge_product_ids") or [None])[0]
        or fact.get("knowledge_product_id")
    )
    dedupe = f"nsn:{str(nsn).upper()}" if nsn else (f"pn:{str(pn).upper()}" if pn else None)
    return {
        "product_id": pid,
        "product_dedupe_key": dedupe,
        "nsn": nsn,
        "part_number": pn,
        "title": row.get("title"),
    }


def _is_promotable_confidence(conf: Any) -> bool:
    return str(conf or "").upper() in {"HIGH", "VALIDATED", "VERIFIED", "LEVEL_1", "LEVEL_2"}


# ---------------------------------------------------------------------------
# Candidate extraction (promote only strong evidence)
# ---------------------------------------------------------------------------

def candidates_from_historical_awardees(row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    product = _product_keys_from_row(row)
    if not product.get("product_dedupe_key") and not product.get("product_id"):
        return []  # no exact product — do not invent

    proj = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    rels = list(proj.get("supplier_relationships") or [])
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        name = _clean(rel.get("supplier_name"))
        if not name:
            continue
        conf = str(rel.get("confidence") or "HIGH").upper()
        if conf not in {"HIGH", "VALIDATED", "MEDIUM"}:
            continue
        # MEDIUM historical only if award_id present
        ev = rel.get("evidence") if isinstance(rel.get("evidence"), dict) else {}
        if conf == "MEDIUM" and not ev.get("award_id"):
            continue
        out.append(
            {
                "supplier_name": name,
                "supplier_id": rel.get("supplier_id"),
                "relationship_type": HISTORICAL_GOVERNMENT_SUPPLIER,
                "confidence": EDGE_VALIDATED if conf in {"HIGH", "VALIDATED"} else EDGE_POSSIBLE,
                "source": "award_product_projection",
                "evidence": [
                    _evidence(
                        "historical_awardee",
                        ev.get("award_id") or name,
                        conf,
                        field="awardee",
                    )
                ],
                "pricing_evidence": {
                    "award_amount": ev.get("award_amount"),
                    "award_date": ev.get("award_date"),
                    "temporal_class": "HISTORICAL",
                },
                "product": product,
            }
        )

    # Also direct historical_awards with exact product
    for a in row.get("historical_awards") or row.get("award_history") or []:
        if not isinstance(a, dict):
            continue
        name = _clean(a.get("awardee") or a.get("winner") or a.get("vendor") or a.get("Recipient Name"))
        if not name:
            continue
        if not (a.get("award_id") or a.get("Award ID") or a.get("award_amount") or a.get("Award Amount")):
            continue
        out.append(
            {
                "supplier_name": name,
                "relationship_type": HISTORICAL_GOVERNMENT_SUPPLIER,
                "confidence": EDGE_VALIDATED,
                "source": "historical_awards",
                "evidence": [
                    _evidence(
                        "historical_awards",
                        a.get("award_id") or a.get("Award ID") or name,
                        "HIGH",
                        field="awardee",
                    )
                ],
                "pricing_evidence": {
                    "award_amount": a.get("award_amount") or a.get("Award Amount"),
                    "award_date": a.get("award_date") or a.get("Start Date"),
                    "temporal_class": "HISTORICAL",
                },
                "product": product,
            }
        )
    return out


def candidates_from_validated_quotes(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Verified offers / HIGH pricing evidence only — no outreach."""
    out: list[dict[str, Any]] = []
    product = _product_keys_from_row(row)
    if not product.get("product_dedupe_key") and not product.get("product_id"):
        return []

    # Pipeline-attached offers
    for offer in row.get("supplier_offers") or row.get("verified_offers") or []:
        if not isinstance(offer, dict):
            continue
        status = str(offer.get("verification_status") or offer.get("validation_status") or "").upper()
        source_type = str(offer.get("source_type") or "").upper()
        if status not in {"VALIDATED", "VERIFIED", "HIGH"} and source_type not in {"FORMAL_QUOTE"}:
            continue
        if status not in {"VALIDATED", "VERIFIED", "HIGH"} and str(offer.get("confidence") or "").upper() not in {
            "HIGH",
            "VALIDATED",
        }:
            continue
        name = _clean(offer.get("supplier_name") or offer.get("company") or offer.get("name"))
        if not name:
            continue
        rel = _map_relationship(offer.get("relationship") or offer.get("role") or DISTRIBUTOR)
        out.append(
            {
                "supplier_name": name,
                "supplier_id": offer.get("supplier_id"),
                "relationship_type": rel if rel != UNKNOWN else DISTRIBUTOR,
                "confidence": EDGE_VALIDATED,
                "source": "verified_offer",
                "evidence": [
                    _evidence(
                        offer.get("source_url") or offer.get("source_reference") or "supplier_offer",
                        offer.get("quote_number") or offer.get("unit_price") or name,
                        "HIGH",
                        field="formal_quote",
                    )
                ],
                "pricing_evidence": {
                    "unit_price": offer.get("unit_price"),
                    "extended_price": offer.get("extended_price"),
                    "source_type": source_type or "FORMAL_QUOTE",
                    "temporal_class": offer.get("temporal_class") or "CURRENT",
                },
                "availability": offer.get("availability") or offer.get("stock_status") or "UNKNOWN",
                "product": product,
            }
        )

    # Supplier intelligence pricing HIGH + validated channels only
    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    if si.get("kind") == "M3SupplierIntelligence":
        cost = str(si.get("ACQUISITION_COST_CONFIDENCE") or "").upper()
        supply = si.get("Supply_chain") if isinstance(si.get("Supply_chain"), dict) else {}
        channels = supply.get("all_channels") or []
        pricing = si.get("Pricing_evidence") if isinstance(si.get("Pricing_evidence"), dict) else {}
        for ch in channels:
            if not isinstance(ch, dict):
                continue
            vstat = str(ch.get("validation_status") or ch.get("status") or "").upper()
            ch_conf = str(ch.get("confidence") or "").upper()
            # Do NOT promote map-only MEDIUM distributors
            if vstat not in {"VALIDATED", "VERIFIED", "HIGH"} and not (
                cost in {"HIGH", "VALIDATED"} and ch_conf in {"HIGH", "VALIDATED"}
            ):
                continue
            name = _clean(ch.get("company") or ch.get("name"))
            if not name:
                continue
            rel = _map_relationship(ch.get("role"))
            if rel == UNKNOWN:
                rel = DISTRIBUTOR
            out.append(
                {
                    "supplier_name": name,
                    "relationship_type": rel,
                    "confidence": EDGE_VALIDATED,
                    "source": "supplier_intelligence_validated_channel",
                    "evidence": [
                        _evidence(
                            ch.get("website") or ch.get("evidence_source") or "supplier_intelligence",
                            name,
                            "HIGH",
                            field="validated_channel",
                        )
                    ],
                    "pricing_evidence": {
                        "primary_level": pricing.get("primary_level"),
                        "temporal_class": "CURRENT",
                    },
                    "availability": "UNKNOWN",
                    "product": product,
                }
            )
    return out


def candidates_from_manufacturer(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Validated OEM/manufacturer on exact product identity."""
    product = _product_keys_from_row(row)
    if not product.get("nsn") and not product.get("part_number"):
        return []
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}
    mfr = struct.get("oem") or struct.get("manufacturer") or row.get("manufacturer")
    mfr_wrap = fields.get("oem") or fields.get("manufacturer")
    conf = "HIGH"
    if isinstance(mfr_wrap, dict):
        mfr = mfr or mfr_wrap.get("value")
        conf = str(mfr_wrap.get("confidence") or "HIGH").upper()
    cage = struct.get("cage")
    if isinstance(fields.get("cage"), dict):
        cage = cage or fields["cage"].get("value")
    # Promote manufacturer only with CAGE or HIGH OEM on exact identity
    if not mfr:
        return []
    if not cage and conf not in {"HIGH", "VALIDATED"}:
        return []
    if not _is_promotable_confidence(conf) and not cage:
        return []
    return [
        {
            "supplier_name": str(mfr)[:512],
            "relationship_type": MANUFACTURER,
            "confidence": EDGE_VALIDATED if cage or conf in {"HIGH", "VALIDATED"} else EDGE_POSSIBLE,
            "source": "dla_product_structure.oem",
            "evidence": [
                _evidence(
                    "product_identity",
                    f"OEM {mfr}" + (f" CAGE {cage}" if cage else ""),
                    "HIGH" if cage else conf,
                    field="manufacturer",
                )
            ],
            "pricing_evidence": None,
            "availability": "UNKNOWN",
            "product": product,
        }
    ]


def collect_supplier_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    cands: list[dict[str, Any]] = []
    cands.extend(candidates_from_historical_awardees(row))
    cands.extend(candidates_from_validated_quotes(row))
    cands.extend(candidates_from_manufacturer(row))
    # Reject category-only / seed-only names without validation flags
    filtered = []
    for c in cands:
        name = c.get("supplier_name")
        if not name or len(str(name)) < 2:
            continue
        if str(c.get("confidence")) == EDGE_UNKNOWN:
            continue
        # Scraped-looking empty evidence
        if not c.get("evidence"):
            continue
        filtered.append(c)
    return filtered


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class InMemorySupplierGraphBackend:
    def __init__(self) -> None:
        self.products: dict[int, dict[str, Any]] = {}
        self.suppliers: dict[int, dict[str, Any]] = {}
        self.offers: list[dict[str, Any]] = []
        self.edges: dict[str, dict[str, Any]] = {}
        self.index: dict[str, Any] = {"by_edge_id": {}, "by_product": {}, "by_supplier": {}}
        self._next_pid = 1
        self._next_sid = 1

    def ensure_product(self, product: dict[str, Any]) -> dict[str, Any]:
        pid = product.get("product_id")
        dedupe = product.get("product_dedupe_key")
        if pid and int(pid) in self.products:
            return self.products[int(pid)]
        for p in self.products.values():
            specs = p.get("specifications_json") or {}
            if dedupe and specs.get("dedupe_key") == dedupe:
                return p
            if product.get("nsn") and specs.get("nsn") == product.get("nsn"):
                return p
        pid = self._next_pid
        self._next_pid += 1
        row = {
            "id": pid,
            "part_number": product.get("part_number"),
            "description": product.get("title"),
            "specifications_json": {
                "dedupe_key": dedupe,
                "nsn": product.get("nsn"),
                "supplier_graph_edges": [],
            },
            "verification_status": "VALIDATED",
            "source": "supplier_product_graph",
        }
        self.products[pid] = row
        return row

    def upsert_supplier(self, name: str, *, source: str | None = None, relationship: str | None = None) -> dict[str, Any]:
        key = _norm_name(name)
        for s in self.suppliers.values():
            if _norm_name(s["name"]) == key:
                if relationship and relationship != UNKNOWN:
                    s["manufacturer_relationship"] = relationship
                return deepcopy(s)
        sid = self._next_sid
        self._next_sid += 1
        row = {
            "id": sid,
            "name": name[:512],
            "source": source or "supplier_product_graph",
            "verification_status": "VALIDATED",
            "manufacturer_relationship": relationship if relationship != UNKNOWN else None,
            "crm_json": {"build": BUILD_TAG},
        }
        self.suppliers[sid] = row
        return deepcopy(row)

    def upsert_edge(self, edge: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        eid = edge["edge_id"]
        if eid in self.edges:
            prev = self.edges[eid]
            # Keep stronger confidence
            if _conf_rank(edge.get("confidence")) >= _conf_rank(prev.get("confidence")):
                merged = {**prev, **edge, "created_at": prev.get("created_at"), "updated_at": _utc()}
                self.edges[eid] = merged
                self._index_edge(merged)
                return deepcopy(merged), False
            return deepcopy(prev), False
        edge = dict(edge)
        edge["created_at"] = edge.get("created_at") or _utc()
        edge["updated_at"] = _utc()
        self.edges[eid] = edge
        self._index_edge(edge)
        # Attach to product specs
        pid = edge.get("product_id")
        if pid and int(pid) in self.products:
            specs = dict(self.products[int(pid)].get("specifications_json") or {})
            edges = list(specs.get("supplier_graph_edges") or [])
            if eid not in {e.get("edge_id") for e in edges if isinstance(e, dict)}:
                edges.append({k: edge[k] for k in ("edge_id", "supplier_id", "supplier_name", "relationship_type", "confidence", "source") if k in edge})
            specs["supplier_graph_edges"] = edges[-40:]
            self.products[int(pid)]["specifications_json"] = specs
        # Optional offer row for pricing
        if edge.get("pricing_evidence") and edge.get("product_id") and edge.get("supplier_id"):
            self._add_offer(edge)
        return deepcopy(edge), True

    def _add_offer(self, edge: dict[str, Any]) -> None:
        pe = edge.get("pricing_evidence") or {}
        ref = str((edge.get("evidence") or [{}])[0].get("snippet") or edge["edge_id"])[:512]
        for o in self.offers:
            if o.get("product_id") == edge["product_id"] and o.get("supplier_id") == edge["supplier_id"] and o.get("source_reference") == ref:
                return
        self.offers.append(
            {
                "id": len(self.offers) + 1,
                "product_id": edge["product_id"],
                "supplier_id": edge["supplier_id"],
                "unit_price": pe.get("unit_price"),
                "extended_price": pe.get("award_amount") or pe.get("extended_price"),
                "source_type": pe.get("source_type")
                or ("HISTORICAL_PRICE" if pe.get("temporal_class") == "HISTORICAL" else "OTHER"),
                "temporal_class": pe.get("temporal_class") or "HISTORICAL",
                "verification_status": "VALIDATED",
                "source_reference": ref,
                "availability": edge.get("availability"),
                "notes": json.dumps({"relationship_type": edge.get("relationship_type"), "build": BUILD_TAG}),
            }
        )

    def _index_edge(self, edge: dict[str, Any]) -> None:
        eid = edge["edge_id"]
        self.index.setdefault("by_edge_id", {})[eid] = edge
        pk = edge.get("product_dedupe_key") or f"pid:{edge.get('product_id')}"
        self.index.setdefault("by_product", {}).setdefault(str(pk), [])
        if eid not in self.index["by_product"][str(pk)]:
            self.index["by_product"][str(pk)].append(eid)
        sk = _norm_name(str(edge.get("supplier_name") or ""))
        self.index.setdefault("by_supplier", {}).setdefault(sk, [])
        if eid not in self.index["by_supplier"][sk]:
            self.index["by_supplier"][sk].append(eid)


def _conf_rank(c: Any) -> int:
    return {EDGE_VALIDATED: 3, "HIGH": 3, EDGE_POSSIBLE: 2, "MEDIUM": 2, EDGE_UNKNOWN: 0}.get(str(c or "").upper(), 0)


class SqlSupplierGraphBackend:
    """Postgres via existing KnowledgeProduct / KnowledgeSupplier / SupplierOffer."""

    def __init__(self, session: Any, index: dict[str, Any] | None = None) -> None:
        self.session = session
        self.index = index or {"by_edge_id": {}, "by_product": {}, "by_supplier": {}}

    def ensure_product(self, product: dict[str, Any]) -> dict[str, Any]:
        from models import KnowledgeProduct

        pid = product.get("product_id")
        if pid:
            row = self.session.query(KnowledgeProduct).filter_by(id=int(pid)).one_or_none()
            if row:
                return {"id": row.id, "specifications_json": row.specifications_json or {}, "part_number": row.part_number}
        dedupe = product.get("product_dedupe_key")
        nsn = product.get("nsn")
        if nsn:
            for row in self.session.query(KnowledgeProduct).all():
                specs = row.specifications_json or {}
                if str(specs.get("nsn") or "").upper() == str(nsn).upper() or specs.get("dedupe_key") == dedupe:
                    return {"id": row.id, "specifications_json": specs, "part_number": row.part_number}
        row = KnowledgeProduct(
            part_number=product.get("part_number"),
            description=(product.get("title") or "")[:500] or None,
            specifications_json={"dedupe_key": dedupe, "nsn": nsn, "supplier_graph_edges": []},
            source="supplier_product_graph",
            verification_status="VALIDATED",
            last_verified_at=now_utc(),
        )
        self.session.add(row)
        self.session.flush()
        return {"id": row.id, "specifications_json": row.specifications_json or {}, "part_number": row.part_number}

    def upsert_supplier(self, name: str, *, source: str | None = None, relationship: str | None = None) -> dict[str, Any]:
        from models import KnowledgeSupplier

        existing = (
            self.session.query(KnowledgeSupplier)
            .filter(KnowledgeSupplier.name.ilike(name[:512]))
            .order_by(KnowledgeSupplier.id.asc())
            .first()
        )
        if existing:
            if relationship and relationship != UNKNOWN:
                existing.manufacturer_relationship = relationship
            if existing.verification_status in (None, "", "UNKNOWN"):
                existing.verification_status = "VALIDATED"
            self.session.flush()
            return {"id": existing.id, "name": existing.name, "verification_status": existing.verification_status}
        row = KnowledgeSupplier(
            name=name[:512],
            source=source or "supplier_product_graph",
            verification_status="VALIDATED",
            manufacturer_relationship=relationship if relationship != UNKNOWN else None,
            last_verified_at=now_utc(),
            notes=f"BUILD 8 supplier-product graph — no outreach",
            crm_json={"build": BUILD_TAG, "outreach_triggered": False},
        )
        self.session.add(row)
        self.session.flush()
        return {"id": row.id, "name": row.name, "verification_status": row.verification_status}

    def upsert_edge(self, edge: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        from models import KnowledgeProduct, SupplierOffer

        eid = edge["edge_id"]
        prev = (self.index.get("by_edge_id") or {}).get(eid)
        created = prev is None
        if prev and _conf_rank(prev.get("confidence")) > _conf_rank(edge.get("confidence")):
            return prev, False

        stored = dict(edge)
        stored["updated_at"] = _utc()
        if prev and prev.get("created_at"):
            stored["created_at"] = prev["created_at"]
        self.index.setdefault("by_edge_id", {})[eid] = stored
        pk = stored.get("product_dedupe_key") or f"pid:{stored.get('product_id')}"
        self.index.setdefault("by_product", {}).setdefault(str(pk), [])
        if eid not in self.index["by_product"][str(pk)]:
            self.index["by_product"][str(pk)].append(eid)

        # Persist summary on KnowledgeProduct.specifications_json
        if stored.get("product_id"):
            row = self.session.query(KnowledgeProduct).filter_by(id=int(stored["product_id"])).one_or_none()
            if row:
                specs = dict(row.specifications_json or {})
                edges = list(specs.get("supplier_graph_edges") or [])
                edges = [e for e in edges if isinstance(e, dict) and e.get("edge_id") != eid]
                edges.append(
                    {
                        "edge_id": eid,
                        "supplier_id": stored.get("supplier_id"),
                        "supplier_name": stored.get("supplier_name"),
                        "relationship_type": stored.get("relationship_type"),
                        "confidence": stored.get("confidence"),
                        "source": stored.get("source"),
                        "timestamp": stored.get("timestamp"),
                    }
                )
                specs["supplier_graph_edges"] = edges[-40:]
                row.specifications_json = specs
                self.session.flush()

        pe = stored.get("pricing_evidence") or {}
        if stored.get("product_id") and stored.get("supplier_id") and pe:
            ref = str((stored.get("evidence") or [{}])[0].get("snippet") or eid)[:512]
            existing = (
                self.session.query(SupplierOffer)
                .filter_by(
                    product_id=int(stored["product_id"]),
                    supplier_id=int(stored["supplier_id"]),
                    source_reference=ref,
                )
                .first()
            )
            if not existing:
                amt = pe.get("award_amount") or pe.get("extended_price")
                up = pe.get("unit_price")
                offer = SupplierOffer(
                    product_id=int(stored["product_id"]),
                    supplier_id=int(stored["supplier_id"]),
                    extended_price=Decimal(str(amt)) if amt not in (None, "", "UNKNOWN") else None,
                    unit_price=Decimal(str(up)) if up not in (None, "", "UNKNOWN") else None,
                    source_type=str(pe.get("source_type") or ("HISTORICAL_PRICE" if pe.get("temporal_class") == "HISTORICAL" else "OTHER")),
                    temporal_class=str(pe.get("temporal_class") or "HISTORICAL"),
                    verification_status="VALIDATED",
                    source_reference=ref,
                    availability=stored.get("availability"),
                    notes=json.dumps(
                        {"relationship_type": stored.get("relationship_type"), "build": BUILD_TAG, "outreach_triggered": False}
                    ),
                    last_verified_at=now_utc(),
                    validation_status="VALIDATED",
                )
                self.session.add(offer)
                self.session.flush()
        return stored, created


def load_graph_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == GRAPH_INDEX_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_edge_id", {})
                    data.setdefault("by_product", {})
                    data.setdefault("by_supplier", {})
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"kind": "M3SupplierProductGraphIndex", "by_edge_id": {}, "by_product": {}, "by_supplier": {}, "build": BUILD_TAG}


def save_graph_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == GRAPH_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=GRAPH_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def harden_supplier_product_graph(
    row: dict[str, Any],
    *,
    backend: Any | None = None,
    persist_index: bool = False,
) -> dict[str, Any]:
    """
    Project validated supplier↔product edges for one opportunity/product row.

    Idempotent. No outreach. Weak evidence skipped.
    """
    backend = backend or InMemorySupplierGraphBackend()
    candidates = collect_supplier_candidates(row)
    out: dict[str, Any] = {
        "kind": "M3SupplierProductGraphHardening",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id"),
        "projected": False,
        "edges_created": 0,
        "edges_updated": 0,
        "edges_skipped": 0,
        "edges": [],
        "skipped": [],
        "outreach_triggered": False,
        "pipeline_json_preserved": True,
        "pipeline_annotation": None,
    }

    if not candidates:
        out["skipped"].append({"reason": "no_promotable_supplier_candidates"})
        return out

    for cand in candidates:
        product_meta = cand.get("product") or _product_keys_from_row(row)
        if not product_meta.get("product_dedupe_key") and not product_meta.get("product_id"):
            out["edges_skipped"] += 1
            out["skipped"].append({"reason": "no_exact_product", "supplier": cand.get("supplier_name")})
            continue
        if str(cand.get("confidence")) not in {EDGE_VALIDATED, EDGE_POSSIBLE}:
            out["edges_skipped"] += 1
            out["skipped"].append({"reason": "weak_confidence", "supplier": cand.get("supplier_name")})
            continue
        if str(cand.get("confidence")) == EDGE_POSSIBLE and str(cand.get("relationship_type")) not in {
            HISTORICAL_GOVERNMENT_SUPPLIER,
            MANUFACTURER,
        }:
            # Possible non-historical distributors stay out of durable VALIDATED graph
            out["edges_skipped"] += 1
            out["skipped"].append({"reason": "possible_non_core_not_promoted", "supplier": cand.get("supplier_name")})
            continue

        product = backend.ensure_product(product_meta)
        supplier = backend.upsert_supplier(
            str(cand["supplier_name"]),
            source=cand.get("source"),
            relationship=cand.get("relationship_type"),
        )
        rel = _map_relationship(cand.get("relationship_type"))
        eid = edge_key(
            product_key=str(product_meta.get("product_dedupe_key") or f"pid:{product['id']}"),
            supplier_key=_norm_name(str(cand["supplier_name"])),
            relationship=rel,
        )
        edge = empty_edge(
            edge_id=eid,
            product_id=product["id"],
            product_dedupe_key=product_meta.get("product_dedupe_key"),
            product_nsn=product_meta.get("nsn"),
            supplier_id=supplier["id"],
            supplier_name=supplier["name"],
            relationship_type=rel,
            source=cand.get("source"),
            evidence=cand.get("evidence") or [],
            confidence=cand.get("confidence"),
            pricing_evidence=cand.get("pricing_evidence"),
            availability=cand.get("availability") or UNKNOWN,
            outreach_triggered=False,
        )
        stored, created = backend.upsert_edge(edge)
        out["edges"].append(stored)
        if created:
            out["edges_created"] += 1
        else:
            out["edges_updated"] += 1
        out["projected"] = True

    if out["projected"]:
        out["pipeline_annotation"] = {
            "supplier_product_graph": {
                "build": BUILD_TAG,
                "projected_at": _utc(),
                "edge_count": len(out["edges"]),
                "edges": [
                    {
                        "edge_id": e.get("edge_id"),
                        "supplier_name": e.get("supplier_name"),
                        "supplier_id": e.get("supplier_id"),
                        "relationship_type": e.get("relationship_type"),
                        "confidence": e.get("confidence"),
                        "source": e.get("source"),
                    }
                    for e in out["edges"]
                ],
                "knowledge_product_id": out["edges"][0].get("product_id") if out["edges"] else None,
                "outreach_triggered": False,
            }
        }

    if persist_index and hasattr(backend, "index"):
        try:
            # merge durable
            durable = load_graph_index()
            backend.index.setdefault("by_edge_id", {}).update(durable.get("by_edge_id") or {})
            for eid, edge in (backend.index.get("by_edge_id") or {}).items():
                durable.setdefault("by_edge_id", {})[eid] = edge
            save_graph_index(durable if persist_index else backend.index)
            # Actually save backend index which has new edges
            idx = load_graph_index()
            idx.setdefault("by_edge_id", {}).update(backend.index.get("by_edge_id") or {})
            idx.setdefault("by_product", {}).update(backend.index.get("by_product") or {})
            save_graph_index(idx)
        except Exception:
            pass

    return out


def build_supplier_product_graph_view(
    *,
    store: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read assembly of durable edges from annotations + index (no outreach)."""
    if rows is None:
        if store is None:
            from m3_pipeline_store import M3PipelineStore

            store = M3PipelineStore()
        rows = list(store.all()) if hasattr(store, "all") else []

    edges: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ann = row.get("supplier_product_graph") if isinstance(row.get("supplier_product_graph"), dict) else {}
        for e in ann.get("edges") or []:
            if isinstance(e, dict):
                edges.append({**e, "opportunity_id": row.get("canonical_id")})
        # Also collect from harden dry-run without persist for rows lacking annotation
        if not ann.get("edges"):
            result = harden_supplier_product_graph(row, backend=InMemorySupplierGraphBackend(), persist_index=False)
            for e in result.get("edges") or []:
                edges.append({**e, "opportunity_id": row.get("canonical_id")})

    # Dedupe by edge_id
    by: dict[str, dict[str, Any]] = {}
    for e in edges:
        eid = e.get("edge_id") or edge_key(
            product_key=str(e.get("product_dedupe_key") or e.get("product_id")),
            supplier_key=_norm_name(str(e.get("supplier_name") or "")),
            relationship=str(e.get("relationship_type") or UNKNOWN),
        )
        e["edge_id"] = eid
        prev = by.get(eid)
        if prev is None or _conf_rank(e.get("confidence")) >= _conf_rank(prev.get("confidence")):
            by[eid] = e

    limited = list(by.values())[: max(1, min(500, int(limit or 100)))]
    by_rel: dict[str, int] = {}
    for e in limited:
        t = str(e.get("relationship_type") or UNKNOWN)
        by_rel[t] = by_rel.get(t, 0) + 1

    return {
        "kind": "M3SupplierProductGraphView",
        "build": BUILD_TAG,
        "question": "Who can realistically supply this product?",
        "count": len(limited),
        "by_relationship": by_rel,
        "edges": limited,
        "outreach_triggered": False,
        "generated_at": _utc(),
    }
