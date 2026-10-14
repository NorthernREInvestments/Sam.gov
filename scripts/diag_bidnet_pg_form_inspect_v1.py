"""Inspect Empire State purchasing-group registration form (read-only).

Opens Complete Form URL / group-form/edit. Does NOT fill or submit.
railway run -- python scripts/diag_bidnet_pg_form_inspect_v1.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_ID = "8411151"
PG_NAME = "Empire State Purchasing Group"
SOL_ID = "9797265001"
TITLE = "Copy Paper"


def _prep() -> None:
    raw = (os.environ.get("M3_DATA_ROOT") or "").strip()
    local = (ROOT / "data").resolve()
    if raw in {"/data", "/app/data"} or (raw and not Path(raw).exists() and os.name == "nt"):
        os.environ["M3_DATA_ROOT"] = str(local)


def main() -> int:
    _prep()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from m3_data_root import data_path

    cfg = load_bidnet_auth_config()
    client = BidNetAuthenticatedClient(cfg)
    t0 = time.time()
    try:
        auth = client.ensure_authenticated()
        if not auth.authenticated:
            print(json.dumps({"ok": False, "auth": auth.status}))
            return 1
        page = client._page
        assert page is not None

        # Navigate via known gate → capture Complete Form href, then open it
        resolved = client.resolve_detail_via_authenticated_search(
            title=TITLE,
            timeout_ms=25_000,
        )
        page.wait_for_timeout(1500)
        html = page.content() or ""
        m = re.search(
            r'id="interceptorProceedBtn"[^>]*data-href="([^"]+)"',
            html,
            re.I,
        )
        form_href = None
        if m:
            form_href = m.group(1).replace("&amp;", "&")
            if form_href.startswith("/"):
                form_href = "https://www.bidnetdirect.com" + form_href
        else:
            # Fallback construct from known pattern (supplier org id may vary)
            form_href = (
                f"https://www.bidnetdirect.com/private/supplier/purchasing-groups/"
                f"{PG_ID}/group-form/edit?accessedElementId={SOL_ID}&originalAction=SOLICITATION_OPEN"
            )

        page.goto(form_href, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        final = page.url or ""
        form_html = page.content() or ""
        sample = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "<script/>", form_html, flags=re.I)
        out = Path(data_path("_og_commercial_research_pipeline_out"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "m3_diag_pg_form_empire_state_v1.html").write_text(sample[:400_000], encoding="utf-8")

        fields = page.evaluate(
            """() => {
              const out = [];
              const nodes = document.querySelectorAll('input, select, textarea');
              for (const n of Array.from(nodes)) {
                const type = (n.getAttribute('type') || '').toLowerCase();
                if (type === 'hidden') continue;
                const labelEls = [];
                if (n.id) {
                  const lab = document.querySelector('label[for=\"'+n.id+'\"]');
                  if (lab) labelEls.push((lab.innerText||'').trim());
                }
                const parentLabel = n.closest('label');
                if (parentLabel) labelEls.push((parentLabel.innerText||'').trim().slice(0,120));
                const nearby = n.closest('.mets-field, .form-group, tr, .field') ;
                let nearbyLabel = '';
                if (nearby) {
                  const lf = nearby.querySelector('.mets-field-label, .field-label, th, label, .label');
                  if (lf) nearbyLabel = (lf.innerText||'').trim().slice(0,160);
                }
                const opts = [];
                if (n.tagName === 'SELECT') {
                  for (const o of Array.from(n.options).slice(0,30)) {
                    opts.push({value: o.value, text: (o.text||'').trim().slice(0,80)});
                  }
                }
                out.push({
                  tag: n.tagName,
                  type: type || n.tagName.toLowerCase(),
                  name: n.getAttribute('name') || '',
                  id: n.id || '',
                  required: !!n.required || (n.getAttribute('aria-required') === 'true'),
                  value: (n.value || '').slice(0,120),
                  placeholder: n.getAttribute('placeholder') || '',
                  label: (labelEls[0] || nearbyLabel || '').slice(0,160),
                  options: opts,
                  checked: !!n.checked,
                });
              }
              return out;
            }"""
        )

        # Fee / attestation signals
        body = re.sub(r"\s+", " ", page.locator("body").inner_text() or "")[:5000]
        signals = {
            "fee_mentioned": bool(re.search(r"\$\d|payment|credit card|purchase|fee\b|subscription", body, re.I)),
            "attestation_mentioned": bool(
                re.search(r"I (certify|attest|declare|agree)|under penalty|terms and conditions|binding", body, re.I)
            ),
            "captcha_mentioned": bool(re.search(r"captcha|recaptcha|hcaptcha", body, re.I)),
        }
        buttons = page.evaluate(
            """() => Array.from(document.querySelectorAll('button, input[type=submit], a.mets-command-button'))
              .map(n => ({
                tag: n.tagName,
                type: n.getAttribute('type')||'',
                id: n.id||'',
                text: (n.innerText||n.value||'').trim().replace(/\\s+/g,' ').slice(0,100),
                href: n.getAttribute('href')||'',
              })).slice(0,40)"""
        )

        report = {
            "ok": True,
            "auth_status": auth.status,
            "pg_id": PG_ID,
            "pg_name": PG_NAME,
            "solicitation_id": SOL_ID,
            "gate_navigation_ok": bool(resolved.get("ok")),
            "form_href_path": re.sub(r"https?://[^/]+", "", form_href or "")[:220],
            "final_url_path": re.sub(r"https?://[^/]+", "", final)[:220],
            "field_count": len(fields or []),
            "fields": fields,
            "buttons": buttons,
            "signals": signals,
            "body_excerpt": body[:2500],
            "elapsed_s": round(time.time() - t0, 2),
            "submitted": False,
            "filled": False,
        }
        (out / "m3_diag_pg_form_empire_state_v1.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        # Slim print — no secrets
        slim = {
            **{k: report[k] for k in report if k not in {"fields", "body_excerpt"}},
            "fields": [
                {
                    "name": f.get("name"),
                    "id": f.get("id"),
                    "type": f.get("type"),
                    "label": f.get("label"),
                    "required": f.get("required"),
                    "has_value": bool(f.get("value")),
                    "option_count": len(f.get("options") or []),
                }
                for f in (fields or [])
            ],
            "body_excerpt": body[:1200],
        }
        print(json.dumps(slim, indent=2))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
