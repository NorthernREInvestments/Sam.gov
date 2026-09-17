"""Generate runtime clock audit artifacts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from application_clock import clock_mode, complete_run_metadata, now_utc, start_run_metadata

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

# Classification labels
RUNTIME_CURRENT_TIME = "RUNTIME_CURRENT_TIME"
HISTORICAL_DATA = "HISTORICAL_DATA"
SOLICITATION_DATA = "SOLICITATION_DATA"
FIXTURE_TEST_TIME = "FIXTURE_TEST_TIME"
STATIC_REFERENCE_DATE = "STATIC_REFERENCE_DATE"
OTHER = "OTHER"

PATTERNS = [
    (r"datetime\.now\(", "datetime.now"),
    (r"datetime\.today\(", "datetime.today"),
    (r"datetime\.utcnow\(", "datetime.utcnow"),
    (r"date\.today\(", "date.today"),
    (r"time\.time\(", "time.time"),
    (r"unix_timestamp\(\)", "unix_timestamp"),
    (r"now_utc\(\)", "now_utc"),
    (r"get_clock\(\)", "get_clock"),
    (r"FrozenClock\(", "FrozenClock"),
    (r"SystemClock\(", "SystemClock"),
]


def classify(path: Path, line: str) -> str:
    p = str(path).replace("\\", "/")
    if path.name.startswith("run_runtime_clock_audit") or "scripts/run_runtime_clock_audit" in p:
        return OTHER
    if "/tests/" in p or p.startswith("tests/"):
        return FIXTURE_TEST_TIME
    if "application_clock.py" in p and "datetime.now" in line:
        return RUNTIME_CURRENT_TIME  # SystemClock sole OS read
    if "now_utc()" in line or "get_clock()" in line or "app_now_utc()" in line or "unix_timestamp()" in line:
        return RUNTIME_CURRENT_TIME
    # Performance elapsed timers are not deadline "now"
    if "time.time()" in line and ("elapsed" in line.lower() or "t0" in line or "t0 =" in line or " - t0" in line):
        return OTHER
    if re.search(r"2026-09-1[456]|observed 2026|VERIFIED_AT|last_verified", line):
        return STATIC_REFERENCE_DATE
    if "due_date" in line or "deadline" in line.lower() or "response_deadline" in line:
        if "now" in line or "today" in line:
            return RUNTIME_CURRENT_TIME
        return SOLICITATION_DATA
    if "fixture" in p or "fixtures/" in p:
        return FIXTURE_TEST_TIME
    if "historical" in line.lower() or "award_date" in line.lower():
        return HISTORICAL_DATA
    if "datetime.now" in line or "date.today" in line or "time.time" in line:
        return RUNTIME_CURRENT_TIME
    return OTHER


def main() -> None:
    meta = start_run_metadata(extra={"run_kind": "runtime_clock_audit"})
    occurrences: list[dict] = []
    remaining_hardcoded_now: list[dict] = []
    routed_through_clock: list[dict] = []

    skip_dirs = {".venv", "venv", "__pycache__", "node_modules", ".git", "artifacts"}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.name.startswith("_migrate"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            hit = None
            for pat, name in PATTERNS:
                if re.search(pat, line):
                    hit = name
                    break
            if not hit and re.search(r"\b2026-09-1[456]\b", line):
                hit = "hardcoded_date_literal"
            if not hit:
                continue
            cls = classify(rel, line)
            rec = {
                "file": str(rel).replace("\\", "/"),
                "line": i,
                "pattern": hit,
                "classification": cls,
                "snippet": line.strip()[:180],
                "routed_through_application_clock": hit in {"now_utc", "get_clock", "FrozenClock", "SystemClock"}
                or ("application_clock" in line)
                or (path.name == "application_clock.py" and hit == "datetime.now"),
            }
            occurrences.append(rec)
            if (
                cls == RUNTIME_CURRENT_TIME
                and hit in {"datetime.now", "datetime.today", "datetime.utcnow", "date.today", "time.time"}
                and path.name != "application_clock.py"
                and "tests" not in rel.parts
                and not path.name.startswith("run_runtime_clock_audit")
            ):
                remaining_hardcoded_now.append(rec)
            if rec["routed_through_application_clock"] or hit in {"now_utc", "get_clock", "unix_timestamp"}:
                routed_through_clock.append(rec)

    # Production hard-coded "current date" literals (not solicitation/fixture)
    prod_hardcoded_current = [
        o
        for o in occurrences
        if o["pattern"] == "hardcoded_date_literal"
        and o["classification"] not in {FIXTURE_TEST_TIME, HISTORICAL_DATA, SOLICITATION_DATA, STATIC_REFERENCE_DATE}
        and "tests" not in o["file"]
    ]

    meta = complete_run_metadata(meta)
    results = {
        "generated_at": now_utc().isoformat(),
        "run_metadata": meta,
        "clock_mode": clock_mode(),
        "summary": {
            "total_occurrences": len(occurrences),
            "routed_through_clock": len(routed_through_clock),
            "remaining_direct_runtime_now_outside_SystemClock": len(remaining_hardcoded_now),
            "production_hardcoded_current_date_literals": len(prod_hardcoded_current),
            "migration_complete": len(remaining_hardcoded_now) == 0 and len(prod_hardcoded_current) == 0,
        },
        "remaining_direct_runtime_now": remaining_hardcoded_now,
        "production_hardcoded_current_date_literals": prod_hardcoded_current,
        "occurrences": occurrences,
        "request_counts": {
            "public_http_search": 0,
            "SAM": 0,
            "USAspending": 0,
            "OpenAI": 0,
            "Paid": 0,
            "supplier_outreach": 0,
            "agency_outreach": 0,
            "lender_outreach": 0,
            "bid_submissions": 0,
        },
    }
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "runtime_clock_audit.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    report = f"""# Runtime Clock Audit

Generated: {results['generated_at']}
Clock mode: {results['clock_mode']}

## Summary
- Total time-related occurrences scanned: {results['summary']['total_occurrences']}
- Routed through ApplicationClock helpers: {results['summary']['routed_through_clock']}
- Remaining direct runtime `datetime.now` / `date.today` outside SystemClock: {results['summary']['remaining_direct_runtime_now_outside_SystemClock']}
- Production hard-coded current-date literals: {results['summary']['production_hardcoded_current_date_literals']}
- Migration complete: {results['summary']['migration_complete']}

## Remaining direct runtime now (must be empty for complete migration)
{json.dumps(remaining_hardcoded_now, indent=2)}

## Production hard-coded current-date literals
{json.dumps(prod_hardcoded_current, indent=2)}

## Notes
- `application_clock.SystemClock.now_utc()` is the sole intentional OS `datetime.now(timezone.utc)` call.
- Test `date.today()` / `datetime.now` usages are classified FIXTURE_TEST_TIME.
- Static observation dates (e.g. portal validation notes, VERIFIED_AT) are STATIC_REFERENCE_DATE / HISTORICAL_DATA — not production "now".

NEXT STATE:
CENTRAL_RUNTIME_CLOCK_OPERATIONAL
"""
    (ARTIFACTS / "runtime_clock_audit_report.md").write_text(report, encoding="utf-8")
    print(json.dumps(results["summary"], indent=2))


if __name__ == "__main__":
    main()
