"""Staged product-identity run over OpenGov recovered packages."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from product_identity.models import (
    BUILD_TARGET,
    EXACT_CATALOG_NUMBER,
    EXACT_MODEL,
    EXACT_MPN,
    EXACT_NSN,
    EXACT_UPC,
    LINE_ITEM_AMBIGUOUS,
    LINE_ITEM_CONFIRMED,
    LINE_ITEM_LIKELY,
    NO_IDENTITY,
    NOT_LINE_ITEM,
    PERMITTED_EQUAL,
    STRONG_GENERIC_SPEC,
    USABLE_GRADES,
    WEAK_IDENTITY,
)
from product_identity.normalizer import ProductIdentityNormalizer
from product_identity.package_process import discover_package_dirs, process_package

log = logging.getLogger("govtracker.product_identity.batch")

CHECKPOINT = "m3_product_identity_checkpoint.json"
REPORT = "m3_product_identity_last_report.json"
STORE = "m3_product_identity_store.json"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT), data_path(STORE)


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _handoff_evidence(identities: list[dict[str, Any]], *, opportunity_id: str) -> dict[str, Any]:
    """Send A/B/C identities into existing line-item economics (no engine changes)."""
    usable = [i for i in identities if i.get("confidence_grade") in USABLE_GRADES]
    if not usable:
        return {"sent": 0, "gov_value": 0, "public_cost": 0, "both": 0}
    try:
        from line_item_economics.engine import analyze_line_item_economics
        from line_item_economics.extract import line_from_row
    except Exception as exc:
        return {"sent": 0, "error": type(exc).__name__}

    existing = []
    for i, ident in enumerate(usable[:200], start=1):
        existing.append(
            line_from_row(
                {
                    "clin": ident.get("attributes", {}).get("item") if isinstance(ident.get("attributes"), dict) else None,
                    "description": ident.get("raw_description"),
                    "manufacturer": ident.get("manufacturer"),
                    "model": ident.get("model"),
                    "part_number": ident.get("part_number") or ident.get("catalog_number"),
                    "nsn": ident.get("nsn"),
                    "quantity": ident.get("quantity"),
                    "uom": ident.get("uom_normalized") or ident.get("uom"),
                    "or_equal_allowed": "YES" if ident.get("equal_allowed") else "UNKNOWN",
                    "salient_characteristics": ident.get("raw_description"),
                    "original_text": ident.get("commercial_search_key") or ident.get("raw_description"),
                },
                index=i,
                provenance={"source": "product_identity", "identity_type": ident.get("identity_type")},
            )
        )
    try:
        lie = analyze_line_item_economics(
            opportunity_id=f"{opportunity_id}:pi",
            body_text=None,
            existing_lines=existing,
            persist=True,
        )
    except Exception as exc:
        return {"sent": len(existing), "error": type(exc).__name__}

    gov = 0
    cost = 0
    both = 0
    for ln in lie.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        has_gov = bool((ln.get("historical") or {}).get("unit_price") or (ln.get("historical") or {}).get("matched"))
        has_cost = bool((ln.get("retail") or {}).get("unit_price"))
        # Engine may not find evidence without supplied retail/historical maps — count attempted handoff
        if has_gov:
            gov += 1
        if has_cost:
            cost += 1
        if has_gov and has_cost:
            both += 1
    return {
        "sent": len(existing),
        "gov_value": gov,
        "public_cost": cost,
        "both": both,
        "analysis_lines": len(lie.get("lines") or []),
    }


def run_product_identity_stage(
    *,
    line_limit: int = 500,
    resume: bool = True,
    handoff: bool = True,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    max_packages: int | None = None,
) -> dict[str, Any]:
    """Process packages until ~line_limit confirmed/likely lines classified."""
    from m3_data_root import data_path

    run_id = run_id or f"PI-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck_path, report_path, store_path = _paths()
    ck = _load_json(ck_path, {"kind": "ProductIdentityCheckpoint", "processed_pkgs": [], "stats": {}}) if resume else {
        "kind": "ProductIdentityCheckpoint",
        "processed_pkgs": [],
        "stats": {},
    }
    done = set(ck.get("processed_pkgs") or []) if resume else set()
    store = _load_json(store_path, {"kind": "ProductIdentityStore", "by_opportunity": {}})
    by_opp = store.setdefault("by_opportunity", {})

    root = data_path("opengov_public_docs", "documents")
    packages = discover_package_dirs(root)

    # Prefer packages with spreadsheets (higher structured identity yield)
    def _pkg_rank(item: tuple[str, str, Any]) -> tuple[int, int, str]:
        _gov, _pid, path = item
        try:
            names = [p.name.lower() for p in path.iterdir() if p.is_file()]
        except Exception:
            names = []
        has_sheet = any(n.endswith((".xlsx", ".xls", ".csv")) for n in names)
        catalogish = any(
            any(k in n for k in ("part", "catalog", "price", "bid", "schedule", "usage", "inventory"))
            for n in names
            if n.endswith((".xlsx", ".xls", ".csv"))
        )
        # Lower is better: catalog spreadsheets first, then any sheet, then PDF-only
        tier = 0 if catalogish else (1 if has_sheet else 2)
        return (tier, 0 if has_sheet else 1, f"{_gov}/{_pid}")

    packages = sorted(packages, key=_pkg_rank)
    if max_packages is not None:
        packages = packages[: max(0, int(max_packages))]

    stats: Counter = Counter(ck.get("stats") or {}) if resume else Counter()
    id_c: Counter = Counter()
    grade_c: Counter = Counter()
    ex_c: Counter = Counter()
    q_c: Counter = Counter()
    fail_c: Counter = Counter()
    src_id_c: Counter = Counter()  # source_kind → usable
    samples: list[dict[str, Any]] = []
    processed_pkgs = list(done)
    normalizer = ProductIdentityNormalizer()

    # Count already-usable lines in store toward limit when resuming
    prior_usable = int(stats.get("usable_abc") or 0)
    usable_lines = prior_usable

    def prog(pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase="PRODUCT_IDENTITY", pct=pct, **extra)
            except Exception:
                pass

    prog(2, packages=len(packages), done=len(done))

    for pi, (gov, pid, path) in enumerate(packages):
        key = f"{gov}/{pid}"
        if key in done:
            continue
        if usable_lines >= line_limit and int(stats.get("confirmed_likely") or 0) >= line_limit:
            break

        try:
            result = process_package(gov, pid, path, normalizer=normalizer)
        except Exception as exc:
            fail_c[type(exc).__name__] += 1
            processed_pkgs.append(key)
            continue

        stats["packages_tested"] += 1
        stats["raw_rows"] += int(result.get("raw_rows") or 0)
        for k, v in (result.get("extraction_counts") or {}).items():
            ex_c[k] += int(v)
            stats[f"ex_{k}"] += int(v)
        for k, v in (result.get("quality_counts") or {}).items():
            q_c[k] += int(v)
            stats[f"q_{k}"] += int(v)

        confirmed_likely = int((result.get("quality_counts") or {}).get(LINE_ITEM_CONFIRMED) or 0) + int(
            (result.get("quality_counts") or {}).get(LINE_ITEM_LIKELY) or 0
        )
        # Cap per-package contribution so one noisy sheet cannot burn the whole stage budget
        stats["confirmed_likely"] += min(confirmed_likely, 120)
        stats["confirmed_likely_uncapped"] = int(stats.get("confirmed_likely_uncapped") or 0) + confirmed_likely

        pkg_usable = 0
        for ident in result.get("identities") or []:
            it = str(ident.get("identity_type") or NO_IDENTITY)
            id_c[it] += 1
            grade_c[str(ident.get("confidence_grade") or "F")] += 1
            if ident.get("confidence_grade") in USABLE_GRADES:
                pkg_usable += 1
                usable_lines += 1
                sk = str(ident.get("source_kind") or "unknown")
                src_id_c[sk] += 1
            if ident.get("enriched_from_spec"):
                stats["enriched_from_spec"] += 1
            if ident.get("inherited_manufacturer"):
                stats["inherited_manufacturer"] += 1
            if ident.get("brand_or_equal_context"):
                stats["brand_or_equal"] += 1

        stats["usable_abc"] = usable_lines
        if pkg_usable:
            stats["packages_with_usable"] += 1
        if pkg_usable >= 5:
            stats["packages_ge5"] += 1
        if pkg_usable >= 10:
            stats["packages_ge10"] += 1

        hand = {"sent": 0, "gov_value": 0, "public_cost": 0, "both": 0}
        if handoff and result.get("usable"):
            hand = _handoff_evidence(result["usable"], opportunity_id=result["opportunity_id"])
            stats["handoff_sent"] += int(hand.get("sent") or 0)
            stats["handoff_gov"] += int(hand.get("gov_value") or 0)
            stats["handoff_cost"] += int(hand.get("public_cost") or 0)
            stats["handoff_both"] += int(hand.get("both") or 0)

        by_opp[result["opportunity_id"]] = {
            "opportunity_id": result["opportunity_id"],
            "raw_rows": result.get("raw_rows"),
            "quality_counts": result.get("quality_counts"),
            "extraction_counts": result.get("extraction_counts"),
            "identity_counts": dict(Counter(str(i.get("identity_type")) for i in (result.get("identities") or []))),
            "usable_count": pkg_usable,
            "identities": (result.get("identities") or [])[:500],
            "handoff": hand,
            "updated_at": now_utc().isoformat(),
        }

        if len(samples) < 40:
            for ident in (result.get("usable") or [])[:3]:
                samples.append(
                    {
                        "opportunity_id": result["opportunity_id"],
                        "identity_type": ident.get("identity_type"),
                        "grade": ident.get("confidence_grade"),
                        "key": ident.get("commercial_search_key"),
                        "mfr": ident.get("manufacturer"),
                        "model": ident.get("model"),
                        "pn": ident.get("part_number") or ident.get("catalog_number"),
                        "source": ident.get("source_kind"),
                    }
                )

        processed_pkgs.append(key)
        done.add(key)
        if (pi + 1) % 10 == 0:
            _save_json(
                ck_path,
                {
                    "kind": "ProductIdentityCheckpoint",
                    "run_id": run_id,
                    "processed_pkgs": processed_pkgs[-100000:],
                    "stats": dict(stats),
                    "updated_at": now_utc().isoformat(),
                },
            )
            _save_json(store_path, store)
        pct = 5 + int(90 * min(1.0, usable_lines / max(1, line_limit)))
        if (pi + 1) % 5 == 0:
            prog(pct, packages=stats.get("packages_tested", 0), usable=usable_lines, raw=stats.get("raw_rows", 0))

        if int(stats.get("confirmed_likely") or 0) >= line_limit:
            break

    _save_json(store_path, store)

    confirmed = int(q_c.get(LINE_ITEM_CONFIRMED) or stats.get(f"q_{LINE_ITEM_CONFIRMED}") or 0)
    likely = int(q_c.get(LINE_ITEM_LIKELY) or stats.get(f"q_{LINE_ITEM_LIKELY}") or 0)
    # Prefer counters accumulated this+resume via stats keys
    if resume:
        confirmed = int(stats.get(f"q_{LINE_ITEM_CONFIRMED}") or confirmed)
        likely = int(stats.get(f"q_{LINE_ITEM_LIKELY}") or likely)
    real_lines = confirmed + likely
    usable_abc = int(stats.get("usable_abc") or 0)
    packages_tested = int(stats.get("packages_tested") or 0)

    report = {
        "kind": "ProductIdentityStageReport",
        "build_target": BUILD_TARGET,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "line_limit": line_limit,
        "RAW_EXTRACTION_AUDIT": {
            "Raw rows": int(stats.get("raw_rows") or 0),
            "Real purchasing lines": int(stats.get(f"ex_REAL_PURCHASING_LINE") or 0),
            "Headers": int(stats.get("ex_HEADER") or 0),
            "Boilerplate": int(stats.get("ex_BOILERPLATE") or 0),
            "Spec text": int(stats.get("ex_SPEC_TEXT") or 0),
            "Instructions": int(stats.get("ex_INSTRUCTION_TEXT") or 0),
            "Duplicates": int(stats.get("ex_DUPLICATE") or 0),
            "Table fragments": int(stats.get("ex_TABLE_FRAGMENT") or 0),
            "Other noise": int(stats.get("ex_OTHER_NOISE") or 0),
        },
        "LINE_QUALITY": {
            "Confirmed": confirmed,
            "Likely": likely,
            "Ambiguous": int(stats.get(f"q_{LINE_ITEM_AMBIGUOUS}") or 0),
            "Not line item": int(stats.get(f"q_{NOT_LINE_ITEM}") or 0),
        },
        "IDENTITY": {
            "EXACT_MPN": int(id_c.get(EXACT_MPN) or 0),
            "EXACT_MODEL": int(id_c.get(EXACT_MODEL) or 0),
            "EXACT_CATALOG_NUMBER": int(id_c.get(EXACT_CATALOG_NUMBER) or 0),
            "EXACT_NSN": int(id_c.get(EXACT_NSN) or 0),
            "EXACT_UPC": int(id_c.get(EXACT_UPC) or 0),
            "PERMITTED_EQUAL": int(id_c.get(PERMITTED_EQUAL) or 0),
            "STRONG_GENERIC_SPEC": int(id_c.get(STRONG_GENERIC_SPEC) or 0),
            "WEAK_IDENTITY": int(id_c.get(WEAK_IDENTITY) or 0),
            "NO_IDENTITY": int(id_c.get(NO_IDENTITY) or 0),
        },
        "CONFIDENCE": {g: int(grade_c.get(g) or 0) for g in ("A", "B", "C", "D", "F")},
        "PACKAGE_LEVEL": {
            "Packages tested": packages_tested,
            "Packages with >=1 A/B/C identity": int(stats.get("packages_with_usable") or 0),
            "Packages with >=5 usable identities": int(stats.get("packages_ge5") or 0),
            "Packages with >=10 usable identities": int(stats.get("packages_ge10") or 0),
            "Commercially researchable line rate": round(usable_abc / real_lines, 4) if real_lines else 0.0,
        },
        "DOCUMENT_JOINING": {
            "Lines enriched from separate spec docs": int(stats.get("enriched_from_spec") or 0),
            "Lines with inherited manufacturer": int(stats.get("inherited_manufacturer") or 0),
            "Lines with recovered brand-or-equal context": int(stats.get("brand_or_equal") or 0),
        },
        "DOWNSTREAM_HANDOFF": {
            "Usable identities sent to gov-value research": int(stats.get("handoff_sent") or 0),
            "Usable identities sent to acquisition-cost research": int(stats.get("handoff_sent") or 0),
            "Gov-value found": int(stats.get("handoff_gov") or 0),
            "Public cost found": int(stats.get("handoff_cost") or 0),
            "Both sides known": int(stats.get("handoff_both") or 0),
        },
        "TOP_FAILURE_REASONS": dict(fail_c.most_common(12)),
        "source_usable_yield": dict(src_id_c),
        "samples": samples,
        "prior_audit": {
            "note": "Previous 225k rows were PDF-as-CSV garbage with 100% null descriptions",
            "audit_file": "PRODUCT_IDENTITY_AUDIT.json",
        },
        "stats": dict(stats),
    }

    # Fill failure reasons from quality/extraction if empty
    if not report["TOP_FAILURE_REASONS"]:
        report["TOP_FAILURE_REASONS"] = {
            "NOT_LINE_ITEM": int(stats.get(f"q_{NOT_LINE_ITEM}") or 0),
            "OTHER_NOISE": int(stats.get("ex_OTHER_NOISE") or 0),
            "NO_IDENTITY": int(id_c.get(NO_IDENTITY) or 0),
            "WEAK_IDENTITY": int(id_c.get(WEAK_IDENTITY) or 0),
        }

    xlsx_u = int(src_id_c.get("xlsx") or 0) + int(src_id_c.get("xls") or 0) + int(src_id_c.get("csv") or 0)
    pdf_u = int(src_id_c.get("pdf") or 0)
    report["MOST_IMPORTANT_ANSWERS"] = {
        "1": "PDF body text was passed as csv_text when comma-count>20, so DictReader produced empty-column rows; 100% null descriptions → UNKNOWN identity.",
        "2": f"Of prior ~161k–225k store rows, effectively 0 were real purchasing lines (null desc). New extraction found {report['RAW_EXTRACTION_AUDIT']['Real purchasing lines']} real purchasing lines.",
        "3": usable_abc,
        "4": int(stats.get("packages_with_usable") or 0),
        "5": report["PACKAGE_LEVEL"]["Commercially researchable line rate"],
        "6": f"Spreadsheets usable={xlsx_u}, PDFs usable={pdf_u} — {'spreadsheets' if xlsx_u >= pdf_u else 'PDFs'} currently better for identity.",
        "7": bool(int(stats.get("enriched_from_spec") or 0) > 0),
        "8": "Bid/pricing schedules often lack manufacturer columns; need deeper spec-PDF joining and labeled MPN extraction.",
        "9": int(stats.get("handoff_gov") or 0) > 0,
        "10": int(stats.get("handoff_cost") or 0) > 0,
    }

    _save_json(
        ck_path,
        {
            "kind": "ProductIdentityCheckpoint",
            "run_id": run_id,
            "processed_pkgs": processed_pkgs[-100000:],
            "stats": dict(stats),
            "updated_at": now_utc().isoformat(),
            "last_report_summary": {
                "usable_abc": usable_abc,
                "packages_tested": packages_tested,
                "real_lines": real_lines,
            },
        },
    )
    _save_json(report_path, report)
    prog(100, packages=packages_tested, usable=usable_abc, raw=stats.get("raw_rows", 0))
    return report
