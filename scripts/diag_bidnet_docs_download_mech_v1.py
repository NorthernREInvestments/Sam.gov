"""Inspect docs-items JS payload + Download Page / per-file download mechanics."""

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
    out: dict = {}
    try:
        auth = client.ensure_authenticated()
        out["auth"] = auth.status
        page = client._page
        assert page is not None and auth.authenticated

        docs_js = {"url": None, "body": None, "status": None}

        def _on_resp(resp) -> None:
            try:
                u = resp.url or ""
                if "docs-items" in u and resp.status == 200:
                    docs_js["url"] = u[:240]
                    docs_js["status"] = resp.status
                    docs_js["ct"] = str((resp.headers or {}).get("content-type") or "")[:80]
                    try:
                        docs_js["body"] = (resp.text() or "")[:12000]
                    except Exception:
                        docs_js["body"] = None
            except Exception:
                pass

        page.on("response", _on_resp)

        page.goto(
            f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view",
            wait_until="domcontentloaded",
            timeout=25_000,
        )
        page.wait_for_timeout(1_500)
        page.locator("a[href*='innerTabId=docs-items']").first.click(timeout=5_000)
        page.wait_for_timeout(3_000)

        # Parse docs-items body for IDs / filenames
        body = docs_js.get("body") or ""
        out["docs_js"] = {
            "url": docs_js.get("url"),
            "status": docs_js.get("status"),
            "ct": docs_js.get("ct"),
            "len": len(body),
            "filenames": re.findall(
                r"[\w ._-]+\.(?:pdf|docx?|xlsx?|xls|zip)", body, re.I
            )[:20],
            "ids": re.findall(r"(?:documentId|fileId|docId|attachmentId)[\"'=: ]+(\d+)", body, re.I)[
                :30
            ],
            "hrefs": re.findall(r"https?://[^\"'\\ ]+", body)[:20],
            "paths": re.findall(r"/private/[^\"'\\ ]+", body)[:30],
            "snip": re.sub(r"\s+", " ", body)[:2000],
        }

        # DOM structure of each document row after load
        structure = page.evaluate(
            """() => {
              const out = [];
              // Find elements containing known filenames
              const names = [
                'APPENDIX A', 'APPENDIX B', 'APPENDIX C', 'Bid_Documents', '.pdf', '.xls'
              ];
              const all = Array.from(document.querySelectorAll('tr, li, div, a, span'));
              for (const el of all) {
                const t = (el.innerText||'').trim();
                if (!/\\.(pdf|xls|xlsx|docx?)\\b/i.test(t)) continue;
                if (t.length > 300) continue;
                // prefer leaf-ish nodes
                const kids = el.querySelectorAll('a,button,input,i,[onclick]');
                out.push({
                  tag: el.tagName,
                  cls: (el.className||'').toString().slice(0,100),
                  id: el.id||'',
                  text: t.replace(/\\s+/g,' ').slice(0,160),
                  html: el.outerHTML.slice(0,500),
                  controls: Array.from(kids).slice(0,8).map(k => ({
                    tag: k.tagName,
                    t: (k.innerText||k.title||'').trim().slice(0,60),
                    href: k.getAttribute('href')||'',
                    onclick: (k.getAttribute('onclick')||'').slice(0,120),
                    name: k.getAttribute('name')||'',
                    type: k.getAttribute('type')||'',
                    value: (k.getAttribute('value')||'').slice(0,40),
                    cls: (k.className||'').toString().slice(0,80),
                    id: k.id||'',
                  })),
                });
                if (out.length >= 12) break;
              }
              const dlBtn = document.getElementById('downloadDocuments');
              return {
                rows: out,
                downloadPage: dlBtn ? {
                  tag: dlBtn.tagName, cls: dlBtn.className, disabled: !!dlBtn.disabled,
                  text: (dlBtn.innerText||'').trim()
                } : null,
              };
            }"""
        )
        out["structure"] = structure

        # Try Download Page once and capture download event
        downloads = []
        try:
            with page.expect_download(timeout=45_000) as dl_info:
                page.locator("#downloadDocuments").click(timeout=5_000)
            dl = dl_info.value
            src = dl.path()
            suggested = dl.suggested_filename or ""
            size = Path(src).stat().st_size if src else 0
            head = Path(src).read_bytes()[:20] if src else b""
            dest_dir = Path(data_path("bidnet_auth", "documents", SID))
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / re.sub(r"[^a-zA-Z0-9._-]+", "_", suggested or "package.bin")[:100]
            if src:
                Path(dest).write_bytes(Path(src).read_bytes())
            downloads.append(
                {
                    "suggested": suggested,
                    "size": size,
                    "url": (dl.url or "")[:200],
                    "head_hex": head.hex(),
                    "is_zip": head[:2] == b"PK",
                    "is_pdf": head.startswith(b"%PDF"),
                    "saved": str(dest) if src else None,
                }
            )
        except Exception as exc:
            downloads.append({"error": type(exc).__name__, "msg": str(exc)[:160]})

        out["download_page_attempt"] = downloads
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_docs_download_mech_v1.json"))
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    # Also save docs-js body
    if out.get("docs_js", {}).get("snip"):
        Path(data_path("_og_commercial_research_pipeline_out/m3_diag_docs_items_js_v1.txt")).write_text(
            (out.get("docs_js") or {}).get("snip") or "", encoding="utf-8"
        )
    print(json.dumps(out, indent=2, default=str)[:8000])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
