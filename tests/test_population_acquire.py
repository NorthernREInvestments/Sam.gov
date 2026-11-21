from m3.research.population_acquire import prioritize_population, reconcile_population


def test_prioritize_never_drops():
    items = [
        {"external_key": f"k{i}", "requirement_class": "unknown_mixed" if i % 2 else "product_supply"}
        for i in range(50)
    ]
    out = prioritize_population(items, saved_keys={"k1"})
    assert len(out) == 50
    assert out[0]["external_key"] == "k1"


def test_reconcile_accounts_for_baseline():
    items = [{"solicitation_status": "open", "requirement_class": "product_supply"} for _ in range(10)]
    r = reconcile_population(items, baseline_count=9799)
    assert r["frozen_unique_count"] == 10
    assert r["delta_vs_baseline"] == 10 - 9799
