"""Persist canonical opportunities + sightings with Postgres-first routing."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from discovery.classify import classify_discovery_opportunity, early_reject_reasons
from discovery.deadline import normalize_deadline
from discovery.dedup import (
    description_fingerprint,
    prefer_official_source,
    strong_canonical_key,
    title_fingerprint,
)
from discovery.priority import compute_research_priority
from discovery.schema import CanonicalOpportunity


def upsert_canonical_opportunity(
    session: Any,
    opp: CanonicalOpportunity,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Database-first: reuse existing canonical + retain sighting.
    Official (lower tier) preferred when available.
    """
    from models import DiscoveredOpportunity, OpportunitySighting

    key = strong_canonical_key(opp)
    if not key:
        key = f"src:{opp.source_id}|{opp.external_id}"

    classification = classify_discovery_opportunity(
        title=opp.title,
        description=opp.description,
        status=opp.status,
    )
    dl = normalize_deadline(
        opp.deadline_raw or (opp.response_deadline.isoformat() if opp.response_deadline else None),
        timezone_hint=opp.deadline_timezone,
        timezone_explicit=opp.deadline_tz_confidence == "KNOWN",
    )
    reject = early_reject_reasons(
        title=opp.title,
        description=opp.description,
        status=opp.status,
        deadline_passed=bool(dl.get("deadline_passed")),
        classification=classification["classification"],
        estimated_value=opp.estimated_value,
        estimated_value_status=opp.estimated_value_status,
    )
    docs = list(opp.document_links or [])
    priority = compute_research_priority(
        classification=classification["classification"],
        estimated_value=opp.estimated_value,
        estimated_value_status=opp.estimated_value_status,
        trust_tier=opp.trust_tier,
        document_count=len(docs),
        reject=reject["reject"],
    )

    interesting = None
    if classification["classification"] == "CORE_PRODUCT":
        interesting = "Core product signals in title/description"
    elif classification["classification"] == "PRODUCT_PLUS_SERVICE":
        interesting = "Product with installation/service component"

    blocker = "; ".join(reject["reasons"]) if reject["reject"] else None

    existing = session.query(DiscoveredOpportunity).filter_by(canonical_key=key).first()
    is_new = existing is None
    is_dup_sighting = False

    if dry_run:
        return {
            "canonical_key": key,
            "is_new": is_new,
            "classification": classification["classification"],
            "research_priority": priority["research_priority"],
            "reject": reject["reject"],
            "dry_run": True,
            "LIVE_API_REQUESTS": 0,
        }

    if existing is None:
        existing = DiscoveredOpportunity(
            canonical_key=key,
            external_id=opp.external_id,
            preferred_source_id=opp.source_id,
            preferred_source_url=opp.source_url or opp.detail_url,
            detail_url=opp.detail_url,
            title=opp.title[:1024],
            solicitation_number=opp.solicitation_number,
            agency=opp.agency,
            subagency=opp.subagency,
            jurisdiction=opp.jurisdiction,
            buyer_type=opp.buyer_type,
            state_code=opp.state_code,
            city=opp.city,
            posted_date=opp.posted_date,
            response_deadline=opp.response_deadline,
            deadline_raw=opp.deadline_raw or dl.get("deadline_raw"),
            deadline_timezone=dl.get("timezone"),
            deadline_tz_confidence=dl.get("timezone_confidence"),
            status=opp.status or "OPEN",
            procurement_method=opp.procurement_method,
            set_aside=opp.set_aside,
            naics=opp.naics,
            psc=opp.psc,
            commodity_codes_json=opp.commodity_codes or [],
            description=opp.description,
            estimated_value=opp.estimated_value,
            estimated_value_status=opp.estimated_value_status,
            contact_json=opp.contact or {},
            document_links_json=docs,
            amendment_links_json=opp.amendment_links or [],
            qa_links_json=opp.qa_links or [],
            submission_method=opp.submission_method,
            product_classification=classification["classification"],
            research_priority=priority["research_priority"],
            research_priority_label=priority["research_priority_label"],
            interesting_reason=interesting,
            obvious_blocker=blocker,
            operator_status="REJECTED" if reject["reject"] else "NEW",
            trust_tier=opp.trust_tier,
            title_fingerprint=title_fingerprint(opp.title),
            description_fingerprint=description_fingerprint(opp.description),
            raw_metadata_json=opp.raw_metadata,
            source_updated_at=opp.source_updated_at,
        )
        session.add(existing)
        session.flush()
    else:
        # Update last_seen; prefer official source
        existing.last_seen_at = now_utc()
        if prefer_official_source(
            existing.trust_tier or 99,
            existing.preferred_source_id,
            opp.trust_tier,
            opp.source_id,
        ):
            existing.preferred_source_id = opp.source_id
            existing.preferred_source_url = opp.detail_url or opp.source_url
            existing.trust_tier = opp.trust_tier
            if opp.detail_url:
                existing.detail_url = opp.detail_url
        # Merge document links
        prev_docs = list(existing.document_links_json or [])
        urls = {d.get("url") for d in prev_docs if isinstance(d, dict)}
        for d in docs:
            if isinstance(d, dict) and d.get("url") and d["url"] not in urls:
                prev_docs.append(d)
        existing.document_links_json = prev_docs
        if opp.amendment_links:
            prev_a = list(existing.amendment_links_json or [])
            aurls = {d.get("url") for d in prev_a if isinstance(d, dict)}
            for d in opp.amendment_links:
                if isinstance(d, dict) and d.get("url") and d["url"] not in aurls:
                    prev_a.append(d)
            existing.amendment_links_json = prev_a
        # Refresh classification if still NEW
        if existing.operator_status == "NEW":
            existing.product_classification = classification["classification"]
            existing.research_priority = priority["research_priority"]
            existing.research_priority_label = priority["research_priority_label"]
            existing.interesting_reason = interesting
            existing.obvious_blocker = blocker
            if reject["reject"]:
                existing.operator_status = "REJECTED"

    # Sighting (always retain)
    sighting = (
        session.query(OpportunitySighting)
        .filter_by(source_id=opp.source_id, external_id=opp.external_id)
        .first()
    )
    if sighting is None:
        sighting = OpportunitySighting(
            discovered_opportunity_id=existing.id,
            source_id=opp.source_id,
            external_id=opp.external_id,
            source_url=opp.source_url or opp.detail_url,
            trust_tier=opp.trust_tier,
            is_preferred=(existing.preferred_source_id == opp.source_id),
            raw_payload_json=opp.raw_metadata,
            content_hash=opp.content_hash,
        )
        session.add(sighting)
    else:
        is_dup_sighting = True
        sighting.last_seen_at = now_utc()
        sighting.is_preferred = existing.preferred_source_id == opp.source_id
        if opp.raw_metadata:
            sighting.raw_payload_json = opp.raw_metadata

    session.flush()
    return {
        "id": existing.id,
        "canonical_key": key,
        "is_new": is_new,
        "is_duplicate_sighting": is_dup_sighting,
        "classification": existing.product_classification,
        "research_priority": existing.research_priority,
        "operator_status": existing.operator_status,
        "preferred_source_id": existing.preferred_source_id,
        "document_count": len(existing.document_links_json or []),
        "amendment_count": len(existing.amendment_links_json or []),
        "LIVE_API_REQUESTS": 0,
    }
