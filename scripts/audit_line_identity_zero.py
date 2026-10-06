"""Audit why OpenGov line corpus produced zero exact/generic identities."""
from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path


def main() -> int:
    from m3_data_root import data_path

    path = data_path("m3_line_item_economics_store.json")
    print("loading", path, "mb=", round(path.stat().st_size / 1e6, 1), flush=True)
    store = json.loads(path.read_text(encoding="utf-8"))
    by_opp = store.get("by_opportunity") or {}
    print("opportunities", len(by_opp), flush=True)

    og = {k: v for k, v in by_opp.items() if str(k).startswith("opengov:")}
    print("opengov_opps", len(og), flush=True)

    id_c: Counter = Counter()
    src_c: Counter = Counter()
    null_desc = 0
    total = 0
    samples: list[dict] = []
    modelish: list[dict] = []
    csvish: list[dict] = []
    textish: list[dict] = []

    model_re = re.compile(r"[A-Za-z]*\d+[A-Za-z0-9\-./]{2,}")

    for oid, analysis in og.items():
        if not isinstance(analysis, dict):
            continue
        src = ((analysis.get("extraction") or {}).get("source_used") or "unknown")
        src_c[src] += 1
        for ln in analysis.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            total += 1
            ic = str(ln.get("identity_class") or "MISSING")
            id_c[ic] += 1
            desc = ln.get("product_description") or ln.get("original_text")
            if not desc:
                null_desc += 1
            rec = {
                "oid": oid[:40],
                "src": src,
                "ic": ic,
                "desc": (str(desc)[:120] if desc else None),
                "mfr": ln.get("manufacturer"),
                "model": ln.get("model"),
                "pn": ln.get("part_number"),
                "qty": ln.get("quantity"),
                "uom": ln.get("unit_of_measure"),
            }
            if len(samples) < 5000:
                samples.append(rec)
            blob = str(desc or "")
            if model_re.search(blob) and len(modelish) < 500:
                modelish.append(rec)
            if src == "csv_schedule" and len(csvish) < 500:
                csvish.append(rec)
            if src == "solicitation_body" and len(textish) < 500:
                textish.append(rec)

    rng = random.Random(42)
    rand100 = rng.sample(samples, min(100, len(samples)))

    def summarize(label: str, rows: list[dict]) -> dict:
        c = Counter(r["ic"] for r in rows)
        nulls = sum(1 for r in rows if not r["desc"])
        with_fields = sum(1 for r in rows if r["mfr"] or r["model"] or r["pn"])
        return {
            "n": len(rows),
            "identity": dict(c),
            "null_desc": nulls,
            "with_mfr_model_pn": with_fields,
            "examples": rows[:8],
        }

    report = {
        "opengov_opportunities": len(og),
        "total_lines": total,
        "null_description": null_desc,
        "null_desc_pct": round(null_desc / total, 4) if total else 0,
        "identity_classes": dict(id_c),
        "extraction_sources_by_opp": dict(src_c),
        "random_100": summarize("random", rand100),
        "modelish_100": summarize("modelish", modelish[:100]),
        "csv_100": summarize("csv", csvish[:100]),
        "text_100": summarize("text", textish[:100]),
        "hypotheses": [],
    }

    # Failure mode diagnosis
    if null_desc / max(1, total) > 0.5:
        report["hypotheses"].append("A/C: majority of lines have null description — CSV/column collapse likely")
    if id_c.get("UNKNOWN", 0) / max(1, total) > 0.9:
        report["hypotheses"].append("E/F: identity almost all UNKNOWN because fields empty")
    if src_c.get("csv_schedule", 0) > src_c.get("solicitation_body", 0):
        report["hypotheses"].append(
            "BUG: OpenGov handoff set csv_text=body when commas>20 — PDF text parsed as CSV"
        )
    if id_c.get("GENERIC_SPEC", 0) == 0 and id_c.get("EXACT_PART_NUMBER", 0) == 0:
        report["hypotheses"].append("I: classifier ran but produced no usable classes (input garbage)")

    out = data_path("PRODUCT_IDENTITY_AUDIT.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k not in {"random_100", "modelish_100", "csv_100", "text_100"}}, indent=2))
    print("SAMPLES_RANDOM", json.dumps(report["random_100"]["examples"][:5], indent=2))
    print("SAMPLES_CSV", json.dumps(report["csv_100"]["examples"][:5], indent=2))
    print("Wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
