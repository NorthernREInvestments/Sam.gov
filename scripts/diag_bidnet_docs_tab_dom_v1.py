"""Bounded DOM/network diagnosis of BidNet Documents tab for 9797265001.

Does not download bulk files. Captures how docs are rendered and why scraper misses them.
railway run -- python scripts/diag_bidnet_docs_tab_dom_v1.py
"""

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
    from bidnet_auth.purchasing_group_tracker import get_group, upsert_group, STATUS_REGISTERED
    from m3_data_root import data_path

    # Reconcile tracker if missing
    g = get_group("8411151")
    if not g or g.get("status") != STATUS_REGISTERED:
        upsert_group(
            "8411151",
            name="Empire State Purchasing Group",
            status=STATUS_REGISTERED,
            relevant=True,
            confirmation_excerpt="reconcile_from_verified_completed",
        )

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    out: dict = {"sid": SID, "kind": "BidNetDocsTabDomDiag"}
    try:
        auth = client.ensure_authenticated()
        out["auth"] = {"status": auth.status, "authenticated": bool(auth.authenticated)}
        if not auth.authenticated:
            print(json.dumps(out, indent=2))
            return 1

        page = client._page
        assert page is not None

        # Capture XHR during docs tab load
        xhrs: list[dict] = []

        def _on_response(resp) -> None:
            try:
                u = resp.url or ""
                if any(
                    k in u.lower()
                    for k in ("doc", "file", "attach", "download", "solicitation", "abstract")
                ):
                    xhrs.append(
                        {
                            "url": u[:220],
                            "status": resp.status,
                            "ct": (resp.headers or {}).get("content-type", "")[:80],
                        }
                    )
            except Exception:
                pass

        page.on("response", _on_response)

        urls = [
            f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view",
            f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view&innerTabId=docs-items",
        ]
        for u in urls:
            page.goto(u, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(2_000)
            # try click docs tab if present
            try:
                tab = page.locator("a[href*='innerTabId=docs-items']")
                if tab.count():
                    tab.first.click(timeout=3_000)
                    page.wait_for_timeout(2_500)
            except Exception:
                pass

        final = str(page.url or "")
        # Existing scraper baseline
        discovered = client.discover_attachment_links(final, timeout_ms=12_000) or []

        dom = page.evaluate(
            """() => {
              const text = (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 4000);
              const tabs = Array.from(document.querySelectorAll('a[href*=\"innerTabId\"], [role=tab], .nav-tabs a, .mets-tabs a'))
                .map(a => ({t:(a.innerText||'').trim().slice(0,80), h:a.href||a.getAttribute('href')||''}))
                .slice(0, 20);
              // tables / lists that look like document rows
              const tables = Array.from(document.querySelectorAll('table')).map(t => ({
                id: t.id || '',
                cls: t.className || '',
                rows: t.querySelectorAll('tr').length,
                headers: Array.from(t.querySelectorAll('th')).map(th => (th.innerText||'').trim()).slice(0,8),
                sample: Array.from(t.querySelectorAll('tr')).slice(0,6).map(tr =>
                  Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText||'').trim().slice(0,60))
                ),
              })).filter(t => t.rows > 0).slice(0, 8);
              const anchors = Array.from(document.querySelectorAll('a[href], button, [onclick], [data-href], [data-url]'))
                .map(el => ({
                  tag: el.tagName,
                  t: (el.innerText||el.getAttribute('title')||'').trim().slice(0,100),
                  href: el.getAttribute('href') || '',
                  dataHref: el.getAttribute('data-href') || el.getAttribute('data-url') || '',
                  onclick: (el.getAttribute('onclick')||'').slice(0,120),
                  cls: (el.className||'').toString().slice(0,80),
                  id: el.id || '',
                }))
                .filter(x => /doc|file|download|pdf|attach|xls|zip|amend/i.test(
                  x.t+' '+x.href+' '+x.dataHref+' '+x.onclick+' '+x.cls+' '+x.id
                ))
                .slice(0, 40);
              const icons = Array.from(document.querySelectorAll('[class*=\"download\"], [class*=\"attachment\"], [class*=\"document\"], img[src*=\"pdf\"], i[class*=\"file\"]'))
                .map(el => ({tag: el.tagName, cls: (el.className||'').toString().slice(0,100), html: el.outerHTML.slice(0,200)}))
                .slice(0, 20);
              // iframe / shadow?
              const iframes = Array.from(document.querySelectorAll('iframe')).map(f => f.src).slice(0,5);
              // script JSON blobs mentioning documents
              const scripts = Array.from(document.querySelectorAll('script'))
                .map(s => s.textContent || '')
                .filter(t => /docs-items|documentId|fileId|attachment|solicitationDocuments/i.test(t))
                .map(t => t.slice(0, 500))
                .slice(0, 5);
              return {text_snip: text.slice(0, 1500), tabs, tables, anchors, icons, iframes, scripts};
            }"""
        )

        # Also try existing discover after forcing docs tab URL
        docs_url = (
            f"https://www.bidnetdirect.com/private/solicitations/{SID}/"
            f"abstract?target=view&innerTabId=docs-items"
        )
        page.goto(docs_url, wait_until="domcontentloaded", timeout=25_000)
        page.wait_for_timeout(3_000)
        html = page.content() or ""
        # Save sanitized HTML snippet for fixture (strip scripts partially)
        sample = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "<script/>", html, flags=re.I)
        out_dir = Path(data_path("_og_commercial_research_pipeline_out"))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "m3_diag_docs_tab_dom_v1.html").write_text(sample[:500_000], encoding="utf-8")

        # Regex hunt in HTML for file-ish patterns
        patterns = {
            "fileId": re.findall(r"fileId[=:][\"']?(\d+)", html, re.I)[:20],
            "documentId": re.findall(r"documentId[=:][\"']?(\d+)", html, re.I)[:20],
            "download_paths": re.findall(
                r"[\"']([^\"']*(?:download|attachment|getFile|docs?)[^\"']{0,120})[\"']",
                html,
                re.I,
            )[:30],
            "pdf_mentions": len(re.findall(r"\.pdf", html, re.I)),
            "docs_items": "docs-items" in html,
        }

        out.update(
            {
                "final_url": final[:200],
                "docs_url": docs_url,
                "existing_discover_count": len(discovered),
                "existing_discover": [
                    {
                        "name": (d.get("document_name") or d.get("name") or "")[:100],
                        "url": str(d.get("document_url") or d.get("url") or "")[:160],
                    }
                    for d in discovered[:10]
                ],
                "dom": dom,
                "html_patterns": {
                    k: (v[:15] if isinstance(v, list) else v) for k, v in patterns.items()
                },
                "xhr_sample": xhrs[-40:],
                "empire_tracker": (get_group("8411151") or {}).get("status"),
            }
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_docs_tab_dom_v1.json"))
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str)[:6000])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
