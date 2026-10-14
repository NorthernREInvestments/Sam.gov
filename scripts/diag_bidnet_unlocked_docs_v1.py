"""After PG unlock, probe documents for solicitation 9797265001."""

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

    sid = "9797265001"
    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    out: dict = {"sid": sid}
    try:
        auth = client.ensure_authenticated()
        out["auth"] = auth.status
        urls = [
            f"https://www.bidnetdirect.com/private/solicitations/{sid}/abstract?target=view",
            f"https://www.bidnetdirect.com/private/solicitations/{sid}/documents",
            f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid}/documents",
            f"https://www.bidnetdirect.com/private/supplier/solicitations/{sid}/view",
        ]
        for u in urls:
            try:
                client._page.goto(u, wait_until="domcontentloaded", timeout=20_000)
                client._page.wait_for_timeout(1_200)
            except Exception as exc:
                out.setdefault("nav", []).append({"url": u[:120], "error": type(exc).__name__})
                continue
            final = str(client._page.url or "")
            html = client._page.content() or ""
            body = re.sub(r"\s+", " ", client._page.locator("body").inner_text() or "")[:800]
            links = client.discover_attachment_links(final, timeout_ms=8_000) or []
            out.setdefault("pages", []).append(
                {
                    "requested": u[:120],
                    "final": final[:160],
                    "gate": "action is required" in html.lower(),
                    "link_count": len(links),
                    "links": [
                        (d.get("document_name") or d.get("name") or "")[:100]
                        for d in links[:10]
                        if isinstance(d, dict)
                    ],
                    "body_snip": body[:400],
                }
            )
    finally:
        try:
            client.close()
        except Exception:
            pass
    path = Path(data_path("_og_commercial_research_pipeline_out/m3_diag_unlocked_docs_v1.json"))
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str)[:5000])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
