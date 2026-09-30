"""CLI: python -m validation_harness [run|list] ..."""

from __future__ import annotations

import argparse
import json
import sys

from validation_harness.case_loader import list_cases, load_cases
from validation_harness.readiness_tests import adversarial_cases, false_rejection_cases
from validation_harness.reports import write_run_artifacts
from validation_harness.runner import run_case, run_corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3 Phase E validation harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List golden cases")
    p_list.add_argument("--tags", nargs="*", default=None)

    p_run = sub.add_parser("run", help="Run golden corpus")
    p_run.add_argument("--case", action="append", dest="cases", default=None, help="Case id (repeatable)")
    p_run.add_argument("--tags", nargs="*", default=None)
    p_run.add_argument("--adversarial", action="store_true")
    p_run.add_argument("--include-programmatic-adversarial", action="store_true", help="Also run readiness_tests adversarial suite")
    p_run.add_argument("--no-artifacts", action="store_true")
    p_run.add_argument("--json", action="store_true", help="Print summary JSON to stdout")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        cases = load_cases(tags=args.tags)
        for c in cases:
            print(f"{c.get('case_id')}\t{c.get('case_name')}\t{','.join(c.get('tags') or [])}")
        print(f"Total: {len(cases)}")
        return 0

    if args.cmd == "run":
        run = run_corpus(tags=args.tags, case_ids=args.cases, adversarial_only=bool(args.adversarial))
        if args.include_programmatic_adversarial:
            extra = adversarial_cases() + false_rejection_cases()
            from validation_harness.runner import run_case as _rc

            extra_results = [_rc(c, run_id=run["run_id"]) for c in extra]
            run["results"].extend(extra_results)
            run["case_count"] = len(run["results"])
            run["passed"] = sum(1 for r in run["results"] if r.get("status") == "PASSED")
            run["partial"] = sum(1 for r in run["results"] if r.get("status") == "PARTIAL")
            run["failed"] = sum(1 for r in run["results"] if r.get("status") == "FAILED")
            gaps = []
            for r in run["results"]:
                gaps.extend(r.get("gaps") or [])
            run["gaps"] = gaps
            run["gap_count"] = len(gaps)
            run["critical_gaps"] = sum(1 for g in gaps if g.get("severity") == "CRITICAL")

        paths = None
        if not args.no_artifacts:
            paths = write_run_artifacts(run)
        if args.json:
            print(json.dumps({k: run[k] for k in ("run_id", "case_count", "passed", "partial", "failed", "gap_count", "critical_gaps")}, indent=2))
        else:
            print(
                f"M3 validation run {run['run_id']}: "
                f"{run['passed']} passed / {run['partial']} partial / {run['failed']} failed "
                f"({run['case_count']} cases, {run['gap_count']} gaps)"
            )
            if paths:
                print(f"Artifacts: {paths['output_dir']}")
        # Exit nonzero only on FAILED (partial is expected while closing business gaps)
        return 1 if run.get("failed") else 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
