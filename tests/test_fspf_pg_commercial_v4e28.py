"""v4e28: PG/FSPF browser interleave + commercial price-pass wiring."""

from __future__ import annotations

import importlib


def test_builds_are_v4e31():
    fspf = importlib.import_module("buyer_intelligence.full_scale_production_funnel_v1")
    pg = importlib.import_module("bidnet_auth.purchasing_group_access_manager_v1")
    q = importlib.import_module("bidnet_auth.purchasing_group_queue")
    assert "v4e31" in fspf.BUILD
    assert "v1f" in pg.BUILD
    assert "v1g" in q.BUILD
    assert float(pg.BROWSER_SLOT_WAIT_S) >= 30.0


def test_resolve_form_url_preserves_accessed_element():
    q = importlib.import_module("bidnet_auth.purchasing_group_queue")
    dirty = (
        "https://www.bidnetdirect.com/private/supplier/9785456456/purchasing-groups/"
        "12555501/group-form/edit?accessedElementId=2718664187&originalAction=SOLICITATION_OPEN"
    )
    kept = q._resolve_form_url("12555501", dirty, strip_stale=False)
    assert kept and "accessedElementId=2718664187" in kept
    stripped = q._resolve_form_url("12555501", dirty, strip_stale=True)
    assert stripped and "accessedElementId=" not in stripped


def test_hammermill_known_catalog_price():
    gap = importlib.import_module("buyer_intelligence.commercial_gap_closure_v1")
    pr = gap.research_mpn_public_price(
        mpn="150010",
        manufacturer="Hammermill",
        description="Hammermill Copy Plus",
        opportunity_id="ctrl",
        deadline_s=5,
    )
    assert pr.get("unit_price") == "360.79"
    assert pr.get("route") == "KNOWN_CATALOG_REUSED"
    assert "elliot" in str(pr.get("supplier") or "").lower() or "elliottelectric" in str(
        pr.get("url") or ""
    ).lower()


def test_orphan_cleanup_skips_while_pg_holds_mutex(monkeypatch):
    life = importlib.import_module("bidnet_auth.auth_lifecycle_v1")
    monkeypatch.setattr(
        "bidnet_auth.browser_mutex_v1.browser_mutex_status",
        lambda: {
            "held": True,
            "holder": "pg-access-deadbeef",
            "remaining_s": 90.0,
        },
    )
    out = life.force_cleanup_orphaned_chromium(max_kill=4)
    assert out.get("killed") == 0
    assert out.get("skipped") == "pg_registration_holds_browser"


def test_price_research_maps_budget_exhausted(monkeypatch):
    cfe = importlib.import_module("buyer_intelligence.commercial_funnel_execution_v1")
    from public_price_search.models import PRICE_SEARCH_BUDGET_EXHAUSTED

    def fake_resolve(identity, **kwargs):
        return {
            "status": PRICE_SEARCH_BUDGET_EXHAUSTED,
            "evidence": {},
            "stop_reason": PRICE_SEARCH_BUDGET_EXHAUSTED,
            "failure_reason": PRICE_SEARCH_BUDGET_EXHAUSTED,
        }

    monkeypatch.setattr(
        "public_price_search.resolver.resolve_public_price",
        fake_resolve,
    )
    monkeypatch.setattr(
        "public_price_search.budget.ensure_budget",
        lambda minimum_remaining=40: {"ok": True},
    )

    out = cfe._research_public_prices_for_lines(
        opportunity_id="test-oid",
        lines=[
            {
                "DESCRIPTION": "Copy Paper Letter Size 20 lb White 5000 sheets case",
                "QTY": 10,
                "UOM": "CS",
            }
        ],
        budget_s=20.0,
        max_lines=1,
    )
    fails = out.get("failures") or []
    assert fails, out
    assert any(f.get("failure_class") == "SEARCH_BUDGET" for f in fails), fails


def test_strip_accessed_element_helper():
    reg = importlib.import_module("bidnet_auth.purchasing_group_registration")
    dirty = (
        "https://www.bidnetdirect.com/portal/group-form/edit"
        "?purchasingGroupId=8411151&accessedElementId=abc123"
    )
    clean = reg.strip_stale_accessed_element(dirty)
    assert clean and "accessedElementId" not in clean
    assert "8411151" in clean
