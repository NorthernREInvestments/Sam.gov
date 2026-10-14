"""Fill Empire State radios via JS, Save, retry solicitation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _prep() -> None:
    raw = (os.environ.get("M3_DATA_ROOT") or "").strip()
    local = (ROOT / "data").resolve()
    if raw in {"/data", "/app/data"} or (raw and not Path(raw).exists() and os.name == "nt"):
        os.environ["M3_DATA_ROOT"] = str(local)


def main() -> int:
    _prep()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.purchasing_group_registration import (
        apply_fills,
        load_verified_company_answers,
        prepare_empire_state_answers,
    )
    from m3_data_root import data_path

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    report: dict = {}
    try:
        auth = client.ensure_authenticated()
        report["auth"] = auth.status
        if not auth.authenticated:
            print(json.dumps(report))
            return 1
        form_url = (
            "https://www.bidnetdirect.com/private/supplier/9785456456/"
            "purchasing-groups/8411151/group-form/edit"
        )
        client._page.goto(form_url, wait_until="domcontentloaded", timeout=30_000)
        client._page.wait_for_timeout(1_500)
        report["form_url"] = str(client._page.url or "")[:200]
        answers = load_verified_company_answers()
        plan = prepare_empire_state_answers(answers)
        report["applied"] = apply_fills(client, plan, answers=answers)
        report["pre_save"] = client._page.evaluate(
            """() => {
              const radios = {};
              for (const n of document.querySelectorAll('input[type=radio]:checked'))
                radios[n.name] = n.id;
              const tax = document.querySelector('#federalTaxPayerId');
              return {radios, tax_len: tax && tax.value ? String(tax.value).length : 0};
            }"""
        )
        client._page.locator("#saveButton").click(timeout=5_000)
        client._page.wait_for_timeout(3_000)
        report["post_save"] = client._page.evaluate(
            """() => {
              const body = (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 1500);
              const radios = {};
              for (const n of document.querySelectorAll('input[type=radio]:checked'))
                radios[n.name] = n.id;
              return {
                url: location.href.slice(0, 200),
                radios,
                incomplete: /\\bINCOMPLETE\\b/i.test(body),
                failed: /Failed Check/i.test(body),
                status_snip: (body.match(/Member Since.{0,60}/) || [''])[0],
                body: body.slice(0, 900),
              };
            }"""
        )
        retry = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=20_000
        )
        report["retry"] = {
            "ok": retry.get("ok"),
            "access_gate": retry.get("access_gate"),
            "detail_url": (retry.get("detail_url") or "")[:160],
            "solicitation_id": retry.get("solicitation_id"),
        }
        if retry.get("ok"):
            try:
                report["retry"]["h1"] = (
                    client._page.locator("h1").first.inner_text(timeout=2_000) or ""
                )[:120]
            except Exception:
                report["retry"]["h1"] = None
    finally:
        try:
            client.close()
        except Exception:
            pass
    out = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_pg_radio_save_v1.json"))
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:4500])
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
