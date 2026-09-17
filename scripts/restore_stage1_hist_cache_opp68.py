"""Restore Opp 68 historical Stage 1 cache overwritten by a test. Local only."""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from ai_analysis_cache import get_cached_analysis, put_cached_analysis  # noqa: E402
from ai_stage1 import HISTORICAL_STAGE1_FP_OPP68  # noqa: E402

RESULT = {
    "category": "LABOR_HEAVY",
    "buying": "Groundskeeping services at NUWC DETPAC Building 977",
    "quantity": None,
    "exact_model_identified": False,
    "install_required": False,
    "bond_likely": None,
    "license_likely": None,
    "channel_restriction_likely": True,
    "reseller_fit": "NONE",
    "execution_complexity": "HIGH",
    "fatal_issue": None,
    "advance": True,
    "reason_code": "NEEDS_STAGE2_DOCUMENT_REVIEW",
}

META = {
    "task": "screen_contract_text",
    "model": "gpt-5.6-luna",
    "notice_id": "dc6ac7432d224e06a44b6340db61442f",
    "schema": "stage1-compact-v2",
    "restored_after_test_overwrite": True,
    "original_cached_at": "2026-09-15T01:27:11.676137+00:00",
}


def main() -> int:
    ok = put_cached_analysis(
        HISTORICAL_STAGE1_FP_OPP68,
        result_text=json.dumps(RESULT, separators=(",", ":")),
        meta=META,
    )
    back = get_cached_analysis(HISTORICAL_STAGE1_FP_OPP68)
    print(
        json.dumps(
            {
                "restored": ok,
                "fingerprint": HISTORICAL_STAGE1_FP_OPP68,
                "buying": json.loads(back["result_text"])["buying"] if back else None,
                "meta": (back or {}).get("meta"),
            },
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
