"""Public evidence recovery for transactional opportunities — no portal login."""

from __future__ import annotations
from application_clock import complete_run_metadata, now_utc, start_run_metadata
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from bid_price_targets import compute_bid_price_targets
from deadline_conflict import (
    deadline_evidence_record,
    extract_open_close_from_sciquest_text,
    reconcile_deadline_evidence,
)
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from portal_registration_decision import assess_portal_registration
from preliminary_economics import compute_evidence_bounded_economics
from public_evidence_constants import (
    BLADE_ITEM_CODES,
    BLADE_SOLICITATION,
    EV_AUTHORITATIVE_CURRENT,
    EV_AUTHORITATIVE_HISTORICAL,
    EV_COMMERCIAL_DISTRIBUTOR,
    EV_HISTORICAL_AWARD,
    EV_MANUFACTURER,
    EV_PUBLIC_AGENCY_REFERENCE,
    EV_SEARCH_DISCOVERY,
    EV_THIRD_PARTY_MIRROR,
    MATCH_HISTORICAL,
    MATCH_INSUFFICIENT,
    MATCH_POSSIBLE,
    MATCH_QUOTE,
    MATCH_STRONG_SPEC,
    PRICE_HISTORICAL_GOV_UNIT,
    PRICE_NONE,
    PRICE_PUBLIC_DISTRIBUTOR,
    PRICE_QUOTE_REQUIRED,
    PUBLIC_EVIDENCE_HARD_MAX_HTTP,
    PUBLIC_EVIDENCE_TARGET_HTTP,
)
from public_evidence_models import (
    agency_item_intelligence,
    assert_reconstructed_not_merged_into_requirement,
    empty_reconstructed_specification,
    evidence_fact,
    set_reconstructed_field,
)
from specification_extraction import extract_blade_or_product_specifications

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
PACKETS = ARTIFACTS / "transactional_procurement_packets"
EVIDENCE_DIR = ARTIFACTS / "transactional_procurement_evidence"


def _utc() -> str:
    return now_utc().isoformat()


def _safe_get(client: PublicProcurementHttpClient, url: str, source_id: str = "public_evidence") -> dict[str, Any]:
    try:
        resp = client.get(url, source_id=source_id)
        return {
            "ok": resp.status_code < 400,
            "status_code": resp.status_code,
            "text": resp.text or "",
            "url": url,
            "final_url": getattr(resp.meta, "url", url) if resp.meta else url,
        }
    except Exception as exc:
        return {"ok": False, "status_code": None, "text": "", "url": url, "error": str(exc)[:300]}


def load_local_blade_context(solicitation: str = BLADE_SOLICITATION) -> dict[str, Any]:
    packet_path = PACKETS / f"{solicitation}.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8")) if packet_path.exists() else {}
    text_parts: list[str] = []
    ev = EVIDENCE_DIR / solicitation
    if ev.exists():
        for p in list(ev.glob("*.pdf")) + list(ev.glob("*.txt")):
            if p.suffix.lower() == ".pdf":
                try:
                    from pdf_text import extract_pdf_text

                    text_parts.append(extract_pdf_text(p.read_bytes()) or "")
                except Exception:
                    pass
            else:
                text_parts.append(p.read_text(encoding="utf-8", errors="replace"))
    for d in packet.get("documents") or []:
        if d.get("text_preview"):
            text_parts.append(str(d["text_preview"]))
    return {"packet": packet, "combined_text": "\n\n".join(text_parts)}


def seed_search_queries(solicitation: str = BLADE_SOLICITATION) -> list[str]:
    qs = [
        f'"{solicitation}"',
        f'"{solicitation}" deadline OR close OR due',
        '"Snow Plow Blade Spec 8Sept20263"',
        "Iowa DOT tungsten carbide snow plow blade",
        "Iowa DOT snow plow carbide cutting edge bid",
        "Iowa DOT blades edges snow plow award",
        "Iowa DOT RFB tungsten-carbide blades",
    ]
    for code in BLADE_ITEM_CODES:
        qs.append(f'Iowa DOT "{code}" blade')
    qs.extend(
        [
            "Winter Equipment carbide snow plow blade Iowa",
            "Valley Blades snow plow cutting edge specification",
            "Kennametal snow plow carbide edge",
        ]
    )
    return qs


def harvest_mirror_deadlines(html_or_text: str, *, source_url: str) -> list[dict[str, Any]]:
    """Pull claimed deadlines from third-party mirrors without treating as authoritative."""
    out = []
    # Common patterns near solicitation id
    for m in re.finditer(
        r"(?:due|close|deadline|response)[^\d]{0,40}(\d{1,2}/\d{1,2}/\d{4}(?:[,\s]+\d{1,2}:\d{2}\s*[AP]M(?:\s*[A-Z]{2,4})?)?)",
        html_or_text or "",
        re.I,
    ):
        out.append(
            deadline_evidence_record(
                raw=m.group(1).strip(),
                role="MIRROR_CLAIMED_DEADLINE",
                evidence_class=EV_THIRD_PARTY_MIRROR,
                source_url=source_url,
                document="third_party_mirror",
                page_section=m.group(0)[:80],
                confidence="LOW",
            )
        )
    # Explicit Sept 15 / Oct 7 mentions near tungsten / 2975
    for m in re.finditer(
        r"(September\s+15,?\s+2026|9/15/2026|October\s+7,?\s+2026|10/7/2026)[^\n]{0,40}",
        html_or_text or "",
        re.I,
    ):
        raw = m.group(1)
        # Normalize month names lightly
        if "September" in raw or "september" in raw.lower():
            raw_n = "9/15/2026, 1:00 PM CDT"
        elif "October" in raw or "october" in raw.lower():
            raw_n = "10/7/2026, 1:00 PM CDT"
        else:
            raw_n = raw
        out.append(
            deadline_evidence_record(
                raw=raw_n,
                role="MIRROR_CLAIMED_DEADLINE",
                evidence_class=EV_THIRD_PARTY_MIRROR,
                source_url=source_url,
                document="third_party_mirror",
                page_section=m.group(0)[:100],
                confidence="LOW",
            )
        )
    return out


def build_reconstructed_spec_from_public(
    *,
    authoritative_text: str,
    commercial_texts: list[dict[str, Any]],
    historical_notes: list[str] | None = None,
) -> dict[str, Any]:
    """
    ReconstructedSpecification separate from TransactionalRequirement.
    Authoritative current solicitation facts may seed reconstruction but remain
    marked authoritative_for_current_bid only when sourced from current package —
    reconstructed wrapper always authoritative_for_current_bid=false at root.
    """
    spec = empty_reconstructed_specification()
    # From current event PDF — these are ALSO in TransactionalRequirement; reconstruction
    # copies as PUBLIC/agency reference for research continuity but flags not for bidding merge.
    auth = extract_blade_or_product_specifications(authoritative_text, source_document="event.pdf")
    mapping = {
        "blade_length": "lengths",
        "blade_width": "width",
        "blade_thickness": "thickness",
        "dimensions": "blade_dimensions",
        "carbide_configuration": "carbide_configuration",
        "rubber_configuration": "rubber_encasement",
        "hole_pattern": "hole_pattern",
        "mounting_requirements": "mounting_pattern",
        "material_requirements": "steel_material_requirements",
        "hardness": "hardness",
        "cover_strap_requirements": "cover_strap_requirements",
        "back_support_requirements": "back_support_requirements",
        "acceptable_manufacturers_brands": "acceptable_manufacturers",
        "certifications": "certifications",
        "mill_certifications": "mill_certifications",
        "sample_requirements": "samples_testing",
        "packaging": "packaging",
        "warranty": "warranty",
        "delivery": "delivery_conventions",
    }
    for src_key, dst_key in mapping.items():
        fact = (auth.get("specifications") or {}).get(src_key) or {}
        if fact.get("value") not in (None, "", [], False):
            set_reconstructed_field(
                spec,
                dst_key,
                fact["value"],
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                document="current_event_pdf",
                confidence="HIGH",
                historical_or_current="current_solicitation_excerpt_not_full_spec",
                notes="from_current_event_pdf_not_full_buyer_attachment; reconstructed_root_not_authoritative_package",
            )

    for ct in commercial_texts:
        text = ct.get("text") or ""
        url = ct.get("url")
        cls = ct.get("evidence_class") or EV_MANUFACTURER
        extracted = extract_blade_or_product_specifications(text, source_document=ct.get("document"))
        for src_key, dst_key in mapping.items():
            fact = (extracted.get("specifications") or {}).get(src_key) or {}
            existing = spec["fields"].get(dst_key) or {}
            if fact.get("value") not in (None, "", []) and existing.get("value") in (None, "", []):
                set_reconstructed_field(
                    spec,
                    dst_key,
                    fact["value"],
                    evidence_class=cls,
                    source_url=url,
                    document=ct.get("document"),
                    confidence="LOW",
                    historical_or_current="commercial",
                    notes="commercial_catalog_not_current_solicitation_requirement",
                )
        # Manufacturer names
        for name in re.findall(r"\b(Winter Equipment|Valley Blades|Kennametal|Nordic|Carbide)\b", text, re.I):
            cur = spec["fields"]["historical_manufacturers"].get("value") or []
            if isinstance(cur, str):
                cur = [cur]
            if name not in cur:
                cur.append(name)
            set_reconstructed_field(
                spec,
                "historical_manufacturers",
                cur,
                evidence_class=cls,
                source_url=url,
                confidence="MEDIUM",
                historical_or_current="commercial",
            )

    for note in historical_notes or []:
        set_reconstructed_field(
            spec,
            "equivalent_products",
            note,
            evidence_class=EV_SEARCH_DISCOVERY,
            confidence="LOW",
            historical_or_current="historical",
            notes="search_discovery_only",
        )

    # Enforce all fields non-authoritative for bidding
    for f in spec["fields"].values():
        f["authoritative_for_bidding"] = False
        f["authoritative_for_current_bid"] = False
    return spec


def classify_commercial_match(product: dict[str, Any], requirement_hints: dict[str, Any]) -> str:
    """Never claim current-solicitation compliance without authoritative current requirements."""
    desc = (product.get("description") or product.get("product_family") or "").lower()
    if not desc:
        return MATCH_INSUFFICIENT
    hints = " ".join(str(v) for v in (requirement_hints or {}).values() if v).lower()
    score = 0
    for token in ("carbide", "tungsten", "snow", "plow", "blade", "cutting edge", "rubber"):
        if token in desc:
            score += 1
    if product.get("historical_iowa"):
        return MATCH_HISTORICAL
    if score >= 3 and ("carbide" in desc or "carbide" in hints):
        # Strong commercial similarity — still not authoritative compliance
        return MATCH_STRONG_SPEC if product.get("dimensions") else MATCH_POSSIBLE
    if score >= 2:
        return MATCH_POSSIBLE
    if score >= 1:
        return MATCH_QUOTE
    return MATCH_INSUFFICIENT


def run_public_evidence_recovery(
    *,
    solicitation: str = BLADE_SOLICITATION,
    authorize_live: bool = False,
    max_http: int = PUBLIC_EVIDENCE_HARD_MAX_HTTP,
    target_http: int = PUBLIC_EVIDENCE_TARGET_HTTP,
    search_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Orchestrate public evidence recovery for the blade deal.
    search_hits: optional pre-collected search results [{title,url,snippet}] to avoid
    depending on a search API — caller may inject WebSearch results.
    """
    run_meta = start_run_metadata(extra={"run_kind": "public_evidence_recovery", "solicitation": solicitation})
    local = load_local_blade_context(solicitation)
    packet = local["packet"]
    auth_text = local["combined_text"]

    budget = RequestBudget(
        max_total_requests=max_http,
        max_requests_per_source=max_http,
        max_pages_per_source=10,
        max_records_per_source=50,
        max_runtime_seconds=240.0,
        min_interval_seconds=1.0,
        timeout_seconds=30.0,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=authorize_live)

    deadline_records = extract_open_close_from_sciquest_text(
        auth_text,
        source_url="sciquest_event_pdf",
        document=f"{solicitation}-event.pdf",
    )
    # Also record known M3 packet deadline as authoritative close candidate
    pkt_deadline = ((packet.get("requirement") or {}).get("terms") or {}).get("bid_deadline") or {}
    if pkt_deadline.get("value"):
        deadline_records.append(
            deadline_evidence_record(
                raw=str(pkt_deadline["value"]),
                role="CLOSE",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                source_url="m3_packet",
                document="transactional_procurement_packet",
                page_section="bid_deadline",
                confidence="HIGH",
            )
        )

    search_log: list[dict[str, Any]] = []
    fetched_pages: list[dict[str, Any]] = []
    item_intel: list[dict[str, Any]] = []
    historical_purchases: list[dict[str, Any]] = []
    commercial_matches: list[dict[str, Any]] = []
    price_evidence: list[dict[str, Any]] = []
    commercial_texts: list[dict[str, Any]] = []

    # Initialize item codes from current authoritative line items
    line_items = (packet.get("requirement") or {}).get("line_items") or []
    code_to_desc = {
        str(li.get("CLIN_or_item_number")): li.get("description")
        for li in line_items
        if li.get("CLIN_or_item_number")
    }
    for code in BLADE_ITEM_CODES:
        item_intel.append(
            agency_item_intelligence(
                agency_item_code=code,
                historical_description=code_to_desc.get(code),
                known_dimensions="3 FT" if "3673" in code or code.endswith("530") or code.endswith("600") else (
                    "4 FT" if "3674" in code or code.endswith("540") or code.endswith("700") else None
                ),
                source_url="current_event_pdf",
                evidence_class=EV_AUTHORITATIVE_CURRENT,
                confidence="HIGH" if code in code_to_desc else "MEDIUM",
                authoritative_for_current_bid=bool(code in code_to_desc),
            )
        )

    hits = list(search_hits or [])
    # Deterministic public URLs worth trying (no login)
    candidate_urls = [
        "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa",
        "https://starbridge.ai/rfp/tungsten-carbide-blades-for-snow-ice-removal",
        "https://www.cleat.ai/government/contracts/tungsten-carbide-blades-for-snow-ice-removal-wklv",
        "https://www.winterequipment.com/",
        "https://www.valleyblades.com/",
        "https://www.kennametal.com/",
        "https://iowadot.gov/consultants-contractors/contracts/general-letting-information/bid-item-information",
    ]
    for h in hits:
        u = h.get("url")
        if u and u not in candidate_urls:
            candidate_urls.append(u)
        search_log.append({"query": h.get("query"), "title": h.get("title"), "url": u, "snippet": h.get("snippet")})

    # Seed known related historical solicitation from public mirrors (no award $ invented)
    historical_purchases.append(
        {
            "solicitation_number": "645-DOTRFB-2907-2027",
            "date": "2026-07",
            "quantities": None,
            "awarded_vendor": None,
            "award_amount": None,
            "unit_price": None,
            "product_manufacturer": None,
            "bid_count": None,
            "source_url": "https://www.govcb.com/government-bids/CARBIDE-BLADES-FOR-SNOW-ICE-REMOVAL-23761006.htm",
            "evidence_class": EV_THIRD_PARTY_MIRROR,
            "confidence": "MEDIUM",
            "notes": (
                "Related prior Iowa DOT carbide blades RFB (same buyer contact). "
                "Open/Close dates reported by third-party mirrors; award/unit prices NOT found publicly."
            ),
            "contract_type": "ONE_TIME_PRODUCT_PURCHASE",
            "authoritative_for_current_bid": False,
        }
    )

    if authorize_live:
        for url in candidate_urls:
            if client.request_count >= target_http and client.request_count >= 8:
                # keep some budget headroom but allow up to hard max for critical pages
                if client.request_count >= max_http:
                    break
            if client.request_count >= max_http:
                break
            page = _safe_get(client, url)
            fetched_pages.append({k: v for k, v in page.items() if k != "text"} | {"text_len": len(page.get("text") or "")})
            text = page.get("text") or ""
            if not page.get("ok"):
                continue

            # Deadline harvest from listing / mirrors
            if "sciquest" in url or "2975" in text or "Tungsten" in text:
                deadline_records.extend(harvest_mirror_deadlines(text, source_url=url))
                # Refresh open/close from live listing row if present
                if "2975" in text:
                    deadline_records.extend(
                        extract_open_close_from_sciquest_text(text, source_url=url, document="live_listing")
                    )

            # Item codes on page
            for code in BLADE_ITEM_CODES:
                if code in text:
                    item_intel.append(
                        agency_item_intelligence(
                            agency_item_code=code,
                            historical_description=f"code_mentioned_on_page",
                            source_url=url,
                            evidence_class=EV_PUBLIC_AGENCY_REFERENCE
                            if "iowa" in url.lower() or "sciquest" in url.lower()
                            else EV_SEARCH_DISCOVERY,
                            confidence="LOW",
                        )
                    )

            # Manufacturer pages
            if "winterequipment" in url:
                commercial_texts.append(
                    {"text": text[:50000], "url": url, "document": "winterequipment", "evidence_class": EV_MANUFACTURER}
                )
                commercial_matches.append(
                    {
                        "manufacturer": "Winter Equipment Company",
                        "product_family": "snow plow cutting edges / carbide blades",
                        "part_sku": None,
                        "dimensions": None,
                        "technical_specification": None,
                        "public_price": None,
                        "quote_required": True,
                        "source_url": url,
                        "evidence_class": EV_MANUFACTURER,
                        "fit_state": MATCH_POSSIBLE,
                        "compliant_with_current_solicitation": False,
                        "compliance_note": "commercial_presence_only_not_authoritative_compliance",
                    }
                )
                price_evidence.append(
                    {
                        "price_class": PRICE_QUOTE_REQUIRED,
                        "unit_price": None,
                        "date": None,
                        "source_url": url,
                        "notes": "manufacturer_site_no_public_unit_price_captured",
                    }
                )
            if "valleyblades" in url:
                commercial_texts.append(
                    {"text": text[:50000], "url": url, "document": "valleyblades", "evidence_class": EV_MANUFACTURER}
                )
                commercial_matches.append(
                    {
                        "manufacturer": "Valley Blades Limited",
                        "product_family": "snow plow blades",
                        "part_sku": None,
                        "public_price": None,
                        "quote_required": True,
                        "source_url": url,
                        "evidence_class": EV_MANUFACTURER,
                        "fit_state": MATCH_POSSIBLE,
                        "compliant_with_current_solicitation": False,
                    }
                )
            if "kennametal" in url:
                commercial_texts.append(
                    {"text": text[:50000], "url": url, "document": "kennametal", "evidence_class": EV_MANUFACTURER}
                )
                commercial_matches.append(
                    {
                        "manufacturer": "Kennametal Inc.",
                        "product_family": "carbide wear products",
                        "part_sku": None,
                        "public_price": None,
                        "quote_required": True,
                        "source_url": url,
                        "evidence_class": EV_MANUFACTURER,
                        "fit_state": MATCH_POSSIBLE,
                        "compliant_with_current_solicitation": False,
                    }
                )

            # Historical award-ish patterns (do not invent numbers)
            for am in re.finditer(
                r"(award(?:ed)?[^$\n]{0,40}\$?\s*([0-9,]+\.\d{2}))",
                text,
                re.I,
            ):
                # Only keep if blade-related context nearby
                start = max(0, am.start() - 200)
                ctx = text[start : am.end() + 200].lower()
                if any(t in ctx for t in ("blade", "carbide", "snow", "plow", "2975", "cutting edge")):
                    historical_purchases.append(
                        {
                            "solicitation_number": None,
                            "date": None,
                            "quantities": None,
                            "awarded_vendor": None,
                            "award_amount": float(am.group(2).replace(",", "")),
                            "unit_price": None,
                            "product_manufacturer": None,
                            "source_url": url,
                            "evidence_class": EV_HISTORICAL_AWARD,
                            "confidence": "LOW",
                            "notes": "amount_near_blade_context_unverified_linkage",
                            "authoritative_for_current_bid": False,
                        }
                    )

    # Deduplicate deadline records by role+date+class
    seen_dl = set()
    uniq_deadlines = []
    for r in deadline_records:
        key = (r.get("role"), (r.get("parsed") or {}).get("date"), r.get("evidence_class"), r.get("value"))
        if key in seen_dl:
            continue
        seen_dl.add(key)
        uniq_deadlines.append(r)
    deadline_reconciliation = reconcile_deadline_evidence(uniq_deadlines)

    reconstructed = build_reconstructed_spec_from_public(
        authoritative_text=auth_text,
        commercial_texts=commercial_texts,
    )
    # Separation guard vs current requirement
    separation_ok = assert_reconstructed_not_merged_into_requirement(
        packet.get("requirement") or {}, reconstructed
    )

    # Refine commercial fit states
    req_hints = {
        "title": packet.get("title"),
        "carbide": "tungsten-carbide",
        "lengths": "3 FT and 4 FT sections",
    }
    for m in commercial_matches:
        m["fit_state"] = classify_commercial_match(m, req_hints)
        m["compliant_with_current_solicitation"] = False

    # Quantities from packet
    qty_total = sum((li.get("quantity") or 0) for li in line_items if not li.get("is_service_line"))
    # Only use numeric historical unit prices if we actually found them
    numeric_prices = [p for p in price_evidence if p.get("unit_price") is not None]
    economics = compute_evidence_bounded_economics(
        quantities_by_line=line_items,
        unit_price_evidence=numeric_prices,
        freight_evidence=[],
        total_quantity=qty_total or None,
    )

    ten_k = "UNKNOWN"
    if economics.get("status") == "EVIDENCE_BOUNDED":
        rng = economics.get("minimum_bid_for_10k_profit_range") or {}
        # Plausibility without inventing revenue: if we only have cost side, $10k is
        # "possible if bid clears min range" — mark UNKNOWN unless historical award >> cost
        if historical_purchases and economics.get("estimated_supplier_cost_range"):
            awards = [h["award_amount"] for h in historical_purchases if h.get("award_amount")]
            high_cost = economics["estimated_supplier_cost_range"]["high"]
            if awards and max(awards) - high_cost >= 10000:
                ten_k = "YES"
            elif awards and max(awards) - high_cost < 0:
                ten_k = "NO"
        else:
            ten_k = "UNKNOWN"

    portal = assess_portal_registration(
        deadline_reconciliation=deadline_reconciliation,
        product_id_state=((packet.get("requirement") or {}).get("product_identification") or {}).get(
            "product_id_state"
        ),
        economics=economics,
        authoritative_spec_available=False,
        auth_blocked_critical_doc=True,
        bid_submission_requires_registration=True,  # Jaggaer Respond Now requires account — observed
        supplier_quotes_possible_before_registration=True,
        ten_k_plausible=ten_k,
    )

    # Bid thresholds remain UNKNOWN without cost
    bid_targets = compute_bid_price_targets(
        supplier_cost=(economics.get("estimated_supplier_cost_range") or {}).get("mid")
        if False
        else None,  # do not invent mid as supplier cost
    )

    results = {
        "solicitation_number": solicitation,
        "generated_at": _utc(),
        "run_metadata": complete_run_metadata(run_meta),
        "request_counts": {
            "public_http_search": client.request_count,
            "SAM": 0,
            "USAspending": 0,
            "OpenAI": 0,
            "Paid": 0,
            "supplier_outreach": 0,
            "agency_outreach": 0,
            "lender_outreach": 0,
            "bid_submissions": 0,
        },
        "target_http": target_http,
        "hard_max_http": max_http,
        "search_log": search_log,
        "fetched_pages": fetched_pages,
        "deadline_records": uniq_deadlines,
        "deadline_reconciliation": deadline_reconciliation,
        "agency_item_intelligence": item_intel,
        "historical_purchases": historical_purchases,
        "reconstructed_specification": reconstructed,
        "reconstructed_vs_requirement_separated": separation_ok,
        "commercial_matches": commercial_matches,
        "public_price_evidence": price_evidence,
        "preliminary_economics": economics,
        "ten_k_profit_plausible": ten_k,
        "portal_registration": portal,
        "bid_price_targets": bid_targets,
        "exact_spec_found_publicly": False,
        "historical_iowa_specs_found": False,
        "answers": {},
        "next_state": "PUBLIC_EVIDENCE_RECOVERY_OPERATIONAL",
    }

    results["answers"] = {
        "1_exact_spec_public": False,
        "2_historical_iowa_specs": False,
        "3_item_codes": {
            c: next((i for i in item_intel if i["agency_item_code"] == c), None) for c in BLADE_ITEM_CODES
        },
        "4_likely_manufacturers_products": [
            {"manufacturer": m.get("manufacturer"), "fit_state": m.get("fit_state")} for m in commercial_matches
        ],
        "5_public_or_historical_prices": bool(numeric_prices)
        or any(h.get("unit_price") is not None or h.get("award_amount") is not None for h in historical_purchases),
        "6_legitimate_cost_range": economics.get("status") == "EVIDENCE_BOUNDED",
        "7_ten_k_plausible": ten_k,
        "8_best_supported_deadline": deadline_reconciliation.get("best_supported_close")
        or deadline_reconciliation.get("operational_deadline"),
        "9_deadline_conflict_resolved": bool(deadline_reconciliation.get("conflict_resolved")),
        "10_portal_registration_worthwhile": portal.get("state"),
        "11_exact_evidence_still_missing": [
            "Snow Plow Blade Spec 8Sept20263.pdf (authoritative)",
            "public historical unit prices for item codes",
            "supplier quotes",
            "freight to Ames",
        ],
        "12_next_operator_action": (
            "Treat Close 10/7/2026 1:00 PM CDT as best-supported deadline "
            "(mirrors that say Sep 15 confuse Open with Close). "
            "Register or obtain authorized download of Snow Plow Blade Spec 8Sept20263.pdf, "
            "ingest via CLI, then request supplier quotes."
        ),
    }

    write_public_evidence_artifacts(results, packet)
    return results


def write_public_evidence_artifacts(results: dict[str, Any], packet: dict[str, Any] | None = None) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "public_evidence_recovery_results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    (ARTIFACTS / "reconstructed_specification.json").write_text(
        json.dumps(results.get("reconstructed_specification"), indent=2, default=str), encoding="utf-8"
    )

    # Item intelligence CSV
    with (ARTIFACTS / "agency_item_intelligence.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "agency_item_code",
            "historical_description",
            "known_dimensions",
            "historical_supplier",
            "manufacturer",
            "part_number",
            "historical_quantity",
            "historical_unit_price",
            "award_date",
            "solicitation_contract_reference",
            "source",
            "evidence_class",
            "confidence",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in results.get("agency_item_intelligence") or []:
            w.writerow(row)

    with (ARTIFACTS / "historical_product_purchases.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "solicitation_number",
            "date",
            "quantities",
            "awarded_vendor",
            "award_amount",
            "unit_price",
            "product_manufacturer",
            "source_url",
            "evidence_class",
            "confidence",
            "notes",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in results.get("historical_purchases") or []:
            w.writerow(row)

    with (ARTIFACTS / "public_price_evidence.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["price_class", "unit_price", "date", "source_url", "notes"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in results.get("public_price_evidence") or []:
            w.writerow(row)

    dl = results.get("deadline_reconciliation") or {}
    report = f"""# Public Evidence Recovery Report

Solicitation: {results.get('solicitation_number')}
Generated: {results.get('generated_at')}

## Deadline reconciliation
Status: {dl.get('status')}
Best supported close: {dl.get('best_supported_close')}
Operational deadline: {dl.get('operational_deadline')}
Basis: {dl.get('operational_deadline_basis')}
Conflict resolved: {dl.get('conflict_resolved')}
Notes: {dl.get('resolution_notes')}

## Exact public spec?
{results.get('exact_spec_found_publicly')}

## Preliminary economics
{json.dumps(results.get('preliminary_economics'), indent=2, default=str)}

## Portal registration
{json.dumps(results.get('portal_registration'), indent=2, default=str)}

## Answers
{json.dumps(results.get('answers'), indent=2, default=str)}

## Request counts
{json.dumps(results.get('request_counts'), indent=2)}

NEXT STATE:
PUBLIC_EVIDENCE_RECOVERY_OPERATIONAL
"""
    (ARTIFACTS / "public_evidence_recovery_report.md").write_text(report, encoding="utf-8")

    # Update blade operator packet if present
    if packet and packet.get("solicitation_number"):
        sol = packet["solicitation_number"]
        packet = dict(packet)
        packet["public_evidence_recovery"] = {
            "deadline_reconciliation": dl,
            "portal_registration": results.get("portal_registration"),
            "preliminary_economics": results.get("preliminary_economics"),
            "reconstructed_specification_ref": "artifacts/reconstructed_specification.json",
            "exact_spec_found_publicly": False,
            "updated_at": _utc(),
        }
        packet["operational_deadline"] = dl.get("operational_deadline")
        packet["deadline_status"] = dl.get("status")
        PACKETS.mkdir(parents=True, exist_ok=True)
        (PACKETS / f"{sol}.json").write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")
        md_path = PACKETS / f"{sol}.md"
        extra = f"""

## PUBLIC EVIDENCE / DEADLINE
Deadline status: {dl.get('status')}
Best supported close: {dl.get('best_supported_close')}
Operational deadline (conservative if unresolved): {dl.get('operational_deadline')}
Portal registration: {(results.get('portal_registration') or {}).get('state')}
Exact public spec found: false
$10K plausible (public evidence): {results.get('ten_k_profit_plausible')}
"""
        if md_path.exists():
            md_path.write_text(md_path.read_text(encoding="utf-8") + extra, encoding="utf-8")
        else:
            md_path.write_text(extra, encoding="utf-8")
