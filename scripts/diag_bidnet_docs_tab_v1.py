"""Click Documents tab on unlocked abstract and list files."""

from __future__ import annotations

import json
import os
import re
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
    from m3_data_root import data_path

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    out: dict = {}
    try:
        auth = client.ensure_authenticated()
        out["auth"] = auth.status
        url = (
            "https://www.bidnetdirect.com/private/solicitations/9797265001/"
            "abstract?target=view&innerTabId=docs-items"
        )
        client._page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        client._page.wait_for_timeout(2_000)
        clicked = True
        final = str(client._page.url or "")
        body = re.sub(r"\s+", " ", client._page.locator("body").inner_text() or "")[:2000]
        hrefs = client._page.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
              t: (a.innerText||'').trim().slice(0,100),
              h: a.href
            })).filter(x => /pdf|doc|xls|download|attachment|\\.zip|file/i.test(x.t+' '+x.h)).slice(0,25)"""
        )
        docs = client.discover_attachment_links(final, timeout_ms=10_000) or []
        out.update(
            {
                "clicked": clicked,
                "final": final[:180],
                "hrefs": hrefs,
                "discover_count": len(docs),
                "discover": [
                    (d.get("document_name") or d.get("name") or "")[:120] for d in docs[:15]
                ],
                "body": body[:1400],
            }
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    path = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_docs_tab_v1.json"))
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str)[:4500])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
