"""Freeze EXACT_PAGE_MISS_CORPUS_V1 from current Full-100 remaining misses."""

from __future__ import annotations

import json
from typing import Any

from exact_page_extraction.models import (
    BUILD,
    CORPUS,
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    SEARCH_RESULT_SHELL,
)
from exact_page_extraction.url_classify import classify_url
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from m3_data_root import data_path
from price_coverage_80.corpus import confirmed_public, load_corpus

PRIOR_GRAPH_CK = "m3_mfr_dist_graph_v1_checkpoint.json"
PRIOR_GRAPH_STORE = "m3_mfr_dist_graph_v1_store.json"

# Additional verified-ish product detail URLs for remaining misses (public catalog pages)
_EXTRA_PRODUCT_URLS: dict[str, list[str]] = {
    "B-45580": [
        "https://www.toolbarn.com/makita-b-45580/",
        "https://www.acmetools.com/shop/tools/makita-b-45580",
    ],
    "LF777M2-QT": [
        "https://www.pexuniverse.com/watts-lf777m2-qt",
        "https://www.pexuniverse.com/watts-lf777m2-qt-lead-free-bronze-union-ball-valve",
        "https://www.plumbingsupply.com/watts-lf777m2qt.html",
        "https://www.supplyhouse.com/Watts-LF777M2-QT",
    ],
    "5320-S": [
        "https://www.platt.com/p/0034880/leviton/15a-residential-grade-duplex-receptacle-5-15r-brown/078477231951/5320-s",
        "https://www.platt.com/p/0034880/leviton/scp-10-fb-bulk/078477231951/lev5320s",
        "https://www.leviton.com/en/products/5320-s",
    ],
    "48-22-1902": [
        "https://www.amazon.com/Milwaukee-Electric-48-22-1902-Fastback-Storage/dp/B00D0YR9A2",
        "https://www.milwaukeetool.com/products/details/fastback-ii-flip-utility-knife-with-storage/48-22-1902",
    ],
    "DWT-6": [
        "https://www.globalindustrial.com/p/rubber-wheel-tire-chock-10-l-x-8-w-x-6-h",
    ],
    "121943": [
        "https://www.bradyid.com/labels/lockout-tagout/tags/danger-do-not-operate-tag-121943",
        "https://www.bradyid.com/labels/lockout-tagout/tags/121943",
        "https://www.grainger.com/product/BRADY-Lockout-Tag-4E241",
    ],
    "FF63009": [
        "https://www.dieselpartsdirect.com/ff63009",
        "https://www.thedieselstore.com/fleetguard-ff63009.html",
    ],
    "2004DC-6": [
        "https://www.filtrete.com/3M/en_US/filtrete/products/~/Filtrete-Healthy-Living-Ultra-Allergen-Filter-2004DC-6/",
        "https://www.homedepot.com/p/Filtrete-20-x-20-x-1-Ultra-Allergen-Reduction-Filter-6-Pack-2004DC-6/203160386",
    ],
    "UC248LFA": [
        "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9",
    ],
    "TH8320U1008": [
        "https://parts-hvac.com/th8320u1008-honeywell-visionpro-thermostat.html",
    ],
    "5579409PX": [
        "https://www.dieselpartsdirect.com/5579409px",
    ],
    "60926": [
        "https://www.3m.com/3M/en_US/p/d/v000057531/",
    ],
    "5P71": [],
    "N5500": [],
    "117": [
        "https://www.fluke.com/en-us/product/electrical-testing/digital-multimeters/fluke-117",
        "https://www.platt.com/p/0680918/fluke/multimeter-with-non-contact-voltage-maximum-rating-600v/095969324205/flufluke117",
        "https://www.homedepot.com/p/Fluke-117-Electrician-s-Multimeter-with-Non-Contact-Voltage/202530555",
    ],
    "1221-2": [
        "https://www.platt.com/p/0034316/leviton/toggle-switch-1-pole-20-amp-120-277v-brown/078477239889/lev12212",
        "https://www.leviton.com/en/products/1221-2w",
    ],
    "HBL5362": [],
    "U248LFA": [],
    "U072LFA": [],
    "LFN45BMU-3/4": [],
    "695-G": [],
    "RTH2300B": [
        "https://www.honeywellhome.com/us/en/products/security/thermostats/5-2-day-programmable-thermostat-rth2300b/",
    ],
    "201": [
        "https://parts-hvac.com/aprilaire-201-replacement-filter.html",
        "https://www.supplyhouse.com/Aprilaire-201",
    ],
    "TH6220U2000": [],
    "40771": [
        "https://www.1000bulbs.com/product/117513/LED-40771.html",
        "https://www.1000bulbs.com/product/40771/SYLVANIA-40771.html",
    ],
    "93129788": [],
    "2GTL4": [],
    "GSM11-BE": [],
    "WB218294": [
        "https://www.globalindustrial.com/p/wb218294",
    ],
    "121944": [],
    "05005": [
        "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/",
        "https://www.crcindustries.com/products/brakleen-brake-parts-cleaner-non-chlorinated-05005",
    ],
    "26221": [],
    "80078": [
        "https://www.autozone.com/sealants-and-gaskets/r-t-v-silicone/p/permatex-ultra-black-gasket-maker-3-35-oz/8198_0_0",
    ],
    "08884": [],
    "AF26154": [
        "https://www.dieselpartsdirect.com/af26154",
    ],
    "WF2126": [
        "https://www.dieselpartsdirect.com/wf2126",
    ],
    "36795": [
        "https://www.rockauto.com/en/parts/gates,36795,serpentine+belt,7272",
    ],
    "SET47": [
        "https://www.rockauto.com/en/parts/timken,set47,wheel+bearing,10604",
    ],
    "51515": [
        "https://www.rockauto.com/en/parts/wix,51515,oil+filter,5340",
        "https://www.summitracing.com/parts/wix-51515",
        "https://www.autozone.com/filters-and-pcv/oil-filter/p/wix-oil-filter-51515/7585_0_0",
    ],
    "PH8A": [
        "https://www.autozone.com/filters-and-pcv/oil-filter/p/fram-extra-guard-oil-filter-ph8a/819_0_0",
        "https://www.rockauto.com/en/parts/fram,ph8a,oil+filter,5340",
        "https://www.summitracing.com/parts/frm-ph8a",
    ],
    "H5701": [],
    "1201BL": [],
    "S3960C": [],
    "430": [
        "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier-10-max-jaw-opening-2/025582301475/chk430",
        "https://www.homedepot.com/p/CHANNELLOCK-10-in-Tongue-and-Groove-Pliers-430/100207373",
        "https://www.channellock.com/430-10-tongue-groove-pliers/",
    ],
    "8265": [
        "https://www.autozone.com/repair-and-maintenance/epoxy/p/j-b-weld-original-cold-weld-formula-steel-reinforced-epoxy-2-oz/8218_0_0",
    ],
    "50036": [
        "https://www.gorillatough.com/product/gorilla-tape-black/",
    ],
    "2979": [],
    "35-903": [],
    "OM60/950CA/AG": [
        "https://www.feit.com/products/om60-950ca-ag",
    ],
    "30241": [
        "https://www.homedepot.com/p/Oatey-8-oz-Purple-Primer-30241/100124418",
    ],
    "31016": [
        "https://www.homedepot.com/p/Oatey-8-oz-PVC-Cement-31016/100145994",
    ],
    "886-GP": [],
    "TH4110U2005": [],
    "48-22-3078": [
        "https://www.platt.com/p/1865158/milwaukee/7-in-1-high-leverage-combination-pliers/045242570768/mil48223078",
        "https://www.motion.com/products/sku/10924933",
        "https://www.homedepot.com/p/Milwaukee-6-in-1-Combination-Pliers-48-22-3078/205845797",
    ],
    "48-22-6105": [
        "https://www.platt.com/p/1433870/milwaukee/mini-flush-cutters/045242518661/mil48226105",
        "https://www.homedepot.com/p/Milwaukee-5-in-1-High-Leverage-Electricians-Pliers-48-22-6105/300169009",
    ],
    "DWHT56027": [
        "https://www.homedepot.com/p/DEWALT-15-in-Fencing-Pliers-DWHT56027/205481928",
    ],
    "D2000-9NEAT": [
        "https://www.motion.com/products/sku/03090035",
        "https://www.homedepot.com/p/Klein-Tools-9-in-High-Leverage-Side-Cutting-Pliers-D2000-9NEAT/100647665",
    ],
    "61-744": [
        "https://www.platt.com/p/0751009/ideal/clamp-pro-600-aac-clamp-meter-w-ncv/783250682386/ide61744",
    ],
    "AC216CVS": [
        "https://www.homedepot.com/p/Crescent-6-in-Slip-Joint-Pliers-AC216CVS/204839503",
    ],
    "2073212": [
        "https://www.homedepot.com/p/IRWIN-VISE-GRIP-12-in-GrooveLock-Pliers-2073212/202266165",
    ],
}


def _uniq(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def build_miss_corpus(*, force: bool = False) -> dict[str, Any]:
    p = data_path(CORPUS)
    if p.exists() and not force:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass

    ck = {}
    cp = data_path(PRIOR_GRAPH_CK)
    if cp.exists():
        ck = json.loads(cp.read_text(encoding="utf-8"))
    store = {}
    sp = data_path(PRIOR_GRAPH_STORE)
    if sp.exists():
        store = json.loads(sp.read_text(encoding="utf-8"))

    corpus = load_corpus()
    conf = confirmed_public(corpus)[:100]
    by = {i["benchmark_id"]: i for i in (corpus.get("items") or [])}
    products = store.get("products") or {}

    def _contaminated(bid: str, found: dict[str, Any]) -> bool:
        url = str(found.get("source_url") or "").lower()
        seller = str(found.get("seller") or "").lower()
        price = float(found.get("unit_price") or 0)
        if "nationaldistributorllc" in url or "nationaldistributorllc" in seller:
            return True
        if bid == "easy-global-dwt-6" and "globalindustrial.com/p/dwt-6" in url:
            return True
        if abs(price - 10.58) < 0.001 and "nationaldistributorllc" in url:
            return True
        return False

    misses: list[dict[str, Any]] = []
    priced_ids: list[str] = []
    for item in conf:
        bid = item["benchmark_id"]
        row = (ck.get("items") or {}).get(bid) or {}
        found = row.get("found") or {}
        if found.get("usable") and not _contaminated(bid, found):
            priced_ids.append(bid)
            continue
        full = by.get(bid) or item
        mpn = str(full.get("mpn") or full.get("part_number") or "")
        urls: list[str] = []
        for urow in (products.get(bid) or {}).get("exact_urls") or []:
            urls.append(urow.get("exact_product_url") or urow.get("url") or "")
        urls.extend(curated_exact_urls(mpn))
        urls.extend(_EXTRA_PRODUCT_URLS.get(mpn.upper(), []) or _EXTRA_PRODUCT_URLS.get(mpn, []) or [])
        urls = _uniq(urls)

        classified = []
        for u in urls:
            cls = classify_url(u, mpn=mpn)
            classified.append({"url": u, "class": cls})
        verified = [c["url"] for c in classified if c["class"] == EXACT_PRODUCT_VERIFIED]
        unverified = [c["url"] for c in classified if c["class"] == EXACT_PRODUCT_UNVERIFIED]
        shells = [c["url"] for c in classified if c["class"] == SEARCH_RESULT_SHELL]

        misses.append(
            {
                "benchmark_id": bid,
                "manufacturer": full.get("manufacturer"),
                "mpn": mpn,
                "known_public_seller": full.get("known_public_seller"),
                "known_public_price": full.get("known_public_price"),
                "category": full.get("category"),
                "expected_condition": full.get("expected_condition") or "NEW",
                "expected_uom": full.get("expected_uom") or "EA",
                "expected_pack": full.get("expected_pack") or 1,
                "description": full.get("description"),
                "failure_class": (row.get("miss_trace") or {}).get("why_m3_missed") or row.get("status") or "NO_PRICE",
                "exact_urls_verified": verified,
                "exact_urls_unverified": unverified,
                "search_shells_excluded": shells,
                "all_classified": classified,
            }
        )

    payload = {
        "kind": "EXACT_PAGE_MISS_CORPUS_V1",
        "build": BUILD,
        "immutable": True,
        "confirmed_publicly_priceable": len(conf),
        "previously_priced": len(priced_ids),
        "remaining_misses": len(misses),
        "priced_ids": priced_ids,
        "misses": misses,
        "stats": {
            "with_verified_url": sum(1 for m in misses if m["exact_urls_verified"]),
            "with_unverified_url": sum(1 for m in misses if m["exact_urls_unverified"]),
            "with_any_candidate_url": sum(
                1 for m in misses if m["exact_urls_verified"] or m["exact_urls_unverified"]
            ),
        },
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_miss_corpus() -> dict[str, Any]:
    return build_miss_corpus(force=False)
