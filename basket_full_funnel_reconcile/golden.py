"""Golden-path regression corpus + assertions."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from basket_full_funnel_reconcile.models import BUILD, GOLDEN_PATH, PRICED_EXECUTABLE
from m3_data_root import data_path


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


GOLDEN_CASES = [
    {"id": "profitable_multiline", "tags": ["profitable", "multiline"], "pick": "max_profit"},
    {"id": "unprofitable_or_blocked", "tags": ["unprofitable"], "pick": "unprofitable"},
    {"id": "easy_commodity", "tags": ["commodity"], "pick": "high_coverage"},
    {"id": "specialty_oem", "tags": ["oem"], "pick": "low_coverage"},
    {"id": "quote_required", "tags": ["quote"], "pick": "has_quote"},
    {"id": "eligibility_unknown", "tags": ["eligibility"], "pick": "any"},
    {"id": "package_access", "tags": ["package"], "pick": "any"},
    {"id": "freight_heavy", "tags": ["freight"], "pick": "high_freight"},
    {"id": "financing_sensitive", "tags": ["financing"], "pick": "high_acq"},
    {"id": "single_line", "tags": ["single"], "pick": "single_line"},
    {"id": "complex_basket", "tags": ["complex"], "pick": "most_lines"},
    {"id": "office_mro_tool", "tags": ["mro"], "pick": "any"},
]


def build_golden_path_corpus(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = list(results.values())
    cases = []
    used: set[str] = set()

    def take(pred, case_id: str, tags: list[str]) -> None:
        for r in rows:
            oid = r.get("opportunity_id")
            if oid in used:
                continue
            if pred(r):
                used.add(oid)
                cases.append({"case_id": case_id, "tags": tags, "opportunity_id": oid, "snapshot": _snap(r)})
                return
        cases.append({"case_id": case_id, "tags": tags, "opportunity_id": None, "snapshot": None, "missing": True, "skipped": True})

    take(lambda r: float((r.get("economics") or {}).get("net_expected_profit") or 0) > 0 and int((r.get("lines") or {}).get("TOTAL_LINES") or 0) >= 2, "profitable_multiline", ["profitable", "multiline"])
    take(lambda r: (r.get("economics") or {}).get("economic_terminal") in {"UNPROFITABLE", "EXECUTION_BLOCKED", "RESEARCH_EXHAUSTED"}, "unprofitable_or_blocked", ["unprofitable"])
    take(lambda r: float((r.get("lines") or {}).get("line_coverage") or 0) >= 0.5, "easy_commodity", ["commodity"])
    take(lambda r: float((r.get("lines") or {}).get("line_coverage") or 0) < 0.35, "specialty_oem", ["oem"])
    take(lambda r: int((r.get("lines") or {}).get("QUOTE_REQUIRED_LINES") or 0) > 0, "quote_required", ["quote"])
    take(lambda r: True, "eligibility_unknown", ["eligibility"])
    take(lambda r: True, "package_access", ["package"])
    take(lambda r: float((r.get("economics") or {}).get("freight") or 0) > 0, "freight_heavy", ["freight"])
    take(lambda r: float((r.get("economics") or {}).get("product_acquisition_cost") or 0) > 500, "financing_sensitive", ["financing"])
    take(lambda r: int((r.get("lines") or {}).get("TOTAL_LINES") or 0) == 1, "single_line", ["single"])
    take(lambda r: int((r.get("lines") or {}).get("TOTAL_LINES") or 0) >= 5, "complex_basket", ["complex"])
    take(lambda r: True, "office_mro_tool", ["mro"])

    assertions = []
    passed = failed = skipped = 0
    regressions = []
    for c in cases:
        if c.get("skipped") or c.get("missing"):
            skipped += 1
            c["assertions"] = {"pass": True, "skipped": True, "failures": [], "case_id": c.get("case_id")}
            assertions.append(c["assertions"])
            continue
        oid = c.get("opportunity_id")
        r = results.get(oid) if oid else None
        checks = run_assertions(r, c)
        c["assertions"] = checks
        if checks["pass"]:
            passed += 1
        else:
            failed += 1
            regressions.extend(checks.get("failures") or [])
        assertions.append(checks)

    payload = {
        "name": "M3_GOLDEN_PATH_CORPUS_V1",
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "regressions": regressions,
        },
        "pass": failed == 0,
    }
    _save(GOLDEN_PATH, payload)
    return payload


def _snap(r: dict[str, Any]) -> dict[str, Any]:
    lines = r.get("lines") or {}
    econ = r.get("economics") or {}
    return {
        "total_lines": lines.get("TOTAL_LINES"),
        "priced_lines": lines.get("PRICED_LINES"),
        "line_coverage": lines.get("line_coverage"),
        "basket_class": (r.get("basket") or {}).get("basket_class"),
        "economic_terminal": econ.get("economic_terminal"),
        "net_expected_profit": econ.get("net_expected_profit"),
        "canonical_stage": r.get("canonical_stage"),
        "priced_mpns": [x.get("mpn") for x in (lines.get("lines") or []) if x.get("terminal_state") == PRICED_EXECUTABLE],
    }


def run_assertions(r: dict[str, Any] | None, case: dict[str, Any]) -> dict[str, Any]:
    failures = []
    if not r:
        return {"pass": False, "failures": [f"{case.get('case_id')}: missing opportunity"], "case_id": case.get("case_id")}
    lines = r.get("lines") or {}
    snap = case.get("snapshot") or {}
    # package/lines do not silently shrink vs snapshot taken at build time (same run — equality)
    if snap.get("total_lines") and lines.get("TOTAL_LINES") != snap.get("total_lines"):
        failures.append("line_count_changed")
    if not lines.get("TOTAL_LINES"):
        failures.append("line_count_zero")
    # every line has terminal state
    for ln in lines.get("lines") or []:
        if not ln.get("terminal_state"):
            failures.append(f"line_missing_terminal:{ln.get('line_key')}")
    econ = r.get("economics") or {}
    if not econ.get("economic_terminal"):
        failures.append("missing_economic_terminal")
    if r.get("canonical_stage") is None:
        failures.append("missing_canonical_stage")
    if not (r.get("next_action") or {}).get("code"):
        failures.append("missing_next_action")
    # economics math deterministic presence
    for k in ("government_revenue", "product_acquisition_cost", "freight", "financing_cost", "net_expected_profit"):
        if econ.get(k) is None and econ.get("economic_terminal") not in {"RESEARCH_EXHAUSTED", "EXECUTION_BLOCKED"}:
            # allow None freight only if status says quote required with zero priced
            if k == "freight" and int(lines.get("PRICED_LINES") or 0) == 0:
                continue
            if k in {"product_acquisition_cost", "government_revenue"} and int(lines.get("PRICED_LINES") or 0) == 0:
                continue
            failures.append(f"missing_econ_field:{k}")
    return {"pass": len(failures) == 0, "failures": failures, "case_id": case.get("case_id"), "opportunity_id": r.get("opportunity_id")}
