"""Seed curated product URL candidates via Cursor is external — this script validates a seeded list."""
from __future__ import annotations

# Populated from web research + known open-domain patterns for Full-100 misses.
# Only URLs that pass identity validation become READY_FOR_PRICE_EXTRACTION.

SEED_CANDIDATES: dict[str, list[str]] = {
    "9290018191": [
        "https://www.assets.signify.com/is/content/PhilipsLighting/Assets/philips-lighting/global/20200108-P0019380.pdf",  # will reject PDF
    ],
    "B-45580": [
        "https://www.makitatools.com/products/details/B-45580",
        "https://www.makita.ca/index2.php?catid=new&event=newaccessorydetails&id=1798",
    ],
    "LF777M2-QT": [
        "https://www.watts.com/products/plumbing-flow-control-solutions/backflow-prevention/double-check-valve-assemblies/007",
        "https://www.pexuniverse.com/watts-lf777m2-qt",
    ],
    "121943": [
        "https://www.bradyid.com/labels/lockout-tagout/tags/danger-do-not-operate-tag-121943",
        "https://www.bradyid.com/products/danger-do-not-operate-lockout-tagout-tags-pid-121943",
    ],
    "DWT-6": [
        "https://www.globalindustrial.com/p/rubber-wheel-tire-chock-10-l-x-8-w-x-6-h",  # likely wrong MPN (129100)
    ],
    "CF289A": [
        "https://www.hp.com/us-en/shop/pdp/hp-89a-black-original-laserjet-toner-cartridge-cf289a",
        "https://www.officedepot.com/a/products/2438907/HP-89A-Black-Original-LaserJet-Toner/",
    ],
    "490040": [
        "https://www.wd40.com/products/specialist/dry-lube/",
    ],
    "2097": [
        "https://www.3m.com/3M/en_US/p/d/v000058247/",
        "https://www.homelectrical.com/particulate-filter-p100-w-nuisance-ov.mmm-2097.1.html",
    ],
    "40771": [
        "https://www.sylvania-lamps.com/products/40771",
        "https://www.1000bulbs.com/product/40771/SYLVANIA-40771.html",
    ],
    "430": [
        "https://www.channellock.com/430-10-tongue-groove-pliers/",
        "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier/025582301475/chk430",
    ],
    "05005": [
        "https://www.crcindustries.com/products/brakleen-brake-parts-cleaner-non-chlorinated-05005",
    ],
    "26221": [
        "https://www.loctiteproducts.com/en/products/threadlockers/permanent-threadlockers/loctite_threadlockerred262.html",
    ],
    "80078": [
        "https://www.permatex.com/products/gasketing/gasket-makers/permatex-ultra-black-gasket-maker/",
    ],
    "51515": [
        "https://www.summitracing.com/parts/wix-51515",
        "https://www.rockauto.com/en/parts/wix,51515,oil+filter,5340",
    ],
    "30241": [
        "https://www.oatey.com/products/solvent-cements-primers/primers/purple-primer-30241",
    ],
    "31016": [
        "https://www.oatey.com/products/solvent-cements-primers/solvent-cements/pvc-cement-31016",
    ],
    "201": [
        "https://shop.aprilaire.com/products/aprilaire-201-replacement-filter",
        "https://parts-hvac.com/aprilaire-201-replacement-filter.html",
    ],
    "TH6220U2000": [
        "https://www.gsistore.com/products/resideo-th6220u2000-thermostat",
    ],
    "5P71": [
        "https://www.homelectrical.com/6000-7000-series-half-and-full-facepiece-cartridges-filters.mco-5p71.1.html",
    ],
    "886-GP": [
        "https://www.activeplumbing.com/buy/product/886-gp/130861",
    ],
    "WB218294": [
        "https://www.globalindustrial.com/p/wb218294",
    ],
    "H5701": [
        "https://www.hon.com/products/seating/task/volt-series-task-chair",
    ],
    "8265": [
        "https://www.jbweld.com/product/original-cold-weld-formula",
    ],
    "50036": [
        "https://www.gorillatough.com/product/gorilla-tape-black/",
    ],
    "DWHT56027": [
        "https://www.dewalt.com/product/dwht56027/folding-jab-saw",
    ],
    "11055": [
        "https://www.platt.com/p/0497361/klein/wire-stripper-cutter-10-18-awg/092644740572/kle11055",
    ],
}
