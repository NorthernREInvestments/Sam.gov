"""Diagnose Empire State form fill + validation (no claim of success).

railway run -- python scripts/diag_bidnet_pg_form_refill_v1.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
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
        extract_pg_gate_from_html,
        inspect_group_form,
        load_verified_company_answers,
        prepare_empire_state_answers,
    )
    from m3_data_root import data_path

    cfg = load_bidnet_auth_config()
    client = BidNetAuthenticatedClient(cfg)
    report: dict = {"kind": "PgFormRefillDiag"}
    try:
        auth = client.ensure_authenticated()
        report["auth"] = auth.status
        if not auth.authenticated:
            print(json.dumps(report))
            return 1
        resolved = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=25_000
        )
        client._page.wait_for_timeout(1200)
        html = client._page.content() or ""
        gate = extract_pg_gate_from_html(html, page_url=str(client._page.url or ""))
        form_url = (gate or {}).get("complete_form_url")
        report["form_url"] = (form_url or "")[:200]
        inspected = inspect_group_form(client, form_url=form_url)
        report["before"] = {
            "fields": inspected.get("fields"),
            "member_status_incomplete": inspected.get("member_status_incomplete"),
            "final_url": inspected.get("final_url"),
        }
        answers = load_verified_company_answers()
        plan = prepare_empire_state_answers(answers)
        filled = apply_fills(client, plan, answers=answers)
        report["applied"] = filled
        # Re-read field state
        after_vals = client._page.evaluate(
            """() => {
              const out = {};
              for (const n of Array.from(document.querySelectorAll('input, select, textarea'))) {
                const name = n.getAttribute('name') || n.id || '';
                if (!name || (n.type||'').toLowerCase()==='hidden') continue;
                if ((n.type||'').toLowerCase()==='radio') {
                  if (n.checked) out[name] = n.value || n.id;
                } else {
                  const v = (n.value||'').trim();
                  if (name.toLowerCase().includes('tax') || name.toLowerCase().includes('ein') || name.toLowerCase().includes('federal'))
                    out[name] = v ? ('LEN:'+v.length) : '';
                  else
                    out[name] = v;
                }
              }
              const errs = Array.from(document.querySelectorAll('.error, .field-error, .validation-error, .mets-error, .alert-danger, [class*=\"error\"]'))
                .map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,12);
              const body = (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,2500);
              return {values: out, errors: errs, has_incomplete: /\\bINCOMPLETE\\b/i.test(body),
                has_complete: /\\bCOMPLETE\\b/i.test(body) && !/\\bINCOMPLETE\\b/i.test(body)};
            }"""
        )
        report["after_fill"] = after_vals
        # Try Save and capture response
        try:
            client._page.locator("#saveButton").click(timeout=5_000)
            client._page.wait_for_timeout(3_000)
        except Exception as exc:
            report["save_error"] = type(exc).__name__
        post = client._page.evaluate(
            """() => {
              const errs = Array.from(document.querySelectorAll('.error, .field-error, .validation-error, .mets-error, .alert-danger, [class*=\"error\"], .feedback'))
                .map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,20);
              const body = (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,3000);
              const radios = {};
              for (const n of Array.from(document.querySelectorAll('input[type=radio]:checked'))) {
                radios[n.name] = n.id || n.value;
              }
              const tax = document.querySelector('#federalTaxPayerId, input[name=\"fields[federalTaxPayerId]\"]');
              return {
                url: location.href,
                errors: errs,
                radios,
                tax_len: tax && tax.value ? String(tax.value).length : 0,
                status_line: (body.match(/Member Since[^.]{0,80}/)||[''])[0],
                incomplete: /\\bINCOMPLETE\\b/i.test(body),
                body_snip: body.slice(0,1200),
              };
            }"""
        )
        report["after_save"] = post
    finally:
        try:
            client.close()
        except Exception:
            pass
    out = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_pg_form_refill_v1.json"))
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:5000])
    print("WROTE", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
