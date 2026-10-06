"""OpenGov cascade / route resolver unit tests."""

from __future__ import annotations


def test_route_resolver_candidates_skip_cooled(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from opengov_discovery.route_resolver import (
        ROUTE_PUBLIC_EMBED,
        ROUTE_PUBLIC_STRUCTURED,
        OpenGovRouteResolver,
    )

    portal = {
        "entity_name": "Test City",
        "portal_url": "https://procurement.opengov.com/portal/testcity",
    }
    r = OpenGovRouteResolver()
    cands = r.candidate_urls(portal)
    kinds = [k for k, _ in cands]
    assert ROUTE_PUBLIC_STRUCTURED in kinds
    assert ROUTE_PUBLIC_EMBED in kinds

    structured = [u for k, u in cands if k == ROUTE_PUBLIC_STRUCTURED][0]
    r.mark_blocked(portal, route_kind=ROUTE_PUBLIC_STRUCTURED, route_url=structured, reason="ANTI_BOT")
    cands2 = r.candidate_urls(portal)
    # Kind-scoped cooldown: public structured blocked, but AUTH may still try same URL
    assert structured not in {u for k, u in cands2 if k == ROUTE_PUBLIC_STRUCTURED}
    assert any(k == "AUTHENTICATED_SESSION_REQUEST" for k, _ in cands2)


def test_route_resolver_mark_success_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from opengov_auth.states import WORKING_EMBED
    from opengov_discovery.route_resolver import ROUTE_PUBLIC_EMBED, OpenGovRouteResolver

    portal = {
        "entity_name": "Embed City",
        "portal_url": "https://procurement.opengov.com/portal/embedcity",
    }
    r = OpenGovRouteResolver()
    url = "https://procurement.opengov.com/portal/embed/embedcity/project-list"
    r.mark_success(portal, route_kind=ROUTE_PUBLIC_EMBED, route_url=url, status=WORKING_EMBED, retrieved=3)
    ent = r.get_entity(portal)
    assert ent["working_route"] == ROUTE_PUBLIC_EMBED
    assert ent["working_route_url"] == url
    assert ent["status"] == WORKING_EMBED
    assert r.candidate_urls(portal)[0][1] == url


def test_public_data_client_hydration():
    from opengov_discovery.parse import parse_opengov_json_payload
    from opengov_discovery.public_data_client import extract_hydration_payloads

    html = """
    <html><script type="application/json">
    {"projects":[{"title":"Road Salt Bid FY26","projectNumber":"RS-1","publicUrl":"/portal/x/projects/1"}]}
    </script></html>
    """
    payloads = extract_hydration_payloads(html)
    assert payloads
    rows = parse_opengov_json_payload(payloads[0], list_url="https://example.com", agency="X")
    assert any("Road Salt" in (r.get("title") or "") for r in rows)


def test_parse_project_public_rows_shape():
    from opengov_discovery.parse import parse_opengov_json_payload

    payload = {
        "count": 2,
        "rows": [
            {
                "id": 101,
                "title": "City Fleet Tires FY26",
                "status": "open",
                "proposalDeadline": "2026-12-01T17:00:00.000Z",
                "releaseProjectDate": "2026-09-01T12:00:00.000Z",
                "summary": "Tire supply",
                "addendums": [{"id": 1}],
                "government": {
                    "code": "orlando",
                    "organization": {"name": "City of Orlando", "city": "Orlando"},
                },
            }
        ],
    }
    rows = parse_opengov_json_payload(
        payload,
        list_url="https://api.procurement.opengov.com/api/v1/government/orlando/project/public",
        agency="City of Orlando",
    )
    assert len(rows) == 1
    assert rows[0]["title"].startswith("City Fleet")
    assert "orlando" in (rows[0].get("detail_url") or "")
    assert rows[0]["raw_metadata"]["has_addenda"] is True


def test_route_resolver_prefers_project_public(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from opengov_discovery.route_resolver import ROUTE_PUBLIC_STRUCTURED, OpenGovRouteResolver

    portal = {
        "entity_name": "City of Orlando",
        "portal_url": "https://procurement.opengov.com/portal/orlando",
        "government_code": "orlando",
    }
    cands = OpenGovRouteResolver().candidate_urls(portal)
    structured = [u for k, u in cands if k == ROUTE_PUBLIC_STRUCTURED]
    assert structured
    assert structured[0].endswith("/government/orlando/project/public")


def test_anti_bot_not_in_success_statuses():
    from opengov_auth.states import ANTI_BOT, RECOVERY_BLOCKED, SUCCESS_STATUSES, WORKING_EMBED

    assert ANTI_BOT not in SUCCESS_STATUSES
    assert RECOVERY_BLOCKED not in SUCCESS_STATUSES
    assert WORKING_EMBED in SUCCESS_STATUSES


def test_cascade_import():
    from opengov_discovery import OpenGovRouteResolver, run_opengov_cascade_discovery, run_opengov_route_map

    assert callable(run_opengov_cascade_discovery)
    assert callable(run_opengov_route_map)
    assert OpenGovRouteResolver is not None
