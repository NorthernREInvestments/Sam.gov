"""µLab source router — routes evidence needs to ordered sources (not a data warehouse).

Reuses product_resale_source_intelligence recipes + discovery.dla_source_map.
Does not scrape or permanently mirror external systems.
"""

from __future__ import annotations

from typing import Any

BUILD_TAG = "20260922-m3-micro-lab-integrity-1"

# Evidence needs
NEED_LIVE_OPPORTUNITY = "LIVE_OPPORTUNITY"
NEED_FEDERAL_AWARD_HISTORY = "FEDERAL_AWARD_HISTORY"
NEED_USASPENDING_PATTERN = "USASPENDING_BUYER_PATTERN"
NEED_DIBBS_RFQ = "DIBBS_RFQ"
NEED_DLA_ITEM_IDENTITY = "DLA_ITEM_IDENTITY"
NEED_DLA_TECH_DATA = "DLA_TECH_DATA"
NEED_DLA_PACKAGING = "DLA_PACKAGING"
NEED_COMMERCIAL_PRICE = "COMMERCIAL_PRICE"
NEED_GSA_CATALOG = "GSA_CATALOG_PRICE"
NEED_STATE_LOCAL_SOLICITATION = "STATE_LOCAL_SOLICITATION"
NEED_STATE_LOCAL_AWARD = "STATE_LOCAL_AWARD_HISTORY"
NEED_AGGREGATOR_RESOLVE = "AGGREGATOR_AUTHORITATIVE_RESOLVE"

AUTH_ISSUER = "AUTHORITATIVE_ISSUER"
AUTH_GOV_HISTORY = "AUTHORITATIVE_GOVERNMENT_HISTORY"
AUTH_ITEM_MASTER = "AUTHORITATIVE_ITEM_MASTER"
AUTH_MANUFACTURER = "MANUFACTURER"
AUTH_AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
AUTH_COMMERCIAL_SELLER = "COMMERCIAL_SELLER"
AUTH_AGGREGATOR = "AGGREGATOR"
AUTH_SEARCH = "SEARCH_DISCOVERY"

# Priority: lower = stronger
AUTHORITY_RANK = {
    AUTH_ISSUER: 0,
    AUTH_GOV_HISTORY: 1,
    AUTH_ITEM_MASTER: 2,
    AUTH_MANUFACTURER: 3,
    AUTH_AUTHORIZED_DISTRIBUTOR: 4,
    AUTH_COMMERCIAL_SELLER: 5,
    AUTH_AGGREGATOR: 6,
    AUTH_SEARCH: 7,
}


def _route(source_id: str, name: str, authority: str, *, how: str, fields: list[str], notes: str = "") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "name": name,
        "authority": authority,
        "authority_rank": AUTHORITY_RANK.get(authority, 99),
        "how_to_query": how,
        "extract_fields": fields,
        "notes": notes,
        "warehouse": False,
    }


# Static registry of HOW/WHERE — not copied content
EVIDENCE_ROUTES: dict[str, list[dict[str, Any]]] = {
    NEED_LIVE_OPPORTUNITY: [
        _route(
            "fed_sam_contract_opportunities",
            "SAM.gov Contract Opportunities API v2",
            AUTH_ISSUER,
            how="Existing SAM API/client; notice ID / solicitation search",
            fields=["notice_id", "solicitation_number", "title", "agency", "deadline", "naics", "psc", "attachments", "description"],
            notes="Authoritative federal opportunity index — not aggregator",
        ),
        _route(
            "dla_dibbs",
            "DLA DIBBS RFQ",
            AUTH_ISSUER,
            how="Existing DIBBS integration or SAM SPE*/SPR* reconciliation; no bot bypass",
            fields=["nsn", "fsc", "clin", "quantity", "uom", "approved_cages", "pid", "fob", "packaging"],
            notes="Primary DLA product transaction source when accessible",
        ),
        _route(
            "state_local_issuer_portal",
            "Issuing government procurement portal",
            AUTH_ISSUER,
            how="Issuer-specific adapter / public HTML; never invent local thresholds",
            fields=["solicitation_number", "deadline", "line_items", "response_method"],
        ),
        _route(
            "aggregator_lead",
            "BidNet / Sovra / similar aggregator",
            AUTH_AGGREGATOR,
            how="Discovery lead only → resolve to issuer portal before COMPLETE",
            fields=["buyer", "title", "solicitation_number", "geography"],
            notes="AGGREGATOR_LEAD_ONLY until authoritative solicitation resolved",
        ),
    ],
    NEED_FEDERAL_AWARD_HISTORY: [
        _route(
            "sam_contract_awards",
            "SAM.gov Contract Awards / Contract Data",
            AUTH_GOV_HISTORY,
            how="SAM Contract Data search (FPDS successor); existing award clients",
            fields=["award_id", "awardee", "award_amount", "award_date", "naics", "psc", "piid"],
            notes="Do not build new FPDS website deps; SAM is source of record",
        ),
        _route(
            "dla_dibbs_awards",
            "DIBBS award / procurement history",
            AUTH_GOV_HISTORY,
            how="NSN-linked DIBBS history when available",
            fields=["nsn", "unit_price", "quantity", "uom", "awardee", "award_date"],
        ),
    ],
    NEED_USASPENDING_PATTERN: [
        _route(
            "usaspending_api_v2",
            "USAspending API v2",
            AUTH_GOV_HISTORY,
            how="Existing usaspending_client — awards/transactions/agency patterns",
            fields=["obligation", "recipient", "awarding_agency", "psc", "naics", "award_date"],
            notes="Obligation ≠ unit price without verified quantity/UOM",
        ),
    ],
    NEED_DIBBS_RFQ: [
        _route(
            "dla_dibbs_rfq_recents",
            "DIBBS Recent RFQs",
            AUTH_ISSUER,
            how="discovery.dla_source_map URLs + existing enrichment; SAM reconciliation if bot-blocked",
            fields=["solicitation", "nsn", "item_name", "qty", "uoi"],
        ),
        _route(
            "dla_sam_contract_opportunities",
            "DLA via SAM.gov",
            AUTH_ISSUER,
            how="SAM org-path / SPE* SPR* filter",
            fields=["solicitation_number", "description", "resourceLinks"],
        ),
    ],
    NEED_DLA_ITEM_IDENTITY: [
        _route(
            "dibbs_pid",
            "DIBBS Procurement Item Description",
            AUTH_ITEM_MASTER,
            how="Solicitation NSN/PID link first",
            fields=["nsn", "niin", "fsc", "approved_cages", "approved_part_numbers", "nomenclature"],
        ),
        _route(
            "dla_flis_webflis",
            "DLA FLIS / WebFLIS-family item data",
            AUTH_ITEM_MASTER,
            how="Supplier Portal / FLIS identity corroboration — not every product has NSN",
            fields=["nsn", "cage", "part_number", "item_name"],
        ),
    ],
    NEED_DLA_TECH_DATA: [
        _route(
            "dla_tdmt",
            "DLA TDMT technical data",
            AUTH_ITEM_MASTER,
            how="Detect TDMT refs; classify TECH_DATA_REQUIRED_BUT_UNAVAILABLE if gated",
            fields=["drawing_refs", "spec_refs", "access_state"],
            notes="Do not invent drawings from internet pages",
        ),
    ],
    NEED_DLA_PACKAGING: [
        _route(
            "solicitation_section_bd",
            "Solicitation Section B/D packaging & marking",
            AUTH_ISSUER,
            how="Parse solicitation text; flag SPECIAL_PACKAGING_REQUIRED / MIL-STD-2073",
            fields=["mil_std", "unit_pack", "preservation", "marking", "rfid"],
            notes="MIL-STD packaging ≠ automatic reject",
        ),
    ],
    NEED_COMMERCIAL_PRICE: [
        _route(
            "manufacturer_public",
            "Manufacturer public pricing",
            AUTH_MANUFACTURER,
            how="Exact MPN / model lookup",
            fields=["price", "sku", "pack", "url"],
        ),
        _route(
            "authorized_distributor",
            "Authorized distributor",
            AUTH_AUTHORIZED_DISTRIBUTOR,
            how="Existing m3_supplier_intelligence / public listings",
            fields=["price", "seller", "pack", "availability"],
        ),
        _route(
            "industrial_distributor",
            "Industrial / commercial distributor",
            AUTH_COMMERCIAL_SELLER,
            how="MSC / Grainger / Fastenal / similar — retail ≠ wholesale label",
            fields=["price", "seller", "pack"],
        ),
        _route(
            "gsa_commercial_platforms",
            "GSA Commercial Platforms Program participants",
            AUTH_COMMERCIAL_SELLER,
            how="Query current GSA Commercial Platforms page for awarded platforms; then platform product search",
            fields=["price", "seller", "platform"],
            notes="Do not hard-code stale platform list forever",
        ),
        _route(
            "search_discovery",
            "Web search discovery lead",
            AUTH_SEARCH,
            how="Cost-governor gated search — discovery only until exact identity confirmed",
            fields=["candidate_urls"],
        ),
    ],
    NEED_GSA_CATALOG: [
        _route(
            "gsa_advantage",
            "GSA Advantage",
            AUTH_COMMERCIAL_SELLER,
            how="Catalog price intelligence; selling may need Schedule",
            fields=["price", "sin", "vendor"],
        ),
        _route(
            "gsa_ebuy",
            "GSA eBuy",
            AUTH_ISSUER,
            how="RFQ via established vehicle — VEHICLE_ACCESS_REQUIRED / PARTNER_REQUIRED if no access",
            fields=["rfq_id", "deadline", "vehicle"],
        ),
    ],
    NEED_STATE_LOCAL_SOLICITATION: [
        _route(
            "issuer_procurement_site",
            "Issuing government procurement website",
            AUTH_ISSUER,
            how="Portal adapter / public notice",
            fields=["solicitation", "docs", "deadline"],
        ),
        _route(
            "issuer_vendor_portal",
            "Issuing vendor portal",
            AUTH_ISSUER,
            how="Registration may be required — do not invent thresholds",
            fields=["solicitation", "quote_rules"],
        ),
    ],
    NEED_STATE_LOCAL_AWARD: [
        _route(
            "issuer_award_tabulation",
            "Issuer bid tabulation / award / PO records",
            AUTH_GOV_HISTORY,
            how="Agency award notices, board records when directly connected",
            fields=["unit_price", "quantity", "awardee", "date"],
        ),
    ],
    NEED_AGGREGATOR_RESOLVE: [
        _route(
            "aggregator_to_issuer",
            "Resolve aggregator lead to authoritative solicitation",
            AUTH_ISSUER,
            how="Extract buyer/sol# → find agency portal → verify line items",
            fields=["authoritative_url", "line_items", "deadline", "response_method"],
        ),
    ],
}


def routes_for(evidence_need: str) -> list[dict[str, Any]]:
    routes = list(EVIDENCE_ROUTES.get(evidence_need) or [])
    return sorted(routes, key=lambda r: int(r.get("authority_rank") or 99))


def route_evidence(need: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return ordered source candidates for an evidence need — no retrieval side effects."""
    ctx = context or {}
    routes = routes_for(need)
    # Prefer DIBBS path when DLA / NSN context
    sid = str(ctx.get("source_id") or "").lower()
    nsn = ctx.get("nsn") or ctx.get("exact_nsn")
    if need == NEED_LIVE_OPPORTUNITY and (nsn or "dla" in sid or "dibbs" in sid):
        routes = sorted(
            routes,
            key=lambda r: (0 if "dibbs" in r["source_id"] or "dla" in r["source_id"] else 1, r["authority_rank"]),
        )
    # Enrich with existing recipe metadata when available
    try:
        from product_resale_source_intelligence import SOURCE_RECIPES

        by_id = {r.get("source_id"): r for r in SOURCE_RECIPES if isinstance(r, dict)}
        for r in routes:
            recipe = by_id.get(r["source_id"])
            if recipe:
                r["recipe_status"] = recipe.get("m3_status")
                r["coverage_state"] = recipe.get("coverage_state")
                r["api_available"] = recipe.get("api_available")
    except Exception:
        pass
    try:
        from discovery.dla_source_map import DLA_PROCUREMENT_SOURCE_MAP

        if need in {NEED_DIBBS_RFQ, NEED_DLA_ITEM_IDENTITY}:
            for m in DLA_PROCUREMENT_SOURCE_MAP:
                if not any(x["source_id"] == m.get("source_id") for x in routes):
                    routes.append(
                        _route(
                            str(m.get("source_id")),
                            str(m.get("name")),
                            AUTH_ISSUER,
                            how=str(m.get("notes") or m.get("url") or ""),
                            fields=["url", "buying_activity"],
                        )
                    )
    except Exception:
        pass
    return {
        "build": BUILD_TAG,
        "evidence_need": need,
        "warehouse": False,
        "routes": routes,
        "strongest_authority": (routes[0].get("authority") if routes else None),
        "note": "Aggregator/search never outranks authoritative issuer when available",
    }


def route_plan_for_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    """Cheap plan: which evidence needs apply and ordered sources — no network I/O."""
    needs = [NEED_LIVE_OPPORTUNITY]
    sid = str(row.get("source_id") or "").lower()
    title = str(row.get("title") or "")
    if row.get("exact_nsn") or row.get("nsn") or "dla" in sid or "dibbs" in sid or title.upper().startswith(("SPE", "SPR")):
        needs.extend([NEED_DIBBS_RFQ, NEED_DLA_ITEM_IDENTITY, NEED_DLA_PACKAGING, NEED_FEDERAL_AWARD_HISTORY])
    elif "sam" in sid or row.get("notice_id"):
        needs.extend([NEED_FEDERAL_AWARD_HISTORY, NEED_USASPENDING_PATTERN])
    else:
        needs.extend([NEED_STATE_LOCAL_SOLICITATION, NEED_STATE_LOCAL_AWARD])
    if str(row.get("source_family") or "").upper() in {"AGGREGATOR", "BIDNET", "SOVRA"} or "bidnet" in sid:
        needs.insert(0, NEED_AGGREGATOR_RESOLVE)
    needs.append(NEED_COMMERCIAL_PRICE)
    plans = {n: route_evidence(n, context=row) for n in needs}
    return {
        "build": BUILD_TAG,
        "canonical_id": row.get("canonical_id"),
        "evidence_needs": needs,
        "plans": plans,
        "signal_vs_transaction": "TRANSACTIONAL_PRODUCT_OPPORTUNITY",
    }


def prefer_stronger(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """Lower-authority source may not silently overwrite stronger evidence."""
    if not a:
        return b
    if not b:
        return a
    ra = AUTHORITY_RANK.get(str(a.get("authority") or ""), 99)
    rb = AUTHORITY_RANK.get(str(b.get("authority") or ""), 99)
    if ra < rb:
        return a
    if rb < ra:
        return b
    # Equal authority — keep existing (a) unless b explicitly marked newer with same authority
    if b.get("retrieved_at") and a.get("retrieved_at") and str(b["retrieved_at"]) > str(a["retrieved_at"]):
        return b
    return a


def provenance(
    *,
    source_type: str,
    authority: str,
    source_reference: str | None,
    evidence_field: str,
    raw_value: Any,
    normalized_value: Any = None,
    confidence: str = "MODERATE",
) -> dict[str, Any]:
    from application_clock import now_utc

    return {
        "source_type": source_type,
        "source_authority": authority,
        "source_reference": source_reference,
        "retrieved_at": now_utc().isoformat(),
        "evidence_field": evidence_field,
        "raw_value": raw_value,
        "normalized_value": normalized_value if normalized_value is not None else raw_value,
        "confidence": confidence,
    }


def stronger_authority(a: str | None, b: str | None) -> bool:
    """True if authority a is strictly stronger than b."""
    return AUTHORITY_RANK.get(str(a or ""), 99) < AUTHORITY_RANK.get(str(b or ""), 99)
