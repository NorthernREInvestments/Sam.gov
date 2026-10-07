"""SAME-20 full pipeline recovery — corpus freeze, cache, timeout, UI blockers."""

from __future__ import annotations

from bidnet_engine.same20_corpus import (
    BASELINE_ITER23,
    SAME20_STABLE_KEYS,
    SOURCE_JOB,
    SOURCE_RUN,
    freeze_corpus_from_report,
)
from bidnet_engine.same20_full_pipeline_recovery import _blocker_bucket, _enrich_result, BUILD
from bidnet_engine.schedule_recovery_canary import HEARTBEAT_INTERVAL_S, OPP_HARD_TIMEOUT_S


def test_corpus_frozen_exactly_20_keys():
    assert len(SAME20_STABLE_KEYS) == 20
    assert len(set(SAME20_STABLE_KEYS)) == 20
    assert SOURCE_JOB == "ASR-5448d09c35f1"
    assert SOURCE_RUN == "ASR-20261007182448"


def test_freeze_corpus_no_substitution():
    report = {
        "top_recovered": [
            {
                "opportunity": SAME20_STABLE_KEYS[0],
                "title": "Annual Water Material Purchase Contract 2027",
                "buyer": "California",
                "extracted_lines": 15,
                "product_schedule": "Found",
            }
        ],
        "unrecovered_cases": [
            {"opportunity": sk, "title": f"T{i}", "classification": "PRODUCT_SCHEDULE_INACCESSIBLE"}
            for i, sk in enumerate(SAME20_STABLE_KEYS[1:], start=2)
        ],
    }
    payload = freeze_corpus_from_report(report, {})
    assert payload["corpus_size"] == 20
    assert payload["stable_keys"] == SAME20_STABLE_KEYS
    assert payload["substitute_forbidden"] is True
    assert payload["baseline"]["VALID_LOCAL_PACKAGES"] == BASELINE_ITER23["VALID_LOCAL_PACKAGES"]


def test_hard_timeout_and_heartbeat_constants():
    assert OPP_HARD_TIMEOUT_S == 300
    assert HEARTBEAT_INTERVAL_S == 60


def test_enrich_result_plain_blocker_and_decision():
    r = _enrich_result(
        {
            "raw_lines": 0,
            "material_lines": 0,
            "usable_ae": 0,
            "public_prices": 0,
            "package_materialization": {
                "product_classification": "PRODUCT_SCHEDULE_INACCESSIBLE",
                "PACKAGE_DOCUMENT_COUNT_MATERIALIZED": 0,
                "AUTHORITATIVE_PRODUCT_DOC_FOUND": False,
            },
        }
    )
    assert "membership" in (r.get("blocker_plain") or "").lower() or "BidNet" in (r.get("blocker_plain") or "")
    assert r.get("decision") == "INSUFFICIENT_EVIDENCE"
    assert _blocker_bucket(r) == "PACKAGE_INCOMPLETE"


def test_build_id():
    assert BUILD == "20261007-m3-same20-full-pipeline-recovery-v1"


def test_statewide_namespace_guard_in_materialization():
    from bidnet_engine.package_materialization import (
        PATCH,
        is_statewide_bidnet_url,
        resolve_bidnet_private_detail_url_candidates,
    )

    assert "playwright-same-thread" in PATCH or "statewide" in PATCH
    sw = "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/444169970078/abstract"
    assert is_statewide_bidnet_url(sw)
    assert resolve_bidnet_private_detail_url_candidates(sw) == []


def test_process_opp_guards_runs_on_calling_thread():
    """Regression: must not hand Playwright client to a worker thread."""
    import inspect
    import re

    from bidnet_engine import schedule_recovery_canary as src

    src_text = inspect.getsource(src._process_opp_with_guards)
    # Strip comments before checking for banned executor usage
    code_only = re.sub(r"#.*", "", src_text)
    assert "ThreadPoolExecutor" not in code_only
    assert "pool.submit" not in code_only
    assert "process_money_opportunity(" in code_only
