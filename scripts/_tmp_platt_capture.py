"""Capture Platt GraphQL search operation via Playwright."""
from __future__ import annotations

import json
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

captured: list[dict] = []


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )

        def on_request(req):
            if "graphql" in req.url and req.method == "POST":
                try:
                    body = req.post_data or ""
                    captured.append({"url": req.url, "body": body[:4000]})
                except Exception:
                    pass

        def on_response(resp):
            if "graphql" in resp.url and resp.status == 200:
                try:
                    txt = resp.text()[:8000]
                    captured.append({"url": resp.url, "response": txt})
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(
            "https://www.platt.com/search?q=11055",
            wait_until="networkidle",
            timeout=45000,
        )
        page.wait_for_timeout(4000)
        # try click product if any
        hrefs = page.eval_on_selector_all(
            "a[href*='/p/']",
            "els => els.map(e => e.href).slice(0, 10)",
        )
        print("hrefs", hrefs)
        browser.close()

    print("captured", len(captured))
    for i, row in enumerate(captured[:12]):
        print("---", i, "---")
        print(json.dumps({k: (v[:1500] if isinstance(v, str) else v) for k, v in row.items()}, indent=2)[:1800])


if __name__ == "__main__":
    main()
