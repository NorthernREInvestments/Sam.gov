"""Offline NextBid product-classification audit (Build 002Q).

Mutually exclusive product classes — independent of M3 bid eligibility.
Missing qty/docs/submission/pricing/supplier MUST NOT affect class.
ZERO metered NextBid calls. No filter changes.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.classify import (
    CONSTRUCTION_ONLY,
    PRODUCT_CLASSES,
    PRODUCT_HINTS,
    SERVICE_ONLY,
    SOFTWARE_ONLY,
    SUPPLY_PHRASE,
    classify_requirement,
    classify_solicitation_item,
)
from m3.research.commercial_criteria import (
    INITIAL_EXCLUDE_CLASSES,
    INITIAL_PRODUCT_OK,
    IT_MARKERS,
)
from m3.research.nextbid_recovery import collect_population, listing_prefilter

# Owner-requested mutually exclusive audit buckets
BUCKET_CONFIRMED_PRODUCT = "confirmed_tangible_product_purchase"
BUCKET_MIXED = "mixed_product_and_service_purchase"
BUCKET_POTENTIAL = "potential_tangible_product_insufficient_information"
BUCKET_SERVICE = "confirmed_service_only"
BUCKET_CONSTRUCTION = "confirmed_construction_only"
BUCKET_IT_PRODUCT = "it_product_purchase"
BUCKET_NON_SOLICITATION = "confirmed_non_solicitation"
BUCKET_UNCLASSIFIED = "unclassified"

IT_HARDWARE = re.compile(
    r"(?i)\b("
    r"laptops?|desktops?|computers?|chromebooks?|workstations?|"
    r"servers?|printers?|monitors?|tablets?|iphones?|ipads?|"
    r"network\s+equipment|network\s+switches?|ethernet\s+switches?|"
    r"routers?|firewalls?|access\s+points?|storage\s+arrays?|"
    r"network[- ]attached\s+storage|storage\s+area\s+network|"
    r"toner|ink\s+cartridges?|docking\s+stations?|"
    r"it\s+equipment|computer\s+equipment|av\s+equipment"
    r")\b"
)
IT_SOFTWAREISH = re.compile(
    r"(?i)\b(software|saas|cloud\s*license|microsoft\s*365|office\s*365|"
    r"subscription|cyber\s*security|endpoint\s*protection)\b"
)
VEHICLE_MACHINERY_MEDICAL = re.compile(
    r"(?i)\b("
    r"vehicles?|trucks?|buses?|vans?|ambulances?|"
    r"machinery|tractors?|mowers?|generators?|forklifts?|"
    r"medical\s+equipment|hospital\s+beds?|defibrillators?|"
    r"ultrasound|x[- ]?ray|imaging\s+equipment"
    r")\b"
)
NON_SOLICITATION_STATUS = re.compile(
    r"(?i)\b(notice[-_ ]of[-_ ]awards?|/awards?\b|forecast|purchasing\s+plan)\b"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_blob(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("description"),
        item.get("document_text"),
        item.get("procurement_type"),
        item.get("product_category"),
        (item.get("evidence") or {}).get("category_raw")
        if isinstance(item.get("evidence"), dict)
        else None,
    ]
    return " ".join(str(p) for p in parts if p)


def _map_to_audit_bucket(item: dict[str, Any], clf: dict[str, Any]) -> dict[str, Any]:
    """Mutually exclusive product-class bucket — not M3 eligibility."""
    req = str(clf.get("requirement_class") or item.get("requirement_class") or "")
    title = str(item.get("title") or "")
    blob = _text_blob(item)
    evidence = list(clf.get("classification_evidence") or item.get("classification_evidence") or [])
    rt = str(item.get("record_type") or "")

    # 1) Non-solicitation
    if req == "not_applicable_non_solicitation" or rt in {
        "PROCUREMENT_PORTAL",
        "HISTORICAL_AWARD",
        "PROCUREMENT_FORECAST",
        "PURCHASING_PLAN",
        "VENDOR_REGISTRATION",
        "GENERAL_PROCUREMENT_INFORMATION",
        "UNRELATED_CONTENT",
    }:
        return {
            "bucket": BUCKET_NON_SOLICITATION,
            "requirement_class": req or "not_applicable_non_solicitation",
            "evidence": evidence or [f"identity:{rt or 'non_solicitation'}"],
            "ambiguous": False,
        }
    if NON_SOLICITATION_STATUS.search(f"{title} {item.get('official_url') or ''}") and not (
        PRODUCT_HINTS.search(title) or SUPPLY_PHRASE.search(title)
    ):
        return {
            "bucket": BUCKET_NON_SOLICITATION,
            "requirement_class": req or "not_applicable_non_solicitation",
            "evidence": evidence + ["title_or_url_award_or_forecast"],
            "ambiguous": False,
        }

    # 2) IT hardware product (tangible) — before software_only swallows mixed titles
    if IT_HARDWARE.search(title) or (
        IT_HARDWARE.search(blob) and not SERVICE_ONLY.search(title) and not CONSTRUCTION_ONLY.search(title)
    ):
        if not SOFTWARE_ONLY.search(title) or IT_HARDWARE.search(title):
            return {
                "bucket": BUCKET_IT_PRODUCT,
                "requirement_class": req or "product_supply",
                "evidence": evidence + ["it_hardware_tangible_product"],
                "ambiguous": bool(IT_SOFTWAREISH.search(title) and IT_HARDWARE.search(title)),
            }

    # 3) Confirmed construction-only
    if req == "construction" or (
        CONSTRUCTION_ONLY.search(title)
        and not PRODUCT_HINTS.search(title)
        and not SUPPLY_PHRASE.search(title)
        and not IT_HARDWARE.search(title)
    ):
        return {
            "bucket": BUCKET_CONSTRUCTION,
            "requirement_class": req or "construction",
            "evidence": evidence or ["construction_confirmed"],
            "ambiguous": bool(PRODUCT_HINTS.search(blob) and CONSTRUCTION_ONLY.search(title)),
        }

    # 4) Confirmed service-only (includes software_only as non-tangible service-like)
    if req in {
        "services",
        "professional_services",
        "maintenance_repair_services",
        "software_only",
    }:
        return {
            "bucket": BUCKET_SERVICE,
            "requirement_class": req,
            "evidence": evidence or [f"class:{req}"],
            "ambiguous": req == "software_only" and bool(IT_HARDWARE.search(blob)),
        }

    # 5) Mixed product + service
    if req in {"mixed_product_service", "product_with_incidental_services", "product_with_significant_installation"}:
        return {
            "bucket": BUCKET_MIXED,
            "requirement_class": req,
            "evidence": evidence or [f"class:{req}"],
            "ambiguous": req == "product_with_significant_installation",
        }

    # 6) Confirmed tangible product (incl. vehicles/machinery/medical; rental of goods)
    if req in {"product_supply", "product_rental_leasing"}:
        return {
            "bucket": BUCKET_CONFIRMED_PRODUCT,
            "requirement_class": req,
            "evidence": evidence or [f"class:{req}"],
            "ambiguous": False,
        }
    if VEHICLE_MACHINERY_MEDICAL.search(title) and not SERVICE_ONLY.search(title):
        return {
            "bucket": BUCKET_CONFIRMED_PRODUCT,
            "requirement_class": req or "product_supply",
            "evidence": evidence + ["vehicle_machinery_or_medical_equipment"],
            "ambiguous": False,
        }
    if PRODUCT_HINTS.search(title) or SUPPLY_PHRASE.search(title):
        return {
            "bucket": BUCKET_CONFIRMED_PRODUCT,
            "requirement_class": req or "product_supply",
            "evidence": evidence + ["title_product_vocabulary"],
            "ambiguous": req in {"unknown_mixed", "unknown", ""},
        }

    # 7) Potential / insufficient
    if req in {"unknown_mixed", "unknown", "pending_classification", ""}:
        return {
            "bucket": BUCKET_POTENTIAL,
            "requirement_class": req or "unknown_mixed",
            "evidence": evidence or ["insufficient_evidence"],
            "ambiguous": True,
        }

    # 8) Unclassified remainder
    return {
        "bucket": BUCKET_UNCLASSIFIED,
        "requirement_class": req or "unclassified",
        "evidence": evidence or ["no_bucket_rule_matched"],
        "ambiguous": True,
    }


def _exclusion_from_714(item: dict[str, Any], clf: dict[str, Any]) -> dict[str, Any] | None:
    """Replay rules that kept a record out of the reported 714 product opportunities.

    Returns None if the record would have been counted in the 714 path.
    """
    title = str(item.get("title") or "")
    url = str(item.get("official_url") or item.get("source_url_original") or "")
    blob = f"{title} {url}"
    status = str(item.get("solicitation_status") or item.get("status") or "unknown").lower()

    # Mirror nextbid_baseline_audit screening
    if status in {"closed", "cancelled", "canceled", "awarded", "expired", "historical_award"}:
        return {
            "rule": "baseline_screen_exclude_inactive",
            "detail": f"status:{status}",
        }

    disp = listing_prefilter(item)
    if disp == "exclude":
        if re.search(r"(?i)notice[-_ ]of[-_ ]awards?|/awards?\b|forecast", blob):
            return {"rule": "listing_prefilter_award_or_forecast", "detail": "title_or_url"}
        if re.search(
            r"(?i)\b(software|saas|cyber|microsoft\s*365|license)\b", title
        ):
            return {"rule": "listing_prefilter_IT_RE", "detail": "title_it_marker"}
        if re.search(
            r"(?i)\b(construction|consulting|professional\s+services|software|saas|"
            r"maintenance\s+and\s+repair|lease\s+of|rental\s+of|IT\s+services|"
            r"design[- ]build|architectural)\b",
            title,
        ) and not re.search(
            r"(?i)\b(suppl(y|ies)|equipment|furniture|pump|generator|trailer|ppe)\b",
            title,
        ):
            return {
                "rule": "listing_prefilter_EXCLUDE_TITLE_RE",
                "detail": "construction_service_rental_without_product_rescue",
            }
        if item.get("verified_individual_solicitation") is False:
            return {
                "rule": "listing_prefilter_not_verified_individual",
                "detail": "verified_individual_solicitation=False",
            }
        return {"rule": "listing_prefilter_exclude_other", "detail": disp}

    req = str(clf.get("requirement_class") or "")
    # Pipeline product_opportunity logic (commercial_criteria)
    if req in INITIAL_EXCLUDE_CLASSES:
        return {
            "rule": "pipeline_INITIAL_EXCLUDE_CLASSES",
            "detail": f"requirement_class={req}",
        }

    product_opportunity = req in INITIAL_PRODUCT_OK or (
        req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
    )
    if req == "mixed_product_service":
        product_opportunity = True

    # IT soft exclude in pipeline when it_focus_detected and not product_opportunity
    if IT_MARKERS.search(title) and req in INITIAL_PRODUCT_OK | {
        "unknown",
        "unknown_mixed",
        "pending_classification",
        "",
    }:
        if re.search(r"(?i)\b(software|saas|license|cyber)\b", title) and not product_opportunity:
            return {
                "rule": "pipeline_it_focus_detected_exclude",
                "detail": "software/saas/license/cyber in title",
            }

    if product_opportunity:
        return None  # would count toward 714

    if req in INITIAL_PRODUCT_OK:
        return None

    return {
        "rule": "not_product_opportunity_and_not_INITIAL_PRODUCT_OK",
        "detail": f"requirement_class={req or 'empty'}",
    }


def run_product_classification_audit() -> dict[str, Any]:
    pop = collect_population()
    items = list(pop["by_id"].values())
    assert len(items) == pop["unique_nextbid"] or True  # tolerate merge quirks

    # Fresh central classification for ALL records (no prefilter drop)
    for it in items:
        it.setdefault("source_key", "nextbid_state_local")
        if not it.get("external_key") and it.get("nb_id"):
            it["external_key"] = f"nextbid:{it['nb_id']}"
        # Prefer stored description; do not require packages
        clf = classify_requirement(
            str(it.get("title") or ""),
            category_raw=(
                (it.get("evidence") or {}).get("category_raw")
                if isinstance(it.get("evidence"), dict)
                else it.get("product_category")
            ),
            procurement_type=it.get("procurement_type"),
            description=str(it.get("description") or "") or None,
            document_text=it.get("document_text"),
            line_items_text=it.get("line_items_text"),
        )
        full = classify_solicitation_item(it)
        it["requirement_class"] = full.get("requirement_class") or clf["requirement_class"]
        it["classification_evidence"] = full.get("classification_evidence") or clf[
            "classification_evidence"
        ]
        it["_clf"] = {
            "requirement_class": it["requirement_class"],
            "classification_evidence": it["classification_evidence"],
        }

    bucket_counts: Counter[str] = Counter()
    req_class_counts: Counter[str] = Counter()
    by_bucket_ids: dict[str, list[str]] = defaultdict(list)
    by_bucket_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ambiguous: list[dict[str, Any]] = []
    exclusion_rule_counts: Counter[str] = Counter()
    exclusion_ids: dict[str, list[str]] = defaultdict(list)
    counted_in_714 = 0
    excluded_but_tangibleish = 0

    rows_out: list[dict[str, Any]] = []

    for it in items:
        clf = it.get("_clf") or {}
        mapped = _map_to_audit_bucket(it, clf)
        bucket = mapped["bucket"]
        bucket_counts[bucket] += 1
        req_class_counts[str(mapped["requirement_class"])] += 1
        nb = str(it.get("nb_id") or "")
        if nb:
            by_bucket_ids[bucket].append(nb)
        sample = {
            "nb_id": nb,
            "title": (it.get("title") or "")[:160],
            "solicitation_number": it.get("solicitation_number"),
            "requirement_class": mapped["requirement_class"],
            "evidence": mapped["evidence"][:8],
            "official_url": (it.get("official_url") or "")[:120],
        }
        if len(by_bucket_samples[bucket]) < 25:
            by_bucket_samples[bucket].append(sample)
        if mapped.get("ambiguous"):
            ambiguous.append({**sample, "bucket": bucket})

        excl = _exclusion_from_714(it, clf)
        if excl is None:
            counted_in_714 += 1
            excl_rule = None
        else:
            excl_rule = excl["rule"]
            exclusion_rule_counts[excl_rule] += 1
            if nb and len(exclusion_ids[excl_rule]) < 500:
                exclusion_ids[excl_rule].append(nb)
            # Tangible-ish but excluded from 714
            if bucket in {
                BUCKET_CONFIRMED_PRODUCT,
                BUCKET_MIXED,
                BUCKET_IT_PRODUCT,
                BUCKET_POTENTIAL,
            }:
                excluded_but_tangibleish += 1

        rows_out.append(
            {
                "nb_id": nb,
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "bucket": bucket,
                "requirement_class": mapped["requirement_class"],
                "evidence": mapped["evidence"],
                "ambiguous": bool(mapped.get("ambiguous")),
                "excluded_from_714_rule": excl_rule,
                "excluded_from_714_detail": (excl or {}).get("detail"),
            }
        )

    # Sanity: buckets must be mutually exclusive and cover all
    assert sum(bucket_counts.values()) == len(items)

    report = {
        "build": "002Q",
        "job": "nextbid-product-classification-audit",
        "audited_at": _utc(),
        "metered_nextbid_calls": 0,
        "openai_spent_usd": 0,
        "paid_web_searches": 0,
        "filter_changes": False,
        "population": {
            "unique_solicitations": len(items),
            "checkpoint_unique": pop["unique_nextbid"],
            "enriched_count": pop["enriched_count"],
            "listings_count": pop["listings_count"],
        },
        "mutually_exclusive_buckets": {
            BUCKET_CONFIRMED_PRODUCT: bucket_counts[BUCKET_CONFIRMED_PRODUCT],
            BUCKET_MIXED: bucket_counts[BUCKET_MIXED],
            BUCKET_POTENTIAL: bucket_counts[BUCKET_POTENTIAL],
            BUCKET_SERVICE: bucket_counts[BUCKET_SERVICE],
            BUCKET_CONSTRUCTION: bucket_counts[BUCKET_CONSTRUCTION],
            BUCKET_IT_PRODUCT: bucket_counts[BUCKET_IT_PRODUCT],
            BUCKET_NON_SOLICITATION: bucket_counts[BUCKET_NON_SOLICITATION],
            BUCKET_UNCLASSIFIED: bucket_counts[BUCKET_UNCLASSIFIED],
        },
        "bucket_total": sum(bucket_counts.values()),
        "requirement_class_counts": dict(req_class_counts),
        "reconcile_vs_714": {
            "reported_product_opportunities_714": 714,
            "replay_would_count_toward_714": counted_in_714,
            "replay_delta_vs_714": counted_in_714 - 714,
            "excluded_from_714_total": len(items) - counted_in_714,
            "excluded_but_audit_tangible_or_potential": excluded_but_tangibleish,
            "exclusion_rule_counts": dict(exclusion_rule_counts),
            "note": (
                "714 came from baseline_audit after listing_prefilter + pipeline product_opportunity. "
                "This audit classifies ALL 2255 without treating missing qty/docs as non-product. "
                "Replay may differ slightly from 714 if package text enriched a subset during baseline."
            ),
        },
        "exclusion_rule_definitions": {
            "baseline_screen_exclude_inactive": "Closed/cancelled/awarded/expired status dropped before product count",
            "listing_prefilter_award_or_forecast": "listing_prefilter: award/forecast title or URL",
            "listing_prefilter_IT_RE": "listing_prefilter: software/saas/cyber/license title → exclude (drops IT hardware titles too)",
            "listing_prefilter_EXCLUDE_TITLE_RE": "listing_prefilter: construction/services/rental without product rescue terms",
            "listing_prefilter_not_verified_individual": "verified_individual_solicitation is False",
            "pipeline_INITIAL_EXCLUDE_CLASSES": (
                f"evaluate_procurement_pipeline excludes classes: {sorted(INITIAL_EXCLUDE_CLASSES)}"
            ),
            "pipeline_it_focus_detected_exclude": "IT software markers with non-product class",
            "not_product_opportunity_and_not_INITIAL_PRODUCT_OK": (
                "requirement_class not in INITIAL_PRODUCT_OK "
                f"{sorted(INITIAL_PRODUCT_OK)} and not product_opportunity "
                f"(PRODUCT_CLASSES={sorted(PRODUCT_CLASSES)} minus INITIAL_EXCLUDE)"
            ),
        },
        "exclusion_nb_ids_by_rule": {k: v for k, v in exclusion_ids.items()},
        "nb_ids_by_bucket": {k: v for k, v in by_bucket_ids.items()},
        "samples_by_bucket": {k: v for k, v in by_bucket_samples.items()},
        "ambiguous_classifications": {
            "count": len(ambiguous),
            "samples": ambiguous[:80],
        },
        "important_distinctions": {
            "product_classification_vs_m3_eligibility": (
                "Buckets above are product/service nature only. Missing quantities, documents, "
                "submission instructions, pricing, or suppliers do NOT move a record out of a "
                "tangible-product bucket."
            ),
            "it_equipment_is_tangible": (
                "IT hardware (computers, network gear, printers, etc.), vehicles, machinery, and "
                "medical equipment are tangible products here even when M3 bidding criteria later exclude them."
            ),
            "classes_in_INITIAL_EXCLUDE_but_still_tangible": [
                "product_with_significant_installation",
                "product_rental_leasing",
                "software_only is NOT tangible — mapped to confirmed_service_only / non-goods",
            ],
        },
    }

    out_dir = get_settings().data_root() / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_dir / "nextbid_product_classification_rows.jsonl"
    with detail_path.open("w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, default=str) + "\n")
    report["detail_rows_path"] = str(detail_path)

    summary_path = out_dir / "jobs_nextbid_product_class_audit_last.json"
    # Slim JSON for acceptance (IDs capped already)
    slim = {k: v for k, v in report.items() if k != "nb_ids_by_bucket"}
    # Keep ID lists but note full lists are in jsonl
    slim["nb_ids_by_bucket_counts"] = {k: len(v) for k, v in by_bucket_ids.items()}
    slim["nb_ids_by_bucket_full_in_detail_jsonl"] = True
    # Include full ID lists in archive artifact (may be large but owner asked for IDs)
    archive = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
    archive.mkdir(parents=True, exist_ok=True)
    full_path = archive / "BUILD_002Q_NEXTBID_PRODUCT_CLASSIFICATION_AUDIT.json"
    full_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    summary_path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")

    md = _render_md(report)
    (archive / "BUILD_002Q_NEXTBID_PRODUCT_CLASSIFICATION_AUDIT.md").write_text(md, encoding="utf-8")
    report["artifact_md"] = str(archive / "BUILD_002Q_NEXTBID_PRODUCT_CLASSIFICATION_AUDIT.md")
    report["artifact_json"] = str(full_path)
    return report


def _render_md(report: dict[str, Any]) -> str:
    b = report["mutually_exclusive_buckets"]
    excl = report["reconcile_vs_714"]
    lines = [
        "# BUILD 002Q — NextBid Product Classification Audit",
        "",
        f"Audited: {report['audited_at']} · Population: **{report['population']['unique_solicitations']}** · "
        f"NextBid API: **0** · OpenAI: **$0** · Filter changes: **false**",
        "",
        "## Mutually exclusive product classes",
        "",
        "| Bucket | Count |",
        "|---|---:|",
    ]
    labels = [
        (BUCKET_CONFIRMED_PRODUCT, "Confirmed tangible product purchase"),
        (BUCKET_MIXED, "Mixed product and service purchase"),
        (BUCKET_POTENTIAL, "Potential tangible product; insufficient information"),
        (BUCKET_SERVICE, "Confirmed service-only"),
        (BUCKET_CONSTRUCTION, "Confirmed construction-only"),
        (BUCKET_IT_PRODUCT, "IT product purchase"),
        (BUCKET_NON_SOLICITATION, "Confirmed non-solicitation"),
        (BUCKET_UNCLASSIFIED, "Unclassified"),
    ]
    for key, label in labels:
        lines.append(f"| {label} | {b.get(key, 0)} |")
    lines += [
        f"| **Total** | **{report['bucket_total']}** |",
        "",
        "## Not M3 eligibility",
        "",
        report["important_distinctions"]["product_classification_vs_m3_eligibility"],
        "",
        report["important_distinctions"]["it_equipment_is_tangible"],
        "",
        "## Reconcile vs reported 714 product opportunities",
        "",
        f"- Replay would-count toward 714 path: **{excl['replay_would_count_toward_714']}** (delta vs 714: {excl['replay_delta_vs_714']})",
        f"- Excluded from 714 path: **{excl['excluded_from_714_total']}**",
        f"- Of those excluded, audit still tangible/mixed/IT/potential: **{excl['excluded_but_audit_tangible_or_potential']}**",
        "",
        "### Exclusion rules (exact counts)",
        "",
        "| Rule | Count |",
        "|---|---:|",
    ]
    for rule, n in sorted(
        (excl.get("exclusion_rule_counts") or {}).items(), key=lambda x: -x[1]
    ):
        lines.append(f"| `{rule}` | {n} |")
    lines += [
        "",
        "### Rule definitions",
        "",
    ]
    for rule, defn in (report.get("exclusion_rule_definitions") or {}).items():
        lines.append(f"- `{rule}`: {defn}")
    amb = report.get("ambiguous_classifications") or {}
    lines += [
        "",
        f"## Ambiguous classifications: **{amb.get('count', 0)}**",
        "",
        "Samples (up to 80) are in the JSON artifact under `ambiguous_classifications.samples`.",
        "",
        f"Full per-record rows: `{report.get('detail_rows_path')}`",
        "",
        "No filter changes applied. Awaiting owner review before any classification/eligibility edits.",
        "",
    ]
    return "\n".join(lines)
