"""Open abstract (no docs URL), click Documents tab, capture DOM + network."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SID = "9797265001"


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

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    out: dict = {"sid": SID}
    try:
        auth = client.ensure_authenticated()
        out["auth"] = auth.status
        if not auth.authenticated:
            print(json.dumps(out))
            return 1
        page = client._page
        assert page is not None

        xhrs: list[dict] = []

        def _on_resp(resp) -> None:
            try:
                u = resp.url or ""
                low = u.lower()
                if any(
                    k in low
                    for k in (
                        "doc",
                        "file",
                        "attach",
                        "download",
                        "abstract",
                        "solicitation",
                        "inner",
                    )
                ) and not any(x in low for x in (".css", ".js?", "/jawr/", "sharethis", "google")):
                    body_snip = ""
                    ct = str((resp.headers or {}).get("content-type") or "")
                    if "json" in ct.lower() and resp.status == 200:
                        try:
                            body_snip = (resp.text() or "")[:800]
                        except Exception:
                            pass
                    xhrs.append(
                        {
                            "url": u[:240],
                            "status": resp.status,
                            "ct": ct[:80],
                            "json_snip": body_snip,
                        }
                    )
            except Exception:
                pass

        page.on("response", _on_resp)

        # Via search-to-detail (proven path)
        resolved = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=25_000
        )
        page.wait_for_timeout(1_500)
        out["resolved"] = {
            "ok": resolved.get("ok"),
            "access_gate": resolved.get("access_gate"),
            "url": (resolved.get("detail_url") or page.url or "")[:180],
            "sid": resolved.get("solicitation_id"),
        }

        # Ensure on abstract without docs-items query (that 404'd when navigated cold)
        abstract = f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view"
        page.goto(abstract, wait_until="domcontentloaded", timeout=25_000)
        page.wait_for_timeout(2_000)
        before = re.sub(r"\s+", " ", page.locator("body").inner_text() or "")[:500]
        out["before_click"] = {
            "url": page.url[:180],
            "has_docs_label": bool(re.search(r"Documents\s*\(\s*4\s*\)", before, re.I)),
            "snip": before[:300],
        }

        # Click Documents tab carefully — prefer href containing docs-items
        clicked = False
        click_err = None
        try:
            loc = page.locator("a[href*='innerTabId=docs-items']")
            if loc.count():
                loc.first.click(timeout=5_000)
                clicked = True
            else:
                page.get_by_text(re.compile(r"Documents\s*\(\s*\d+\s*\)", re.I)).first.click(
                    timeout=5_000
                )
                clicked = True
        except Exception as exc:
            click_err = type(exc).__name__
        page.wait_for_timeout(3_500)

        after_url = str(page.url or "")
        body = re.sub(r"\s+", " ", page.locator("body").inner_text() or "")[:2500]
        html = page.content() or ""

        # Extract DOWNLOAD_DOCUMENT context
        dl_ctx = []
        for m in re.finditer(r".{0,80}DOWNLOAD_DOCUMENT.{0,120}", html):
            dl_ctx.append(re.sub(r"\s+", " ", m.group(0))[:200])
        # look for doc rows in HTML
        row_patterns = {
            "doc_row_class": len(re.findall(r"document-row|docs-item|solicitationDoc", html, re.I)),
            "mets_file": len(re.findall(r"mets-file|file-name|docName", html, re.I)),
            "checkbox_docs": len(re.findall(r"name=[\"']docs?|documentIds", html, re.I)),
        }

        dom = page.evaluate(
            """() => {
              const rows = [];
              // try common row containers
              const sels = [
                '#docs-items tr', '#docs-items li', '[id*=\"docs\"] tr', '[id*=\"docs\"] li',
                'table.documents tr', '.document-list tr', '.document-list li',
                '[class*=\"document\"] tr', '[class*=\"docs-item\"]',
                'tr[data-document-id]', 'tr[data-file-id]',
              ];
              for (const sel of sels) {
                for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 20)) {
                  const t = (el.innerText||'').trim().replace(/\\s+/g,' ').slice(0,200);
                  if (t.length < 3) continue;
                  const a = el.querySelector('a[href], button, [onclick], [data-href]');
                  rows.push({
                    sel, t,
                    href: a ? (a.getAttribute('href')||'') : '',
                    data: a ? (a.getAttribute('data-href')||a.getAttribute('data-url')||'') : '',
                    onclick: a ? (a.getAttribute('onclick')||'').slice(0,150) : '',
                    html: el.outerHTML.slice(0,300),
                  });
                }
                if (rows.length) break;
              }
              // all visible text lines mentioning pdf/doc/xls
              const lines = (document.body.innerText||'').split('\\n')
                .map(s => s.trim()).filter(s => /\\.(pdf|docx?|xlsx?|zip)|addend|amend|spec|bid|invitation|instruction/i.test(s))
                .slice(0, 30);
              // download buttons
              const dls = Array.from(document.querySelectorAll('a,button,[role=button]'))
                .filter(el => /download/i.test((el.innerText||'')+(el.title||'')+(el.className||'')))
                .map(el => ({
                  t: (el.innerText||el.title||'').trim().slice(0,80),
                  href: el.getAttribute('href')||'',
                  id: el.id||'',
                  cls: (el.className||'').toString().slice(0,80),
                })).slice(0, 20);
              return {rows: rows.slice(0,15), lines, dls, activeTab: (document.querySelector('.active,[aria-selected=true]')||{}).innerText||''};
            }"""
        )

        out_dir = Path(data_path("_og_commercial_research_pipeline_out"))
        out_dir.mkdir(parents=True, exist_ok=True)
        sample = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "<script/>", html, flags=re.I)
        (out_dir / "m3_diag_docs_tab_click_v1.html").write_text(sample[:500_000], encoding="utf-8")

        out.update(
            {
                "clicked": clicked,
                "click_err": click_err,
                "after_url": after_url[:200],
                "is_404": "404" in body or "cannot be found" in body.lower(),
                "body_snip": body[:1200],
                "download_document_ctx": dl_ctx[:10],
                "row_patterns": row_patterns,
                "dom": dom,
                "xhr": xhrs[-30:],
            }
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_docs_tab_click_v1.json"))
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str)[:7000])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
