"""Promote Phase G LIVE_SOURCE observations to FROZEN_REAL_SOURCE fixtures.

Does not alter facts. Does not invent READY. Stores retrieval metadata + titles.
"""

from __future__ import annotations

import json
from pathlib import Path

from validation_harness.models import SOURCE_TYPE_REAL_SOURCE, empty_case

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation_harness" / "cases" / "phase_g_live"
CAND = ROOT / "artifacts" / "phase_g" / "fixture_promotion_candidates.json"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    picks = json.loads(CAND.read_text(encoding="utf-8"))
    written = []
    for i, p in enumerate(picks, start=1):
        if p.get("fixture_tag") == "no_ready_path_observed":
            # Meta note only — not an opportunity fixture
            note_path = OUT / "G_NOTE_no_ready_path.json"
            note_path.write_text(json.dumps(p, indent=2), encoding="utf-8")
            written.append(str(note_path.name))
            continue
        case_id = f"G_{i:02d}_{p.get('fixture_tag')}"
        case = empty_case(
            case_id=case_id,
            case_name=str(p.get("title") or case_id)[:120],
            description=(
                f"Phase G LIVE_SOURCE frozen after supervised run. "
                f"Retrieved {p.get('listing_retrieval_timestamp')}. "
                f"Original provenance LIVE_SOURCE → stored as REAL_SOURCE_FIXTURE / FROZEN_REAL_SOURCE."
            ),
            source_type=SOURCE_TYPE_REAL_SOURCE,
            reality_class=SOURCE_TYPE_REAL_SOURCE,
            tags=["phase_g", "LIVE_PROMOTED", str(p.get("fixture_tag") or "")],
            row_overrides={
                "canonical_id": p.get("canonical_id"),
                "title": p.get("title"),
                "solicitation_number": p.get("solicitation_number"),
                "source_id": p.get("source") or "fed_sam_contract_opportunities",
                "ui_link": p.get("listing_url"),
                "deadline": p.get("deadline"),
                "phase_g_provenance_at_capture": "LIVE_SOURCE",
                "phase_g_frozen_as": "FROZEN_REAL_SOURCE",
                "live_retrieval_timestamp": p.get("listing_retrieval_timestamp"),
                "is_dla": p.get("is_dla"),
                "ready_for_owner_approval_observed": False,
                "execution_critical_blockers_observed": p.get("execution_critical_blockers") or [],
                "operator_next_action_observed": p.get("operator_next_action"),
            },
            expected={
                "ready_for_owner_approval": False,
                "phase_g_note": "Listing-only capture; must not be labeled LIVE_SOURCE in future runs",
            },
            expected_owner_readiness=False,
            notes="Promoted from Phase G live batch without altering facts. Not a READY path.",
        )
        path = OUT / f"{case_id}.json"
        path.write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
        written.append(path.name)
    manifest = {"kind": "PhaseGFrozenFixtures", "count": len(written), "files": written}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
