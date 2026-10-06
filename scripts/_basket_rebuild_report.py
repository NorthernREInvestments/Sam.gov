"""Recompute economics + rebuild Phase 33 report."""
from __future__ import annotations

import json

from basket_full_funnel_reconcile.economics import classify_basket_ready, compute_economics
from basket_full_funnel_reconcile.owner_ui import (
    derive_canonical_stage,
    owner_funnel_view,
    owner_next_action,
    what_can_hurt_us,
)
from basket_full_funnel_reconcile.sweep import build_final_report, format_report
from m3_data_root import data_path

ck = json.loads(data_path("m3_basket_full_funnel_reconcile_v1_checkpoint.json").read_text(encoding="utf-8"))
for oid, row in list((ck.get("opportunities") or {}).items()):
    lines = row.get("lines") or {}
    basket = classify_basket_ready(lines)
    corpus = row.get("corpus") or {}
    econ = compute_economics(oid, corpus, lines, basket)
    row["basket"] = basket
    row["economics"] = econ
    row["canonical_stage"] = derive_canonical_stage(row)
    row["next_action"] = owner_next_action(row)
    row["what_can_hurt_us"] = what_can_hurt_us(row, corpus)
    row["owner_view"] = owner_funnel_view(row)
    ck["opportunities"][oid] = row
    print(
        f"{oid}: cov={lines.get('line_coverage')} basket={basket.get('basket_class')} "
        f"term={econ.get('economic_terminal')} profit={econ.get('net_expected_profit')}"
    )

data_path("m3_basket_full_funnel_reconcile_v1_checkpoint.json").write_text(
    json.dumps(ck, indent=2, default=str), encoding="utf-8"
)
r = build_final_report(ck)
print(format_report(r))
print("BASKET_READY", r["BASKET_READY"])
print("ECONOMICS", r["ECONOMICS"])
print("PROFIT", r["PROFIT"])
print("COVERAGE", r["BASKET_COVERAGE"])
print("CONSERVATION", r["FUNNEL_CONSERVATION"]["pass"], r["FUNNEL_CONSERVATION"]["opportunity_diff"])
print("GOLDEN", r["GOLDEN_PATH"])
m = r["CONTROLLED_SCALE_TEST"]
print(
    "CONTROLLED",
    m.get("sample_size"),
    "acq",
    m.get("acquisition_ready"),
    "basket",
    m.get("basket_ready"),
    "econ",
    m.get("economics_ready"),
    "profitable",
    m.get("profitable"),
)
print("SCALE", r["SCALE_DECISION"])
print(json.dumps(r["FINAL_ANSWERS"], indent=2, default=str))
