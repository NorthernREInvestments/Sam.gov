"""Probe many open-domain URL patterns for unresolved items."""
from __future__ import annotations

from exact_product_url_discovery.sweep import unresolved_items
from exact_product_url_discovery.validate_identity import validate_product_page
from open_web_product_discovery.adapters_platt import platt_suggest_urls
import time

EXTRA = {
    "201": ["https://shop.aprilaire.com/products/aprilaire-201-replacement-filter"],
    "TH6220U2000": ["https://www.gsistore.com/products/resideo-th6220u2000-thermostat"],
    "886-GP": ["https://www.activeplumbing.com/buy/product/886-gp/130861"],
    "5P71": ["https://www.homelectrical.com/6000-7000-series-half-and-full-facepiece-cartridges-filters.mco-5p71.1.html"],
    "2097": [
        "https://www.homelectrical.com/particulate-filter-p100-w-nuisance-ov.mmm-2097.1.html",
        "https://www.veronasafety.com/3m-particulate-filter-p100-w-nuisance-level-organic-vapor-relief--2097",
    ],
    "60926": [
        "https://www.homelectrical.com/multi-gas-vapor-cartridge-filter-p100.mmm-60926.1.html",
        "https://www.veronasafety.com/cartridge-filter-multi-gas-vapor-p100--60926",
    ],
    "430": ["https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier/025582301475/chk430"],
    "11055": ["https://www.platt.com/p/0497361/klein/wire-stripper-cutter-10-18-awg/092644740572/kle11055"],
    "51515": ["https://www.rockauto.com/en/parts/wix,51515,oil+filter,5340"],
    "695-G": [
        "https://www.activeplumbing.com/buy/product/695-g/",
        "https://siouxchief.com/Products/695-G",
    ],
    "RTH2300B": [
        "https://www.gsistore.com/products/honeywell-rth2300b",
        "https://www.honeywellhome.com/us/en/products/security/thermostats/5-2-day-programmable-thermostat-rth2300b/",
    ],
    "30241": [
        "https://www.activeplumbing.com/buy/product/30241/",
        "https://www.oatey.com/products/solvent-cements-primers/primers/purple-primer-30241",
    ],
    "31016": [
        "https://www.activeplumbing.com/buy/product/31016/",
        "https://www.oatey.com/products/solvent-cements-primers/solvent-cements/pvc-cement-31016",
    ],
    "LFN45BMU-3/4": [
        "https://www.activeplumbing.com/buy/product/lfn45bmu/",
        "https://www.pexuniverse.com/watts-lfn45bmu-34",
    ],
    "LF777M2-QT": [
        "https://www.activeplumbing.com/buy/product/lf777m2-qt/",
        "https://www.pexuniverse.com/watts-lf777m2-qt",
    ],
    "10034018": ["https://pksafety.com/products/msa-v-gard-500-vented-cap-style-hard-hat-10034018"],
    "37-175": ["https://pksafety.com/products/ansell-flock-lined-nitrile-glove-37-175-12-pairs"],
    "DWHT56027": [
        "https://www.homedepot.com/p/DEWALT-Folding-Jab-Saw-DWHT56027/205481928",
        "https://www.dewalt.com/product/dwht56027/folding-jab-saw",
    ],
    "D2000-9NEAT": [
        "https://www.kleintools.com/catalog/side-cutting-pliers/high-leverage-side-cutting-pliers-fish-tape-pulling-and-crimping",
    ],
    "05005": [
        "https://www.crcindustries.com/products/brakleen-brake-parts-cleaner-non-chlorinated-05005",
        "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/",
    ],
    "26221": ["https://www.loctiteproducts.com/en/products/threadlockers/permanent-threadlockers/loctite_threadlockerred262.html"],
    "80078": ["https://www.permatex.com/products/gasketing/gasket-makers/permatex-ultra-black-gasket-maker/"],
    "CF289A": ["https://www.hp.com/us-en/shop/pdp/hp-89a-black-original-laserjet-toner-cartridge-cf289a"],
    "CF410A": ["https://www.hp.com/us-en/shop/pdp/hp-410a-black-original-laserjet-toner-cartridge-cf410a"],
    "T252120": ["https://epson.com/Support/Printers/All-In-Ones/WorkForce-Series/Epson-WorkForce-WF-3620/s/SPT_C11CD75011?review-filter=T252120"],
    "40771": ["https://www.ledvanceus.com/products/40771"],
    "121943": ["https://www.bradyid.com/products/danger-do-not-operate-lockout-tagout-tags-pid-121943"],
    "121944": ["https://www.bradyid.com/products/danger-do-not-operate-lockout-tagout-tags-pid-121944"],
    "490040": ["https://www.wd40.com/products/multi-use-product/"],
    "8265": ["https://www.jbweld.com/product/original-cold-weld-formula", "https://www.jbweld.com/product/j-b-weld-twin-tube"],
    "50036": ["https://www.gorillatough.com/product/gorilla-tape/"],
    "H5701": ["https://www.hon.com/products/seating/task/volt-series-task-chair"],
    "B-45580": ["https://www.makitatools.com/products/details/B-45580"],
    "AC216CVS": ["https://www.crescenttool.com/products/adjustable-wrenches/ac216cvs"],
    "2073212": ["https://www.irwin.com/products/locking-tools/vise-grip/2073212"],
    "N5500": ["https://sps.honeywell.com/us/en/products/safety/respiratory-protection/reusable-respirators/north-5500-series-half-mask"],
    "08884": ["https://www.3m.com/3M/en_US/p/d/v000057xxx/"],  # placeholder skip
    "WB218294": ["https://www.globalindustrial.com/p/wb218294"],
    "FG9T6700BLA": ["https://www.rubbermaidcommercial.com/brute-dolly/"],
    "1201BL": ["https://www.safcoproducts.com/products/1201bl"],
    "DWT-6": ["https://www.globalindustrial.com/p/dwt-6"],
    "9290018191": ["https://www.1000bulbs.com/product/218154/PHILIPS-9290018191.html"],
    "93129788": ["https://www.1000bulbs.com/product/93129788/GE-93129788.html"],
    "2GTL4": ["https://www.acuitybrands.com/products/detail/214234/lithonia-lighting/2gtl/2gtl-led-troffer"],
}

found = {}
# first platt for tools/electrical with pacing
items = unresolved_items()
for it in items:
    bid = it["benchmark_id"]
    mpn = it.get("mpn") or ""
    mfr = it.get("manufacturer") or ""
    urls = list(EXTRA.get(mpn, []) or [])
    # platt for relevant
    if it.get("category") in {"tools", "electrical", "electrical tools"} or bid.startswith(("tool-", "elec-", "hvac-")):
        try:
            for row in platt_suggest_urls(mpn, manufacturer=mfr, limit=2):
                urls.insert(0, row["url"])
            time.sleep(0.5)
        except Exception:
            pass
    hit = None
    for u in urls:
        if "placeholder" in u or "v000057xxx" in u:
            continue
        v = validate_product_page(u, mpn=mpn, manufacturer=mfr, description=it.get("description"), allow_browser=False)
        if not v.get("identity_match") and v.get("reason") in {"fetch_blocked_or_failed", "auth_or_non_product_title", "mpn_not_on_page"}:
            v = validate_product_page(u, mpn=mpn, manufacturer=mfr, description=it.get("description"), allow_browser=True)
        if v.get("identity_match"):
            hit = u
            break
    if hit:
        found[bid] = hit
        print("FOUND", bid, hit[:100])
    else:
        print("MISS", bid)

print("TOTAL", len(found))
for k, v in found.items():
    print(k, "=>", v)
