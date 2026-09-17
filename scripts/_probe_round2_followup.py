"""Follow-up probes: BidNet open-bids, Boston listings, Seattle OpenGov portal."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http_client import PublicProcurementHttpClient, RequestBudget

URLS = [
    ("bidnet_il_open", "https://www.bidnetdirect.com/illinois/solicitations/open-bids"),
    ("bidnet_fl_open", "https://www.bidnetdirect.com/florida/solicitations/open-bids"),
    ("boston_bids", "https://www.boston.gov/bid-listings"),
    ("seattle_og", "https://procurement.opengov.com/portal/seattle?status=open&page=1&limit=10"),
    ("king_new", "https://kingcounty.gov/en/dept/executive-services/procurement"),
    ("king_alt", "https://kingcounty.gov/depts/finance/procurement.aspx"),
    ("pp_wy_bids", "https://www.publicpurchase.com/gems/wyoming/buyer/publicbids"),
    ("pp_wy_list", "https://www.publicpurchase.com/gems/wyoming/buyer/listPublicBids.html"),
    ("lausd", "https://www.lausd.org/procurement"),
    ("chicago_opp", "https://www.chicago.gov/city/en/depts/dps/provdrs/contract/svcs/opportunity_takeoutlist.html"),
]


def main() -> None:
    c = PublicProcurementHttpClient(
        budget=RequestBudget(max_total_requests=30, max_requests_per_source=2, min_interval_seconds=1.5),
        authorize_live=True,
    )
    for name, url in URLS:
        try:
            r = c.get(url, source_id=name)
            t = r.text or ""
            Path("artifacts").mkdir(exist_ok=True)
            (ROOT / "artifacts" / f"_probe_{name}.html").write_text(t[:150000], encoding="utf-8", errors="ignore")
            low = t.lower()
            print(
                f"{name}\t{r.status_code}\tlen={len(t)}\t"
                f"login={'login' in low}\treg={'register' in low}\t"
                f"table={'<table' in low}\tjson={t.strip()[:1] in '{['}"
            )
            if name.startswith("bidnet"):
                titles = re.findall(
                    r'href="(/[^"]*solicitations/[^"]+)"[^>]*>([^<]{8,200})</a>',
                    t,
                    re.I,
                )
                print(f"  titles={len(titles)} sample={[x[1].strip()[:50] for x in titles[:5]]}")
            if "boston" in name:
                rows = re.findall(r"<tr[^>]*>.*?</tr>", t, re.I | re.S)
                print(f"  tr_count={len(rows)}")
            if "opengov" in url or "seattle_og" in name:
                print(f"  preview={t[:200].replace(chr(10),' ')}")
        except Exception as exc:  # noqa: BLE001
            print(f"{name}\tERR\t{type(exc).__name__}: {str(exc)[:160]}")


if __name__ == "__main__":
    main()
