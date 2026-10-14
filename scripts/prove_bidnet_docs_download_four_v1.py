"""Prove download of all 4 Copy Paper docs via attachment-download links."""

from __future__ import annotations

import hashlib
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


def _kind(raw: bytes) -> str:
    if raw.startswith(b"%PDF"):
        return "pdf"
    if raw[:2] == b"PK":
        return "zip_or_xlsx"
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    if raw.lstrip().lower().startswith(b"<!doctype") or raw.lstrip().lower().startswith(b"<html"):
        return "html"
    return "bin"


def main() -> int:
    _prep()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from m3_data_root import data_path

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    report: dict = {"sid": SID, "docs": []}
    try:
        auth = client.ensure_authenticated()
        report["auth"] = auth.status
        page = client._page
        assert page is not None and auth.authenticated

        page.goto(
            f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view",
            wait_until="domcontentloaded",
            timeout=25_000,
        )
        page.wait_for_timeout(1_200)
        page.locator("a[href*='innerTabId=docs-items']").first.click(timeout=5_000)
        page.wait_for_selector(
            "table.solicitationDocumentsTable a[href*='attachment-download']",
            timeout=10_000,
        )
        page.wait_for_timeout(800)

        links = page.evaluate(
            """() => Array.from(document.querySelectorAll(
              'table.solicitationDocumentsTable a[href*=\"attachment-download\"]'
            )).map(a => ({name: (a.innerText||'').trim(), href: a.getAttribute('href')||'', id: a.id||''}))"""
        )
        report["identified"] = links

        out_dir = Path(data_path("bidnet_auth", "documents", SID))
        out_dir.mkdir(parents=True, exist_ok=True)

        for item in links or []:
            name = item.get("name") or "doc"
            href = item.get("href") or ""
            if href.startswith("/"):
                href = "https://www.bidnetdirect.com" + href
            row: dict = {"name": name, "href": href[:200]}

            # Method A: browser download event via click
            try:
                with page.expect_download(timeout=30_000) as dl_info:
                    page.locator(f"a[href='{item.get('href')}']").first.click(timeout=5_000)
                dl = dl_info.value
                src = dl.path()
                raw = Path(src).read_bytes() if src else b""
                row["method"] = "expect_download"
                row["suggested"] = dl.suggested_filename
                row["size"] = len(raw)
                row["kind"] = _kind(raw)
                row["sha256_16"] = hashlib.sha256(raw).hexdigest()[:16] if raw else None
                if raw and row["kind"] != "html":
                    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:100]
                    dest = out_dir / safe
                    dest.write_bytes(raw)
                    row["saved"] = str(dest)
                    row["ok"] = True
                else:
                    row["ok"] = False
                    row["error"] = "html_or_empty"
            except Exception as exc:
                row["method_a_error"] = type(exc).__name__
                # Method B: authenticated client download_bytes / fetch
                try:
                    if hasattr(client, "download_authenticated"):
                        result = client.download_authenticated(href)
                    else:
                        # use page request context (shares cookies)
                        resp = page.request.get(href, timeout=30_000)
                        result = {
                            "ok": resp.ok,
                            "status": resp.status,
                            "body": resp.body(),
                            "headers": resp.headers,
                        }
                    body = result.get("body") if isinstance(result, dict) else None
                    if body is None and isinstance(result, (bytes, bytearray)):
                        body = bytes(result)
                    raw = bytes(body or b"")
                    row["method"] = "request_get"
                    row["http_status"] = result.get("status") if isinstance(result, dict) else None
                    row["size"] = len(raw)
                    row["kind"] = _kind(raw)
                    row["sha256_16"] = hashlib.sha256(raw).hexdigest()[:16] if raw else None
                    if raw and row["kind"] != "html" and len(raw) > 64:
                        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:100]
                        dest = out_dir / safe
                        dest.write_bytes(raw)
                        row["saved"] = str(dest)
                        row["ok"] = True
                    else:
                        row["ok"] = False
                        row["error"] = "html_or_empty_via_request"
                        row["head"] = raw[:80].decode("utf-8", errors="replace")
                except Exception as exc2:
                    row["ok"] = False
                    row["method_b_error"] = type(exc2).__name__
                    row["method_b_msg"] = str(exc2)[:160]

            # Re-open docs tab if navigation left it
            if "abstract" not in (page.url or "") or "docs" not in (page.content() or "")[:5000]:
                page.goto(
                    f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view",
                    wait_until="domcontentloaded",
                    timeout=25_000,
                )
                page.wait_for_timeout(800)
                try:
                    page.locator("a[href*='innerTabId=docs-items']").first.click(timeout=5_000)
                    page.wait_for_selector(
                        "table.solicitationDocumentsTable a[href*='attachment-download']",
                        timeout=10_000,
                    )
                except Exception:
                    pass

            report["docs"].append(row)

        report["downloaded_ok"] = sum(1 for d in report["docs"] if d.get("ok"))
        report["identified_count"] = len(links or [])
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_prove_docs_download_four_v1.json"))
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:6000])
    print("WROTE", path)
    return 0 if report.get("downloaded_ok") == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
