"""Funnel conservation audit tests."""

from funnel_conservation.models import BUILD, IDENTITY_TERMINALS, OPPORTUNITY_TERMINALS


def test_build_tag():
    assert BUILD.startswith("20261004-m3-funnel-conservation")


def test_terminal_lists_nonempty():
    assert len(IDENTITY_TERMINALS) >= 14
    assert len(OPPORTUNITY_TERMINALS) >= 10


def test_select_identities_full_no_soft_cap():
    from evidence_breakthrough.corpus import select_identities, load_identity_store

    store = load_identity_store()
    abc = 0
    for pack in (store.get("by_opportunity") or {}).values():
        for i in pack.get("identities") or []:
            if i.get("confidence_grade") in {"A", "B", "C"} and (
                i.get("part_number") or i.get("model") or i.get("sku") or i.get("nsn") or i.get("catalog_number")
            ):
                abc += 1
    full = select_identities(limit=max(abc, 5000), grades=("A", "B", "C"), full=True)
    capped = select_identities(limit=max(abc, 5000), grades=("A", "B", "C"), full=False)
    # full must not silently lose token-bearing A/B/C relative to soft-cap path
    assert len(full) >= len(capped)
    # remaining gap vs raw abc is explained by commercial-key dedupe, not soft-cap
    assert len(full) == len(capped)
    assert len(full) <= abc
    assert len(full) >= abc - 50  # allow modest dedupe collapse
