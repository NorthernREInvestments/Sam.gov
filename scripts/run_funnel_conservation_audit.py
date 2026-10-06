"""Run funnel conservation audit; write compact summary JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from funnel_conservation.audit import run_funnel_conservation_audit


def prog(**kw):
    print(f"  {kw.get('pct','?'):>3}% {kw.get('phase','')}", flush=True)


def main() -> int:
    r = run_funnel_conservation_audit(on_progress=prog, fix_p0=True)
    out_path = Path("data") / "m3_funnel_conservation_summary.json"
    # Prefer m3_data_root if available
    try:
        from m3_data_root import data_path

        out_path = data_path("m3_funnel_conservation_summary.json")
    except Exception:
        pass
    keys = [
        "build",
        "snapshot_id",
        "conservation_ok",
        "IDENTITY_POPULATION",
        "TERMINAL_IDENTITY_STATUS",
        "SUM_TERMINAL_IDENTITIES",
        "DIFFERENCE_FROM_INPUT_IDENTITIES",
        "GOV_VALUE_STATUSES",
        "ACQUISITION_COST_STATUSES",
        "BOTH_SIDES_STATUSES",
        "UNIQUE_OPPORTUNITIES",
        "TERMINAL_OPPORTUNITY_STATUS",
        "SUM_TERMINAL_OPPORTUNITIES",
        "DIFFERENCE_FROM_INPUT_OPPORTUNITIES",
        "CONCENTRATION",
        "SOURCE_ATTRIBUTION",
        "HANDOFF_AUDIT",
        "SHALLOW_SEARCH",
        "BUGS",
        "FIXES_APPLIED",
        "CORRECTED_AFTER_FIXES",
        "MOST_IMPORTANT_ANSWERS",
    ]
    summary = {k: r.get(k) for k in keys}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print("CONSERVATION_OK", r.get("conservation_ok"), flush=True)
    print("WROTE", out_path, flush=True)
    print(
        "ID",
        r.get("SUM_TERMINAL_IDENTITIES"),
        "diff",
        r.get("DIFFERENCE_FROM_INPUT_IDENTITIES"),
        "OPP",
        r.get("SUM_TERMINAL_OPPORTUNITIES"),
        "diff",
        r.get("DIFFERENCE_FROM_INPUT_OPPORTUNITIES"),
        flush=True,
    )
    return 0 if r.get("conservation_ok") else 1


if __name__ == "__main__":
    sys.exit(main())
