"""Unit tests for Full-100 throughput (no live network)."""

from full100_throughput.models import BUILD, KNOWN_MISSES, TERMINAL, PRICE_FOUND, NO_PRICE_EXHAUSTIVE
from full100_throughput.domain_intel import domain_yield_score, rank_domains, seed_from_prior_adapters
from full100_throughput.category_map import seller_candidates_for_item


def test_build_tag():
    assert BUILD == "20261004-m3-full100-throughput-v1"


def test_app_build():
    from app import APP_BUILD_VERSION

    assert APP_BUILD_VERSION.startswith("2026") and "m3-" in APP_BUILD_VERSION


def test_terminal_statuses():
    assert PRICE_FOUND in TERMINAL
    assert NO_PRICE_EXHAUSTIVE in TERMINAL
    assert "ERROR" not in TERMINAL
    assert "BLOCKED_RETRYABLE" not in TERMINAL


def test_known_misses():
    ids = {m["id"] for m in KNOWN_MISSES}
    assert "easy-makita-b-45580" in ids
    assert "easy-fleetguard-ff63009" in ids
    assert len(KNOWN_MISSES) == 5


def test_domain_intel_ranks_high_yield_first():
    seed_from_prior_adapters()
    ranked = rank_domains(["grainger.com", "quill.com", "dieselpartsdirect.com"])
    assert ranked[0] in {"quill.com", "dieselpartsdirect.com"}
    assert domain_yield_score("quill.com") > domain_yield_score("grainger.com")


def test_category_map_returns_sellers():
    rows = seller_candidates_for_item(
        {"mpn": "LF9009", "manufacturer": "Fleetguard", "category": "automotive_heavy"},
        limit=4,
    )
    assert rows
    assert any("dieselpartsdirect" in d for d, _ in rows)


def test_format_report_smoke():
    from full100_throughput.sweep import format_completion_report

    text = format_completion_report(
        {
            "full100_completion": {
                "benchmark_items": 82,
                "confirmed_publicly_priceable": 82,
                "previously_completed": 20,
                "resumed": 62,
                "newly_processed": 62,
                "final_completed": 82,
                "unfinished": 0,
            },
            "full100": {"priced": 66, "coverage": 80.5, "accuracy": 96.0, "pass": True},
            "easy25": {"coverage": 80.0, "accuracy": 100.0, "pass": True},
            "throughput": {},
            "domain_performance": {},
            "top_high_yield_domains": [],
            "alternate_seller": {},
            "known_misses": [],
            "remaining_misses": [],
            "most_important_answers": {"1_full100_finished_all_82": True},
            "scale_safe": False,
            "projected_scale": {},
        }
    )
    assert "FULL-100 COMPLETION" in text
    assert "SAFE_TO_SCALE" in text
