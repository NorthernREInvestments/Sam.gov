"""One-shot public probe of representative Round-2 sources."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http_client import PublicProcurementHttpClient, RequestBudget

URLS = [
    ("boston", "https://www.boston.gov/departments/procurement"),
    ("seattle", "https://www.seattle.gov/purchasing-and-contracting"),
    ("king", "https://kingcounty.gov/en/dept/kceo/governance-leadership/procurement"),
    ("umich", "https://procurement.umich.edu/"),
    ("labavn", "https://www.labavn.org/"),
    ("lawa", "https://www.lawa.org/lawa-businesses/lawa-business-opportunities"),
    ("chicago", "https://www.chicago.gov/city/en/depts/dps.html"),
    ("nyscr", "https://www.nyscr.ny.gov/"),
    ("pp_wy", "https://www.publicpurchase.com/gems/wyoming/buyer/public/home"),
    ("pp_chey", "https://www.publicpurchase.com/gems/cheyenne/buyer/public/home"),
    ("bidnet_il", "https://www.bidnetdirect.com/illinois"),
    ("bidnet_fl", "https://www.bidnetdirect.com/florida"),
    ("opengov_demo", "https://procurement.opengov.com/portal/boston"),
    ("planet_lax", "https://pbsystem.planetbids.com/portal/15424/portal-home"),
]


def main() -> None:
    c = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=40, max_requests_per_source=2, min_interval_seconds=1.5),
        authorize_live=True,
    )
    for name, url in URLS:
        try:
            r = c.get(url, source_id=name)
            t = r.text or ""
            low = t.lower()
            hits = [
                p
                for p in (
                    "opengov",
                    "planetbids",
                    "bidnet",
                    "publicpurchase",
                    "bonfire",
                    "solicitation",
                    "current bid",
                    "open bid",
                    "login",
                    "captcha",
                    "register",
                )
                if p in low
            ]
            hrefs = re.findall(
                r'href=["\']([^"\']*(?:opengov|planetbids|bidnet|publicpurchase|/bid|/solicit|/opportunity)[^"\']*)["\']',
                t,
                re.I,
            )[:8]
            print(f"{name}\t{r.status_code}\tlen={len(t)}\thits={hits}\threfs={hrefs[:5]}")
            # save small sample for fixture harvest
            sample = ROOT / "artifacts" / f"_probe_{name}.html"
            sample.write_text(t[:120000], encoding="utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            print(f"{name}\tERR\t{type(exc).__name__}: {str(exc)[:160]}")


if __name__ == "__main__":
    main()
