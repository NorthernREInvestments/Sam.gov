"""Historical case-study inventory: corpus extraction, ranking, cutoffs, queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from historical_case_constants import (
    BACKTEST_HYPOTHETICAL_ONLY,
    BACKTEST_NOT_SUITABLE,
    BACKTEST_READY,
    BACKTEST_READY_WITH_LIMITATIONS,
    BACKTEST_RESEARCH_REQUIRED,
    CLASS_CONSTRUCTION,
    CLASS_CORE_PRODUCT_RESALE,
    CLASS_GRANT_OR_INNOVATION,
    CLASS_HYPOTHETICAL_EXAMPLE,
    CLASS_OTHER,
    CLASS_PRODUCT_PLUS_INCIDENTAL_SERVICE,
    CLASS_PROMOTIONAL_CLAIM,
    CLASS_SERVICE_DOMINANT,
    CORPUS_NOT_AVAILABLE,
    CORPUS_PARTIAL,
    CUTOFF_HIGH,
    CUTOFF_LOW,
    CUTOFF_MEDIUM,
    CUTOFF_RULE_DESCRIPTION,
    CUTOFF_RULE_ID,
    CUTOFF_UNAVAILABLE,
    LIVE_IOWA_SOLICITATION,
    PRIMARY_BACKTEST_CLASSES,
    QUALITY_INSUFFICIENT,
    QUALITY_LOW,
    QUALITY_MEDIUM,
    STATUS_SOURCE_CLAIM_ONLY,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    TRACE_PARTIALLY,
    TRACE_STRONGLY,
    TRACE_UNTRACEABLE,
    TRACE_WEAKLY,
)
from historical_case_models import (
    UNKNOWN,
    claim_vs_verified,
    future_backtest_metric_scaffold,
    historical_case_lead,
    historical_case_timeline,
    historical_economic_outcome,
    research_quality_summary,
    set_outcome_field,
    simulation_cutoff_record,
    source_claim,
    timeline_event,
)

# Canonical paths relative to govtracker/
CORPUS_DIR = Path(__file__).resolve().parent / "artifacts" / "historical_source_corpus"
KIZZY_CORPUS_FILE = CORPUS_DIR / "kizzy_parks_youtube_transcripts.txt"
KIZZY_DOCX_SOURCE = Path(
    r"C:\Users\M_gra\OneDrive\Documents\Kizzy Parks Youtube Transcripts.docx"
)

MISSING_CORPUS_SOURCES = [
    "GovClose / Ricky transcripts",
    "Government Cheese / Truck & Hustle transcripts",
    "Republic Capital Access transcripts",
    "Other GovCon transcripts previously discussed but not imported",
]


def assess_source_corpus() -> dict[str, Any]:
    kizzy_txt = KIZZY_CORPUS_FILE.exists()
    kizzy_docx = KIZZY_DOCX_SOURCE.exists()
    used: list[str] = []
    if kizzy_txt:
        used.append(str(KIZZY_CORPUS_FILE))
    if kizzy_docx:
        used.append(str(KIZZY_DOCX_SOURCE))

    if not kizzy_txt and not kizzy_docx:
        return {
            "status": CORPUS_NOT_AVAILABLE,
            "transcript_corpus_available": False,
            "files_used": [],
            "missing_required_imports": MISSING_CORPUS_SOURCES
            + ["Dr. Kizzy Parks / Profitable Contracts transcripts"],
            "note": "Do not fabricate cases from memory. Inventory architecture only.",
        }

    return {
        "status": CORPUS_PARTIAL,
        "transcript_corpus_available": True,
        "partial_reason": (
            "Only Dr. Kizzy Parks YouTube transcript material located; "
            "GovClose, Government Cheese, Republic Capital Access, and other "
            "previously discussed creators are SOURCE_CORPUS_NOT_AVAILABLE."
        ),
        "files_used": used,
        "missing_required_imports": MISSING_CORPUS_SOURCES,
        "kizzy_extracted_text_chars": (
            KIZZY_CORPUS_FILE.stat().st_size if kizzy_txt else 0
        ),
    }


def _kizzy_meta() -> dict[str, str]:
    return {
        "source_name": "Dr. Kizzy Parks / Profitable Contracts",
        "source_type": "YOUTUBE_TRANSCRIPT",
        "source_file": str(KIZZY_CORPUS_FILE.name),
        "video_title": (
            "How to Make Money with Middleman Strategy in 2026 The Legal Way"
        ),
        "source_publication_date": UNKNOWN,  # upload date not in extracted text
    }


def extract_case_leads_from_kizzy_corpus() -> list[dict[str, Any]]:
    """Extract distinct SOURCE_CLAIM leads only from available Kizzy transcript text.

    Does not invent awards/agencies. Unknown fields remain UNKNOWN.
    """
    if not KIZZY_CORPUS_FILE.exists():
        return []

    text = KIZZY_CORPUS_FILE.read_text(encoding="utf-8")
    meta = _kizzy_meta()
    leads: list[dict[str, Any]] = []

    def add(
        case_id: str,
        classification: str,
        *,
        speaker: str,
        location: str,
        excerpt: str,
        claimed: dict[str, Any],
        claims: list[tuple[str, Any, str | None]],
        notes: str | None = None,
    ) -> None:
        sc = [
            source_claim(
                field,
                value,
                source_name=meta["source_name"],
                source_file=meta["source_file"],
                source_location=location,
                speaker=speaker,
                excerpt=excerpt[:280],
                notes=cnotes,
            )
            for field, value, cnotes in claims
        ]
        leads.append(
            historical_case_lead(
                case_id=case_id,
                source_name=meta["source_name"],
                source_type=meta["source_type"],
                source_file=meta["source_file"],
                source_location=location,
                speaker=speaker,
                video_title=meta["video_title"],
                source_publication_date=meta["source_publication_date"],
                transcript_excerpt_reference=excerpt[:400],
                case_classification=classification,
                claimed=claimed,
                source_claims=sc,
                notes=notes,
            )
        )

    # 1 — West Michigan machine product reseller (strongest product-resale lead)
    loc = f"char~{text.find('West Michigan')}"
    excerpt = (
        "GovCon winner at airport in West Michigan / Muskegon sells certain types of "
        "machines; machine 50 / 250 / half a mill and he'll 3x it"
    )
    add(
        "HCL-KIZZY-001",
        CLASS_CORE_PRODUCT_RESALE,
        speaker="Dr. Kizzy Parks (describing anonymous operator)",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_product": "certain types of machines (unspecified)",
            "claimed_location": "West Michigan / Muskegon area",
            "claimed_margin": "approximately 3x on machine cost (SOURCE_CLAIM)",
            "claimed_supplier_cost": "example tiers $50k / $250k / $500k",
            "other_identifying_clues": (
                "Anonymous; speaker refused personal identifiers; "
                "operator allegedly focused on work out west"
            ),
        },
        claims=[
            ("claimed_product", "machines (unspecified type)", None),
            ("claimed_margin", "3x markup", "speaker paraphrase of operator claim"),
            ("claimed_location", "West Michigan / Muskegon", None),
        ],
        notes="Primary product-resale anecdote; identity deliberately withheld in source.",
    )

    # 2 — Pluralsight / site license flipping (digital product resale path)
    loc = f"char~{text.find('Pluralsight')}"
    excerpt = (
        "Middleman into hotels and Pluralsight licenses; live opportunities on SAM.gov; "
        "e-learning platform licenses you can flip"
    )
    add(
        "HCL-KIZZY-002",
        CLASS_PRODUCT_PLUS_INCIDENTAL_SERVICE,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_product": "Pluralsight / plural site e-learning licenses",
            "claimed_agency": "federal (SAM.gov opportunities claimed)",
            "other_identifying_clues": "Described as live SAM.gov opportunities at recording time",
        },
        claims=[
            ("claimed_product", "Pluralsight site licenses", "promotional/current opp framing"),
            ("claimed_agency", "unspecified federal via SAM.gov", None),
        ],
        notes="License resale is digital goods; no specific solicitation or award identified in transcript.",
    )

    # 3 — VA fitness app subscription attempt
    loc = f"char~{text.find('fitness app')}"
    excerpt = (
        "Gentleman interviewed bid working with the VA to buy some kind of fitness app subscription"
    )
    add(
        "HCL-KIZZY-003",
        CLASS_SERVICE_DOMINANT,
        speaker="Dr. Kizzy Parks (describing interviewee)",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_agency": "VA (claimed)",
            "claimed_product": "fitness app subscription",
            "other_identifying_clues": "Interviewee unnamed; outcome of bid UNKNOWN",
        },
        claims=[
            ("claimed_agency", "VA", "speaker hedge: 'I think he was working with the VA'"),
            ("claimed_product", "fitness app subscription", None),
        ],
        notes="Software subscription / service-dominant; not tangible product resale.",
    )

    # 4 — Beach packing/removal win (Form 1449) — HB Bronson / Teresa's Treat guest
    loc = f"char~{text.find('nesting season')}"
    excerpt = (
        "Won contract; subcontractor quote ~$20,000 needing $5,000 down; work delayed until "
        "October for nesting season birds on beach; Form 1449 / IP account; HB Bronson Enterprises"
    )
    add(
        "HCL-KIZZY-004",
        CLASS_CONSTRUCTION,
        speaker="Guest (HB Bronson Enterprises / Teresa's Treat)",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_vendor": "HB Bronson Enterprises (DBA reach) / Teresa's Treat niche name",
            "claimed_supplier_cost": "~$20,000 subcontractor quote; $5,000 down",
            "claimed_product": "beach packing / removal work (performance, not goods)",
            "claimed_contract_type": "Form 1449 referenced",
            "claimed_payment_timing": "award letter then IP account setup",
            "claimed_date": "work constrained until October (year UNKNOWN)",
            "other_identifying_clues": (
                "Beach nesting birds; packing removal; CO called about missing form; "
                "email to establish IP account"
            ),
        },
        claims=[
            ("claimed_vendor", "HB Bronson Enterprises", "guest self-identification"),
            ("claimed_supplier_cost", "~$20,000 quote / $5,000 down", "SOURCE_CLAIM amounts"),
            ("claimed_contract_type", "Form 1449", None),
        ],
        notes="Construction/service performance win — useful architecture test, not CORE product queue.",
    )

    # 5 — First GSA schedule win under $3,500 (Dr. Parks)
    loc = f"char~{text.find('first GSA win')}"
    excerpt = (
        "Very first GSA win under $3,500; agency paid by credit card; product/service UNKNOWN in transcript"
    )
    add(
        "HCL-KIZZY-005",
        CLASS_OTHER,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_award_amount": "under $3,500",
            "claimed_contract_type": "GSA schedule order",
            "claimed_payment_timing": "credit card",
            "claimed_vendor": "Dr. Kizzy Parks entity (exact legal name UNKNOWN in transcript)",
            "claimed_product": UNKNOWN,
        },
        claims=[
            ("claimed_award_amount", "under $3,500", "speaker recollection"),
            ("claimed_payment_timing", "credit card", None),
        ],
        notes="Award claimed but product not stated — cannot classify as CORE_PRODUCT_RESALE.",
    )

    # 6 — Hypothetical big-ticket $200k–$500k product
    loc = f"char~{text.find('half a million dollar')}"
    excerpt = "Want a big ticket product — half a million / $200,000 thing like that (advice, not a named deal)"
    add(
        "HCL-KIZZY-006",
        CLASS_HYPOTHETICAL_EXAMPLE,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_award_amount": "$200,000–$500,000 (hypothetical sizing advice)",
            "claimed_product": "unspecified big-ticket product",
        },
        claims=[
            ("claimed_award_amount", "$200k–$500k", "hypothetical teaching example"),
        ],
        notes="Educational sizing guidance, not a historical transaction.",
    )

    # 7 — Puerto Rico hotels staffing
    loc = f"char~{text.find('hotels staffing in Puerto Rico')}"
    excerpt = "Opportunities for hotels staffing in Puerto Rico"
    add(
        "HCL-KIZZY-007",
        CLASS_SERVICE_DOMINANT,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_product": "hotels staffing",
            "claimed_location": "Puerto Rico",
        },
        claims=[("claimed_location", "Puerto Rico", "promotional opportunity mention")],
        notes="Staffing service opportunity mention.",
    )

    # 8 — Navy staffing
    loc = f"char~{text.find('staffing for the Navy')}"
    excerpt = "Staffing for the Navy"
    add(
        "HCL-KIZZY-008",
        CLASS_SERVICE_DOMINANT,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={"claimed_agency": "Navy", "claimed_product": "staffing"},
        claims=[("claimed_agency", "Navy", "opportunity category mention")],
        notes="Service staffing category.",
    )

    # 9 — Religious staffing Hawaii
    loc = f"char~{text.find('religious staffing in Hawaii')}"
    excerpt = "Religious staffing in Hawaii"
    add(
        "HCL-KIZZY-009",
        CLASS_SERVICE_DOMINANT,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={"claimed_location": "Hawaii", "claimed_product": "religious staffing"},
        claims=[("claimed_location", "Hawaii", None)],
        notes="Service staffing category.",
    )

    # 10 — Minneapolis $5M contract networking example (daycare context)
    loc = f"char~{text.find('contract for $5 million')}"
    excerpt = (
        "Illustrative outreach: 'I saw you won this contract for $5 million for blah blah blah'"
    )
    add(
        "HCL-KIZZY-010",
        CLASS_PROMOTIONAL_CLAIM,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_award_amount": "$5 million",
            "claimed_location": "Minneapolis context (daycare enforcement aside)",
            "other_identifying_clues": "Placeholder example for networking script, not a named award",
        },
        claims=[
            ("claimed_award_amount", "$5 million", "scripted example — not asserted as specific award"),
        ],
        notes="Promotional/networking script; not an identifiable procurement.",
    )

    # 11 — Guest copied prior bidder with claimed ~$10M profit mode
    loc = f"char~{text.find('profit was like $10,000,000')}"
    excerpt = (
        "Referenced someone whose profit was like $10,000,000; guest copied mode; "
        "bid was for actually 30 out of it and we won"
    )
    add(
        "HCL-KIZZY-011",
        CLASS_PROMOTIONAL_CLAIM,
        speaker="Guest + Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_profit": "$10,000,000 (referenced third party — ambiguous)",
            "claimed_quantity": "30 (unit UNKNOWN)",
            "other_identifying_clues": "Ambiguous profit vs award; product UNKNOWN",
        },
        claims=[
            ("claimed_profit", "$10,000,000", "third-party reference; do not treat as verified"),
            ("claimed_quantity", "30", "unit unknown"),
        ],
        notes="Ambiguous promotional economics; product/agency unidentified.",
    )

    # 12 — SBA $1M grant / Clover partnership
    loc = f"char~{text.find('SBA Grant')}"
    if loc == "char~-1":
        loc = f"char~{text.find('$1 million')}"
    excerpt = "SBA $1,000,000 grant partnership with Clover; eligibility revenue >= $100,000"
    add(
        "HCL-KIZZY-012",
        CLASS_GRANT_OR_INNOVATION,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_agency": "SBA",
            "claimed_award_amount": "$1,000,000 grant pool / program",
            "claimed_product": "grant — not product resale",
        },
        claims=[("claimed_agency", "SBA", None), ("claimed_award_amount", "$1,000,000", "grant program")],
        notes="Grant/innovation — excluded from product-resale backtest queue.",
    )

    # 13 — Fashion design $25k grant hypothetical
    loc = f"char~{text.find('$25,000 grant')}"
    excerpt = "Win a $25,000 grant and then what — fashion design person needing more grants"
    add(
        "HCL-KIZZY-013",
        CLASS_GRANT_OR_INNOVATION,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_award_amount": "$25,000 grant (illustrative)",
            "claimed_product": "fashion design grant use-case",
        },
        claims=[("claimed_award_amount", "$25,000", "illustrative grant")],
        notes="Grant illustration.",
    )

    # 14 — HVAC franchise mention (not a gov transaction)
    loc = f"char~{text.find('buy an HVAC')}"
    excerpt = "Talking about buying an HVAC franchise / dirty business — not a specific gov buy"
    add(
        "HCL-KIZZY-014",
        CLASS_HYPOTHETICAL_EXAMPLE,
        speaker="Dr. Kizzy Parks",
        location=loc,
        excerpt=excerpt,
        claimed={"claimed_product": "HVAC business acquisition (non-gov example)"},
        claims=[("claimed_product", "HVAC franchise", "business acquisition analogy")],
        notes="Not a government transaction.",
    )

    # 15 — Educational middleman product-resale legal framing (no specific deal)
    loc = f"char~{text.find('resale of products')}"
    excerpt = (
        "Middleman approach as resale of products; prime selling somebody else's product; "
        "not a manufacturer — legal/compliance teaching"
    )
    add(
        "HCL-KIZZY-015",
        CLASS_HYPOTHETICAL_EXAMPLE,
        speaker="Guest counsel / Dr. Kizzy Parks segment",
        location=loc,
        excerpt=excerpt,
        claimed={
            "claimed_product": "generic product resale middleman model",
            "other_identifying_clues": "Legal limitations on set-asides / subcontracting percentages discussed",
        },
        claims=[
            ("claimed_product", "generic resale products", "educational model, not a case"),
        ],
        notes="Architecture-relevant teaching content; not a historical award.",
    )

    return leads


def project_known_product_lead_iowa_prior() -> dict[str, Any]:
    """Prior Iowa DOT carbide blades RFB from project public-evidence artifacts.

    NOT from the transcript corpus. Used for temporal-firewall validation only.
    Must NOT contaminate live 645-DOTRFB-2975-2027.
    """
    return historical_case_lead(
        case_id="HCL-PROJECT-IOWA-2907",
        source_name="Project public evidence artifact (govcb third-party mirror)",
        source_type="PROJECT_PUBLIC_EVIDENCE_ARTIFACT",
        source_file="artifacts/historical_product_purchases.csv",
        source_location="row:645-DOTRFB-2907-2027",
        speaker=None,
        video_title=None,
        source_publication_date="2026-07",  # approximate from artifact notes
        transcript_excerpt_reference=None,
        case_classification=CLASS_CORE_PRODUCT_RESALE,
        claimed={
            "claimed_agency": "Iowa DOT",
            "claimed_product": "Carbide blades for snow/ice removal",
            "claimed_solicitation_number": "645-DOTRFB-2907-2027",
            "claimed_date": "2026-07 (mirror-reported open/close; exact UNKNOWN)",
            "other_identifying_clues": (
                "Related prior Iowa DOT carbide blades RFB; same buyer contact family as live "
                f"{LIVE_IOWA_SOLICITATION} but DISTINCT solicitation — validation subject only"
            ),
        },
        source_claims=[
            source_claim(
                "claimed_solicitation_number",
                "645-DOTRFB-2907-2027",
                source_name="historical_product_purchases.csv",
                source_file="artifacts/historical_product_purchases.csv",
                notes="Third-party bid mirror evidence_class; award/unit prices NOT found",
            ),
        ],
        notes=(
            "PROJECT artifact lead for firewall validation. "
            "Not a transcript case. Not the live Iowa opportunity."
        ),
    )


def classify_for_primary_queue(lead: dict[str, Any]) -> bool:
    return lead.get("case_classification") in PRIMARY_BACKTEST_CLASSES


def rank_traceability_clues(lead: dict[str, Any]) -> int:
    """Higher = more identifying clues for public research priority (not a probability)."""
    score = 0
    for key in (
        "claimed_solicitation_number",
        "claimed_contract_number",
        "claimed_award_number",
        "claimed_vendor",
        "claimed_agency",
        "claimed_product",
        "claimed_award_amount",
        "claimed_date",
        "claimed_location",
        "claimed_quantity",
    ):
        val = lead.get(key)
        if val not in (None, "", UNKNOWN):
            score += 2 if "number" in key or key == "claimed_vendor" else 1
    if lead.get("case_classification") in PRIMARY_BACKTEST_CLASSES:
        score += 5
    return score


def assess_traceability(
    lead: dict[str, Any],
    *,
    public_research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pr = public_research or {}
    solicitation_id = pr.get("solicitation_identified") or (
        lead.get("claimed_solicitation_number") not in (None, "", UNKNOWN)
    )
    agency_id = pr.get("agency_identified") or (
        lead.get("claimed_agency") not in (None, "", UNKNOWN)
    )
    product_id = lead.get("claimed_product") not in (None, "", UNKNOWN)
    deadline_id = bool(pr.get("bid_deadline_identified"))
    award_verified = bool(pr.get("award_independently_verified"))
    awardee_verified = bool(pr.get("awardee_independently_verified"))
    amount_verified = bool(pr.get("award_amount_independently_verified"))
    prebid_docs = bool(pr.get("pre_bid_documents_available"))
    public_docs = bool(pr.get("historical_public_documents_available"))

    reasons: list[str] = []
    if solicitation_id:
        reasons.append("solicitation identifiable")
    else:
        reasons.append("solicitation not identifiable")
    if agency_id:
        reasons.append("agency identifiable")
    else:
        reasons.append("agency not identifiable")
    if product_id:
        reasons.append("product identifiable at claim level")
    if award_verified:
        reasons.append("award independently verified")
    if awardee_verified:
        reasons.append("awardee independently verified")
    if amount_verified:
        reasons.append("award amount independently verified")
    if prebid_docs:
        reasons.append("pre-bid documents available")
    if deadline_id:
        reasons.append("bid deadline identifiable")

    if (
        solicitation_id
        and agency_id
        and product_id
        and award_verified
        and awardee_verified
        and prebid_docs
        and deadline_id
    ):
        state = TRACE_STRONGLY  # FULLY requires more complete field set; we avoid overclaiming
        if amount_verified and public_docs:
            from historical_case_constants import TRACE_FULLY

            state = TRACE_FULLY
    elif solicitation_id and agency_id and product_id and (award_verified or prebid_docs):
        state = TRACE_PARTIALLY
    elif (agency_id or solicitation_id) and product_id:
        state = TRACE_WEAKLY
    elif product_id and lead.get("case_classification") in PRIMARY_BACKTEST_CLASSES:
        state = TRACE_WEAKLY
        reasons.append("product-resale claim only; transaction not distinguished from similar buys")
    else:
        state = TRACE_UNTRACEABLE
        reasons.append("insufficient identifying information")

    return {
        "traceability_state": state,
        "reasons": reasons,
        "factors": {
            "solicitation_identifiable": bool(solicitation_id),
            "agency_identifiable": bool(agency_id),
            "product_identifiable": bool(product_id),
            "bid_deadline_identifiable": deadline_id,
            "award_independently_verified": award_verified,
            "awardee_independently_verified": awardee_verified,
            "award_amount_independently_verified": amount_verified,
            "historical_public_documents_available": public_docs,
            "pre_bid_documents_available": prebid_docs,
        },
    }


def assess_backtest_suitability(
    lead: dict[str, Any],
    trace: dict[str, Any],
    cutoff: dict[str, Any],
    *,
    has_recoverable_prebid: bool = False,
    has_outcome_evidence: bool = False,
) -> dict[str, Any]:
    classification = lead.get("case_classification")
    if classification == CLASS_HYPOTHETICAL_EXAMPLE:
        return {
            "backtest_suitability": BACKTEST_HYPOTHETICAL_ONLY,
            "reason": "Hypothetical teaching example — excluded from primary queue",
        }
    if classification in (
        CLASS_SERVICE_DOMINANT,
        CLASS_CONSTRUCTION,
        CLASS_GRANT_OR_INNOVATION,
        CLASS_PROMOTIONAL_CLAIM,
        CLASS_OTHER,
    ):
        if classification not in PRIMARY_BACKTEST_CLASSES:
            return {
                "backtest_suitability": BACKTEST_NOT_SUITABLE,
                "reason": f"Classification {classification} not core product-resale backtest target",
            }

    tstate = trace.get("traceability_state")
    cconf = cutoff.get("confidence")

    if (
        classification in PRIMARY_BACKTEST_CLASSES
        and tstate in (TRACE_STRONGLY, "FULLY_TRACEABLE")
        and cconf in (CUTOFF_HIGH, CUTOFF_MEDIUM)
        and has_recoverable_prebid
        and has_outcome_evidence
    ):
        return {
            "backtest_suitability": BACKTEST_READY,
            "reason": (
                "Real procurement identifiable, product-resale fit, defensible cutoff, "
                "pre-bid + outcome evidence with temporal separation"
            ),
        }

    if (
        classification in PRIMARY_BACKTEST_CLASSES
        and tstate in (TRACE_PARTIALLY, TRACE_STRONGLY, "FULLY_TRACEABLE")
        and cconf != CUTOFF_UNAVAILABLE
        and has_recoverable_prebid
    ):
        return {
            "backtest_suitability": BACKTEST_READY_WITH_LIMITATIONS,
            "reason": (
                "Product-resale fit with partial traceability/cutoff; "
                "supplier cost may be unknown (useful for supplier-research tests)"
            ),
        }

    if classification in PRIMARY_BACKTEST_CLASSES:
        return {
            "backtest_suitability": BACKTEST_RESEARCH_REQUIRED,
            "reason": "Product-resale lead lacks independent procurement identification or cutoff",
        }

    return {
        "backtest_suitability": BACKTEST_NOT_SUITABLE,
        "reason": "Does not meet product-resale backtest entry criteria",
    }


def select_simulation_cutoff(
    *,
    posting_date: Any = None,
    bid_deadline: Any = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Reproducible cutoff rule — does not prove actual discovery timing."""
    from temporal_evidence_firewall import parse_iso_datetime

    posting = parse_iso_datetime(posting_date)
    deadline = parse_iso_datetime(bid_deadline)
    if posting is None or deadline is None:
        return simulation_cutoff_record(
            simulation_as_of=None,
            confidence=CUTOFF_UNAVAILABLE,
            reason="posting date and/or bid deadline UNKNOWN — cannot set defensible cutoff",
            source_evidence=evidence_refs,
            rule_id=CUTOFF_RULE_ID,
        )
    if deadline <= posting:
        return simulation_cutoff_record(
            simulation_as_of=None,
            confidence=CUTOFF_UNAVAILABLE,
            reason="deadline not after posting — timeline conflict",
            source_evidence=evidence_refs,
            rule_id=CUTOFF_RULE_ID,
        )

    delta = deadline - posting
    midpoint = posting + delta / 2
    floor = posting + timedelta(days=1)
    as_of = max(midpoint, floor)
    if as_of >= deadline:
        as_of = posting + (deadline - posting) / 3  # fall back earlier third
        conf = CUTOFF_LOW
        reason = (
            f"{CUTOFF_RULE_DESCRIPTION} Short window forced earlier-third fallback."
        )
    else:
        # Confidence by window length
        days = delta.total_seconds() / 86400
        if days >= 14:
            conf = CUTOFF_HIGH
        elif days >= 5:
            conf = CUTOFF_MEDIUM
        else:
            conf = CUTOFF_LOW
        reason = CUTOFF_RULE_DESCRIPTION

    time_before = str(deadline - as_of)
    return simulation_cutoff_record(
        simulation_as_of=as_of.astimezone(timezone.utc),
        confidence=conf,
        reason=reason,
        source_evidence=evidence_refs,
        time_before_deadline=time_before,
        rule_id=CUTOFF_RULE_ID,
    )


def build_case_record(
    lead: dict[str, Any],
    *,
    public_research: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    cutoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pr = public_research or {
        "solicitation_identified": False,
        "agency_identified": False,
        "bid_deadline_identified": False,
        "award_independently_verified": False,
        "awardee_independently_verified": False,
        "award_amount_independently_verified": False,
        "pre_bid_documents_available": False,
        "historical_public_documents_available": False,
        "notes": "No public research applied",
        "http_requests": 0,
    }
    tl = timeline or historical_case_timeline(lead["case_id"])
    oc = outcome or historical_economic_outcome(lead["case_id"])

    # Apply SOURCE_CLAIM profit carefully — never as verified
    for sc in lead.get("source_claims") or []:
        if sc.get("field") == "claimed_profit" and sc.get("value") not in (None, "", UNKNOWN):
            set_outcome_field(
                oc,
                "historical_actual_profit",
                sc["value"],
                STATUS_SOURCE_CLAIM_ONLY,
            )
        if sc.get("field") == "claimed_award_amount" and sc.get("value") not in (
            None,
            "",
            UNKNOWN,
        ):
            # Keep as source claim on outcome until verified
            if oc["fields"]["historical_award_amount"]["status"] == STATUS_UNKNOWN:
                set_outcome_field(
                    oc,
                    "historical_award_amount",
                    sc["value"],
                    STATUS_SOURCE_CLAIM_ONLY,
                )
        if sc.get("field") == "claimed_supplier_cost":
            # Supplier cost from transcript remains unknown for verified economics
            if oc["fields"]["historical_supplier_cost"]["status"] == STATUS_UNKNOWN:
                set_outcome_field(
                    oc,
                    "historical_supplier_cost",
                    UNKNOWN,
                    STATUS_UNKNOWN,
                )

    posting = None
    deadline = None
    events = tl.get("events") or {}
    if isinstance(events, dict):
        posting = (events.get("solicitation_posting") or {}).get("event_date")
        deadline = (events.get("bid_deadline") or {}).get("event_date")
        if posting == UNKNOWN:
            posting = None
        if deadline == UNKNOWN:
            deadline = None

    cut = cutoff or select_simulation_cutoff(
        posting_date=posting,
        bid_deadline=deadline,
        evidence_refs=pr.get("evidence_urls") or [],
    )
    trace = assess_traceability(lead, public_research=pr)
    suit = assess_backtest_suitability(
        lead,
        trace,
        cut,
        has_recoverable_prebid=bool(pr.get("pre_bid_documents_available")),
        has_outcome_evidence=bool(pr.get("award_independently_verified")),
    )

    # Research quality dimensions
    if trace["traceability_state"] in ("FULLY_TRACEABLE", TRACE_STRONGLY):
        identity_q = QUALITY_MEDIUM if trace["traceability_state"] == TRACE_STRONGLY else "HIGH"
    elif trace["traceability_state"] == TRACE_PARTIALLY:
        identity_q = QUALITY_MEDIUM
    elif trace["traceability_state"] == TRACE_WEAKLY:
        identity_q = QUALITY_LOW
    else:
        identity_q = QUALITY_INSUFFICIENT

    timeline_known = sum(
        1
        for ev in (events.values() if isinstance(events, dict) else [])
        if isinstance(ev, dict) and ev.get("event_date") not in (None, "", UNKNOWN)
    )
    if timeline_known >= 3:
        timeline_q = QUALITY_MEDIUM
    elif timeline_known >= 1:
        timeline_q = QUALITY_LOW
    else:
        timeline_q = QUALITY_INSUFFICIENT

    rq = research_quality_summary(
        identity=identity_q,
        timeline=timeline_q,
        pre_bid_evidence=(
            QUALITY_MEDIUM if pr.get("pre_bid_documents_available") else QUALITY_INSUFFICIENT
        ),
        outcome_evidence=(
            QUALITY_MEDIUM if pr.get("award_independently_verified") else QUALITY_INSUFFICIENT
        ),
        economic_reconstruction=QUALITY_INSUFFICIENT,
    )

    # Profit claim vs verified illustration
    profit_claim = None
    for sc in lead.get("source_claims") or []:
        if sc.get("field") == "claimed_profit":
            profit_claim = sc
            break
    profit_resolution = claim_vs_verified(source_claim_record=profit_claim, verified=None)

    return {
        "lead": lead,
        "timeline": tl,
        "economic_outcome": oc,
        "public_research": pr,
        "simulation_cutoff": cut,
        "traceability": trace,
        "backtest_suitability": suit,
        "research_quality": rq,
        "profit_claim_resolution": profit_resolution,
        "future_backtest_metrics": future_backtest_metric_scaffold(),
        "primary_queue_eligible": classify_for_primary_queue(lead)
        and suit["backtest_suitability"]
        not in (BACKTEST_HYPOTHETICAL_ONLY, BACKTEST_NOT_SUITABLE),
        "traceability_rank_score": rank_traceability_clues(lead),
    }


def build_inventory() -> dict[str, Any]:
    corpus = assess_source_corpus()
    leads = extract_case_leads_from_kizzy_corpus() if corpus["transcript_corpus_available"] else []
    # Always attach project prior Iowa 2907 as separate non-corpus validation lead
    project_lead = project_known_product_lead_iowa_prior()
    return {
        "source_corpus": corpus,
        "leads": leads,
        "project_validation_leads": [project_lead],
        "counts": {
            "transcript_leads": len(leads),
            "product_resale_leads": sum(
                1 for L in leads if L["case_classification"] in PRIMARY_BACKTEST_CLASSES
            ),
            "project_validation_leads": 1,
        },
    }


def verify_live_iowa_isolation(artifacts_root: Path) -> dict[str, Any]:
    """Confirm live Iowa packet exists and is not under historical_simulation namespace."""
    live_json = (
        artifacts_root
        / "transactional_procurement_packets"
        / f"{LIVE_IOWA_SOLICITATION}.json"
    )
    hist_dir = artifacts_root / "historical_simulation"
    contaminated = False
    notes: list[str] = []
    if live_json.exists():
        notes.append(f"live packet present: {live_json.name}")
        text = live_json.read_text(encoding="utf-8", errors="replace")
        if "HISTORICAL_SIMULATION" in text and "HCL-KIZZY" in text:
            contaminated = True
            notes.append("unexpected historical case ids found in live packet")
    else:
        notes.append("live packet missing — isolation check incomplete")

    hist_touching_live = False
    if hist_dir.exists():
        for p in hist_dir.rglob("*"):
            if p.is_file() and LIVE_IOWA_SOLICITATION in p.name:
                hist_touching_live = True
                notes.append(f"historical artifact named with live sol: {p}")

    return {
        "live_solicitation": LIVE_IOWA_SOLICITATION,
        "live_packet_exists": live_json.exists(),
        "historical_artifacts_isolated": not hist_touching_live,
        "live_packet_contaminated": contaminated,
        "isolation_ok": live_json.exists() and not contaminated and not hist_touching_live,
        "notes": notes,
        "clock_expectation": "SYSTEM for live; do not run this solicitation through historical simulation",
    }
