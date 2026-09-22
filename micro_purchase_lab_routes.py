"""FastAPI routes for Micro-Purchase Economics Lab."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException


def register_micro_purchase_lab_routes(app: FastAPI) -> None:
    @app.get("/api/m3/micro-purchase-lab/config")
    def api_mpl_config():
        from micro_purchase_lab_config import threshold_config

        return threshold_config()

    @app.get("/api/m3/micro-purchase-lab/queue")
    def api_mpl_queue(limit: int = 40):
        from micro_purchase_lab_service import build_test_queue

        return build_test_queue(limit=max(1, min(limit, 100)))

    @app.get("/api/m3/micro-purchase-lab/tests")
    def api_mpl_tests(classification: str | None = None, status: str | None = None):
        from micro_purchase_lab_service import list_tests

        rows = list_tests(classification=classification, status=status)
        return {"count": len(rows), "tests": rows}

    @app.get("/api/m3/micro-purchase-lab/tests/{test_id}")
    def api_mpl_get_test(test_id: str):
        from micro_purchase_lab_service import get_test

        row = get_test(test_id)
        if not row:
            raise HTTPException(status_code=404, detail="test not found")
        return row

    @app.post("/api/m3/micro-purchase-lab/tests")
    def api_mpl_create_test(body: dict):
        from micro_purchase_lab_service import create_manual_test, save_test

        if body.get("id"):
            return save_test(body)
        return create_manual_test(body or {})

    @app.put("/api/m3/micro-purchase-lab/tests/{test_id}")
    def api_mpl_update_test(test_id: str, body: dict):
        from micro_purchase_lab_service import save_test

        payload = dict(body or {})
        payload["id"] = test_id
        return save_test(payload)

    @app.post("/api/m3/micro-purchase-lab/tests/{test_id}/duplicate")
    def api_mpl_duplicate_test(test_id: str):
        from micro_purchase_lab_economics import enrich_test
        from micro_purchase_lab_store import MicroPurchaseLabStore

        row = MicroPurchaseLabStore().duplicate(test_id)
        if not row:
            raise HTTPException(status_code=404, detail="test not found")
        return enrich_test(row)

    @app.post("/api/m3/micro-purchase-lab/tests/{test_id}/archive")
    def api_mpl_archive_test(test_id: str):
        from micro_purchase_lab_service import get_test, save_test

        row = get_test(test_id)
        if not row:
            raise HTTPException(status_code=404, detail="test not found")
        row["archived"] = True
        return save_test(row)

    @app.post("/api/m3/micro-purchase-lab/load-opportunity")
    def api_mpl_load_opportunity(body: dict):
        from micro_purchase_lab_service import load_from_opportunity

        cid = str((body or {}).get("canonical_id") or "").strip()
        if not cid:
            raise HTTPException(status_code=400, detail="canonical_id required")
        try:
            return load_from_opportunity(cid)
        except KeyError:
            raise HTTPException(status_code=404, detail="opportunity not found")

    @app.get("/api/m3/micro-purchase-lab/search-opportunities")
    def api_mpl_search_opps(q: str = "", limit: int = 25):
        from micro_purchase_lab_service import search_opportunities

        rows = search_opportunities(q, limit=max(1, min(limit, 50)))
        return {"count": len(rows), "opportunities": rows}

    @app.get("/api/m3/micro-purchase-lab/dashboard")
    def api_mpl_dashboard():
        from micro_purchase_lab_service import experiment_dashboard

        return experiment_dashboard()

    @app.get("/api/m3/micro-purchase-lab/tests/{test_id}/quote-request")
    def api_mpl_quote_request(test_id: str):
        from micro_purchase_lab_service import quote_request_for

        try:
            return quote_request_for(test_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="test not found")

    @app.post("/api/m3/micro-purchase-lab/tests/{test_id}/calculate")
    def api_mpl_calculate(test_id: str, body: dict | None = None):
        from micro_purchase_lab_service import get_test, save_test

        row = get_test(test_id)
        if not row:
            raise HTTPException(status_code=404, detail="test not found")
        if body:
            row.update(body)
        return save_test(row)
