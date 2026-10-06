"""Probe browser-assisted validation on curated fetch-blocked URLs."""
from __future__ import annotations

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.sweep import unresolved_items
from exact_product_url_discovery.validate_identity import validate_product_page
from manufacturer_distributor_graph.exact_urls import curated_exact_urls

# sample of high-value unresolved
sample_ids = {
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-3m-2097",
    "light-sylvania-40771",
    "mro-crc-05005",
    "tool-dewalt-dwht56027",
    "hvac-aprilaire-201",
    "plumb-oatey-30241",
    "ppe-ansell-37-175",
    "mro-jb-weld-8265",
    "auto-wix-51515",
}

items = [i for i in unresolved_items() if i["benchmark_id"] in sample_ids]
for it in items:
    mpn = it.get("mpn") or ""
    mfr = it.get("manufacturer") or ""
    urls = [u for u in curated_exact_urls(mpn) if u.startswith("http") and not is_search_shell(u)]
    print("===", it["benchmark_id"], "candidates", len(urls))
    for u in urls[:2]:
        v = validate_product_page(
            u,
            mpn=mpn,
            manufacturer=mfr,
            description=it.get("description"),
            allow_browser=True,
        )
        print(
            " ",
            "PASS" if v.get("identity_match") else "FAIL",
            v.get("reason") or v.get("via"),
            u[:90],
        )
