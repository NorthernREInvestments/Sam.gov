"""Curated + learned exact product URL candidates for known misses.

Build: 20261004-m3-manufacturer-distributor-graph-v1

These are public catalog product/search URLs — not prices. Extraction still
requires exact MPN + NEW validation.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from manufacturer_distributor_graph.mpn_normalize import mpn_variants

# MPN upper → list of exact-ish product URLs (high confidence public pages)
_EXACT_URLS: dict[str, list[str]] = {
    # Known Easy / regression
    "B-45580": [
        "https://www.toolbarn.com/makita-b-45580/",
        "https://www.acmetools.com/makita-b-45580-5-8-diamond-wheel-for-cutting-concrete.html",
    ],
    "LF777M2-QT": [
        "https://www.pexuniverse.com/watts-lf777m2-qt",
        "https://www.supplyhouse.com/Watts-LF777M2-QT",
        "https://www.ferguson.com/product/watts-series-lf777m2-lead-free-bronze-union-end-swing-check-valve/_/R-4320854",
    ],
    "5320-S": [
        "https://www.platt.com/p/0034880/leviton/15a-residential-grade-duplex-receptacle-5-15r-brown/078477231951/5320-s",
        "https://www.platt.com/p/0034880/leviton/scp-10-fb-bulk/078477231951/lev5320s",
        "https://www.leviton.com/en/products/5320-s",
    ],
    "121943": [
        "https://www.bradyid.com/labels/lockout-tagout/tags/danger-do-not-operate-tag-121943",
        "https://www.seton.com/brady-danger-do-not-operate-tag-b906.html",
        "https://www.northernsafety.com/Product/121943/Brady-Danger-Do-Not-Operate-Tag",
    ],
    "FF63009": [
        "https://www.dieselpartsdirect.com/ff63009",
        "https://www.thedieselstore.com/fleetguard-ff63009.html",
    ],
    "2004DC-6": [
        "https://www.filtersfast.com/Prod-3M_Filtrete_2004DC-6_Healthy_Living_Elite_Allergen_Reduction_Filter.asp",
        "https://www.filtersfast.com/prod-filtrete-2004dc-6.asp",
    ],
    "DWT-6": [
        # NOTE: /p/dwt-6 resolves to unrelated DWT62 drywall screws — do not use
        "https://www.globalindustrial.com/p/rubber-wheel-tire-chock-10-l-x-8-w-x-6-h",
    ],
    "5579409PX": [
        "https://www.dieselpartsdirect.com/5579409px",
        "https://www.thedieselstore.com/cummins-5579409px.html",
    ],
    "9290018191": [
        "https://www.1000bulbs.com/product/218154/PHILIPS-9290018191.html",
        "https://www.bulbs.com/product/9290018191",
    ],
    "2097": [
        "https://www.3m.com/3M/en_US/p/d/v000058247/",
        "https://www.platt.com/search?q=3M+2097",
    ],
    "CF289A": [
        "https://www.quill.com/hp-89a-black-original-laserjet-toner-cartridge-cf289a/cbs/487845.html",
        "https://www.officedepot.com/a/products/2438907/HP-89A-Black-Original-LaserJet-Toner/",
    ],
    "490040": [
        "https://www.wd40.com/products/specialist/dry-lube/",
        "https://crcautocare.com/product/wd-40-specialist-dry-lube-10-oz-490040/",
    ],
    "48-22-1902": [
        "https://www.amazon.com/Milwaukee-Electric-48-22-1902-Fastback-Storage/dp/B00D0YR9A2",
        "https://www.milwaukeetool.com/products/details/fastback-ii-flip-utility-knife-with-storage/48-22-1902",
    ],
    "UC248LFA": [
        "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9",
    ],
    "TH8320U1008": [
        "https://parts-hvac.com/th8320u1008-honeywell-visionpro-thermostat.html",
        "https://www.supplyhouse.com/Honeywell-TH8320U1008",
    ],
    "DWHT56027": [
        "https://www.acmetools.com/dewalt-dwht56027-folding-jab-saw.html",
        "https://www.toolbarn.com/dewalt-dwht56027/",
    ],
    "D2000-9NEAT": [
        "https://www.kleintools.com/catalog/side-cutting-pliers/high-leverage-side-cutting-pliers-fish-tape-pulling-and-crimping",
        "https://www.platt.com/search?q=D2000-9NEAT",
    ],
    "CF410A": [
        "https://www.officedepot.com/a/products/160147/HP-410A-Black-Original-LaserJet-Toner/",
    ],
    "T252120": [
        "https://www.officedepot.com/a/products/952286/Epson-T252120-DURABrite-Ultra-Ink/",
    ],
    "FG9T6700BLA": [
        "https://www.globalindustrial.com/p/rubbermaid-commercial-products-brute-dolly-for-20-32-44-and-55-gallon",
    ],
    "10034018": [
        "https://www.northernsafety.com/Product/10034018/MSA-V-Gard-Protective-Cap",
    ],
    "37-175": [
        "https://www.northernsafety.com/Product/37-175/Ansell-HyFlex-Foam-Gloves",
    ],
    "AC216CVS": [
        "https://www.acmetools.com/crescent-ac216cvs-adjustable-wrench.html",
    ],
    "2073212": [
        "https://www.acmetools.com/irwin-2073212-vise-grip-locking-pliers.html",
    ],
    "11055": [
        "https://www.platt.com/p/0497361/klein/wire-stripper-cutter-10-18-awg/092644740572/kle11055",
        "https://www.kleintools.com/catalog/wire-strippers-cutters/klein-kurve-wire-stripper-cutter",
    ],
    # Full-100 misses
    "60926": [
        "https://www.quill.com/search?keywords=3M+60926",
        "https://www.mscdirect.com/browse/tn/?searchterm=3M+60926",
        "https://www.3m.com/3M/en_US/p/d/v000057531/",
    ],
    "5P71": [
        "https://www.homelectrical.com/6000-7000-series-half-and-full-facepiece-cartridges-filters.mco-5p71.1.html",
        "https://www.quill.com/search?keywords=3M+5P71",
        "https://www.mscdirect.com/browse/tn/?searchterm=3M+5P71",
    ],
    "N5500": [
        "https://www.quill.com/search?keywords=Honeywell+N5500",
        "https://www.mscdirect.com/browse/tn/?searchterm=Honeywell+N5500",
    ],
    "117": [
        "https://www.fluke.com/en-us/product/electrical-testing/digital-multimeters/fluke-117",
        "https://www.homedepot.com/p/Fluke-117-Electrician-s-Multimeter-with-Non-Contact-Voltage/202530555",
        "https://rspsupply.com/search.aspx?SearchTerm=Fluke+117",
    ],
    "61-744": [
        "https://rspsupply.com/search.aspx?SearchTerm=Ideal+61-744",
        "https://www.homedepot.com/s/Ideal%2061-744",
        "https://www.platt.com/search?q=61-744",
    ],
    "1221-2": [
        "https://www.platt.com/search?q=1221-2",
        "https://www.leviton.com/en/products/1221-2w",
        "https://rspsupply.com/search.aspx?SearchTerm=Leviton+1221-2",
    ],
    "HBL5362": [
        "https://www.platt.com/search?q=HBL5362",
        "https://rspsupply.com/search.aspx?SearchTerm=Hubbell+HBL5362",
        "https://www.hubbell.com/wiringdevice/en/products/locking-devices/hbl5362",
    ],
    "U248LFA": [
        "https://www.homedepot.com/s/U248LFA",
        "https://www.lowes.com/search?searchTerm=U248LFA",
        "https://www.mccoys.com/search?q=u248lfa",
    ],
    "U072LFA": [
        "https://www.homedepot.com/s/U072LFA",
        "https://www.lowes.com/search?searchTerm=U072LFA",
        "https://www.mccoys.com/search?q=u072lfa",
    ],
    "LFN45BMU-3/4": [
        "https://www.pexuniverse.com/search?q=LFN45BMU",
        "https://www.homedepot.com/s/LFN45BMU",
        "https://www.supplyhouse.com/search?q=LFN45BMU",
    ],
    "695-G": [
        "https://www.mccoys.com/search?q=695-g",
        "https://www.homedepot.com/s/Sioux%20Chief%20695-G",
        "https://www.supplyhouse.com/search?q=695-G",
    ],
    "RTH2300B": [
        "https://parts-hvac.com/search?q=RTH2300B",
        "https://www.homedepot.com/s/RTH2300B",
        "https://www.honeywellhome.com/us/en/products/security/thermostats/5-2-day-programmable-thermostat-rth2300b/",
    ],
    "201": [
        "https://shop.aprilaire.com/products/aprilaire-201-replacement-filter",
        "https://parts-hvac.com/aprilaire-201-replacement-filter.html",
        "https://www.supplyhouse.com/Aprilaire-201",
    ],
    "TH6220U2000": [
        "https://www.gsistore.com/products/resideo-th6220u2000-thermostat",
        "https://parts-hvac.com/search?q=TH6220U2000",
        "https://www.supplyhouse.com/search?q=TH6220U2000",
    ],
    "40771": [
        "https://www.1000bulbs.com/product/40771/SYLVANIA-40771.html",
        "https://www.1000bulbs.com/search?q=Sylvania+40771",
    ],
    "93129788": [
        "https://www.1000bulbs.com/search?q=GE+93129788",
        "https://www.homedepot.com/s/93129788",
    ],
    "2GTL4": [
        "https://www.1000bulbs.com/search?q=Lithonia+2GTL4",
        "https://www.acuitybrands.com/products/detail/214234/lithonia-lighting/2gtl/2gtl-led-troffer",
    ],
    "GSM11-BE": [
        "https://www.quill.com/search?keywords=BIC+GSM11-BE",
        "https://www.staples.com/bic-round-stic-gsm11/directory_GSM11",
        "https://www.officedepot.com/catalog/search.do?Ntt=GSM11-BE",
    ],
    "WB218294": [
        "https://www.globalindustrial.com/search?q=WB218294",
        "https://www.globalindustrial.com/p/wb218294",
    ],
    "121944": [
        "https://www.bradyid.com/search?text=121944",
        "https://www.mscdirect.com/browse/tn/?searchterm=Brady+121944",
    ],
    "05005": [
        "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/",
        "https://www.crcindustries.com/products/brakleen-brake-parts-cleaner-non-chlorinated-05005",
        "https://www.quill.com/search?keywords=CRC+05005",
    ],
    "26221": [
        "https://www.quill.com/search?keywords=Loctite+26221",
        "https://www.mscdirect.com/browse/tn/?searchterm=Loctite+26221",
        "https://www.homedepot.com/s/Loctite%2026221",
    ],
    "80078": [
        "https://www.permatex.com/products/gasketing/gasket-makers/permatex-ultra-black-gasket-maker/",
        "https://www.autozone.com/sealants-and-gaskets/r-t-v-silicone/p/permatex-ultra-black-maximum-oil-resistance-r-t-v-silicone-gasket-maker-3-35-oz/8198_0_0",
        "https://www.quill.com/search?keywords=Permatex+80078",
    ],
    "08884": [
        "https://www.quill.com/search?keywords=3M+08884",
        "https://www.mscdirect.com/browse/tn/?searchterm=3M+08884",
    ],
    "AF26154": [
        "https://www.dieselpartsdirect.com/af26154",
        "https://www.thedieselstore.com/search?type=product&q=AF26154",
    ],
    "WF2126": [
        "https://www.dieselpartsdirect.com/wf2126",
        "https://www.thedieselstore.com/search?type=product&q=WF2126",
    ],
    "36795": [
        "https://autobuffy.com/?s=Gates+36795",
        "https://www.rockauto.com/en/parts/gates,36795,serpentine+belt,7272",
        "https://www.summitracing.com/search/part-type/serpentine-belts?keyword=36795",
    ],
    "SET47": [
        "https://maxtran.com/?s=Timken+SET47",
        "https://www.rockauto.com/en/parts/timken,set47,wheel+bearing,10604",
        "https://www.summitracing.com/search?keyword=SET47",
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
    "H5701": [
        "https://www.quill.com/search?keywords=HON+H5701",
        "https://www.staples.com/hon-volt/directory_H5701",
        "https://www.officedepot.com/catalog/search.do?Ntt=H5701",
    ],
    "1201BL": [
        "https://www.globalindustrial.com/search?q=1201BL",
        "https://www.quill.com/search?keywords=Safco+1201BL",
    ],
    "S3960C": [
        "https://www.mscdirect.com/browse/tn/?searchterm=Uvex+S3960C",
        "https://www.quill.com/search?keywords=Uvex+S3960C",
    ],
    "430": [
        "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier/025582301475/chk430",
        "https://www.homedepot.com/p/CHANNELLOCK-10-in-Tongue-and-Groove-Pliers-430/100207373",
        "https://www.channellock.com/430-10-tongue-groove-pliers/",
    ],
    "8265": [
        "https://www.quill.com/search?keywords=J-B+Weld+8265",
        "https://www.homedepot.com/s/JB%20Weld%208265",
        "https://www.autozone.com/repair-and-maintenance/epoxy/p/j-b-weld-original-cold-weld-formula-steel-reinforced-epoxy-2-oz/8218_0_0",
    ],
    "50036": [
        "https://www.quill.com/search?keywords=Gorilla+50036",
        "https://www.homedepot.com/s/Gorilla%20Tape%2050036",
        "https://www.gorillatough.com/product/gorilla-tape-black/",
    ],
    "2979": [
        "https://www.quill.com/search?keywords=3M+2979",
        "https://www.mscdirect.com/browse/tn/?searchterm=3M+2979",
    ],
    "35-903": [
        "https://rspsupply.com/search.aspx?SearchTerm=Ideal+35-903",
        "https://www.homedepot.com/s/Ideal%2035-903",
        "https://www.platt.com/search?q=35-903",
    ],
    "OM60/950CA/AG": [
        "https://www.1000bulbs.com/search?q=OM60%2F950CA%2FAG",
        "https://www.feit.com/products/om60-950ca-ag",
        "https://www.homedepot.com/s/OM60%2F950CA%2FAG",
    ],
    "30241": [
        "https://www.homedepot.com/p/Oatey-8-oz-Purple-Primer-30241/100124418",
        "https://www.lowes.com/search?searchTerm=Oatey+30241",
        "https://www.mccoys.com/search?q=30241",
    ],
    "31016": [
        "https://www.homedepot.com/p/Oatey-8-oz-PVC-Cement-31016/100145994",
        "https://www.lowes.com/search?searchTerm=Oatey+31016",
        "https://www.mccoys.com/search?q=31016",
    ],
    "886-GP": [
        "https://www.activeplumbing.com/buy/product/886-gp/130861",
        "https://siouxchief.com/Products/886-GP",
        "https://www.supplyhouse.com/search?q=886-GP",
    ],
    "TH4110U2005": [
        "https://parts-hvac.com/search?q=TH4110U2005",
        "https://www.homedepot.com/s/TH4110U2005",
        "https://www.supplyhouse.com/search?q=TH4110U2005",
    ],
}


# Domain search templates for high-yield sellers (MPN variants plugged in)
_DOMAIN_SEARCH: list[tuple[str, str]] = [
    ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ("mscdirect.com", "https://www.mscdirect.com/browse/tn/?searchterm={q}"),
    ("homedepot.com", "https://www.homedepot.com/s/{q}"),
    ("lowes.com", "https://www.lowes.com/search?searchTerm={q}"),
    ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/{pn}"),
    ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/search?q={q}"),
    ("1000bulbs.com", "https://www.1000bulbs.com/search?q={q}"),
    ("motion.com", "https://www.motion.com/products/search?q={q}"),
    ("mccoys.com", "https://www.mccoys.com/search?q={pn}"),
    ("rspsupply.com", "https://rspsupply.com/search.aspx?SearchTerm={q}"),
    ("platt.com", "https://www.platt.com/search?q={q}"),
    ("parts-hvac.com", "https://parts-hvac.com/search?q={q}"),
    ("rockauto.com", "https://www.rockauto.com/en/search.php?partnum={pn}"),
    ("summitracing.com", "https://www.summitracing.com/search?keyword={q}"),
    ("autozone.com", "https://www.autozone.com/searchresult?searchText={q}"),
    ("bradyid.com", "https://www.bradyid.com/search?text={q}"),
    # nationaldistributorllc: only curated /product/ URLs — never /?s= search shells
    ("crcautocare.com", "https://crcautocare.com/?s={q}"),
    ("pexuniverse.com", "https://www.pexuniverse.com/search?q={q}"),
    ("staples.com", "https://www.staples.com/search?query={q}"),
    ("officedepot.com", "https://www.officedepot.com/catalog/search.do?Ntt={q}"),
    ("globalindustrial.com", "https://www.globalindustrial.com/search?q={q}"),
    ("thedieselstore.com", "https://www.thedieselstore.com/search?type=product&q={q}"),
    ("maxtran.com", "https://maxtran.com/?s={q}"),
    ("autobuffy.com", "https://autobuffy.com/?s={q}"),
]


def curated_exact_urls(mpn: str) -> list[str]:
    urls: list[str] = []
    for v in mpn_variants(mpn):
        urls.extend(_EXACT_URLS.get(v.upper(), []) or _EXACT_URLS.get(v, []) or [])
    try:
        from exact_product_url_discovery.seed_candidates import SEED_CANDIDATES

        for v in mpn_variants(mpn):
            urls.extend(SEED_CANDIDATES.get(v.upper(), []) or SEED_CANDIDATES.get(v, []) or [])
    except Exception:
        pass
    # de-dupe preserve order
    seen: set[str] = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def domain_search_urls(
    mpn: str,
    manufacturer: str | None,
    domains: list[str],
    *,
    limit_per_domain: int = 2,
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    variants = mpn_variants(mpn, manufacturer=manufacturer)[:4]
    want = {d.lower().replace("www.", "") for d in domains}
    for domain, tmpl in _DOMAIN_SEARCH:
        if domain not in want:
            continue
        n = 0
        for v in variants:
            if n >= limit_per_domain:
                break
            q = quote_plus(f"{manufacturer or ''} {v}".strip())
            pn = quote_plus(v.lower())
            url = tmpl.replace("{q}", q).replace("{pn}", v.lower()).replace("{PN}", v)
            # fix double-encoding if tmpl already expected raw
            if "{pn}" in tmpl:
                url = tmpl.replace("{q}", q).replace("{pn}", v.lower())
            rows.append((domain, url))
            n += 1
    return rows


def register_learned_url(mpn: str, url: str) -> None:
    key = (mpn or "").upper()
    if not key or not url:
        return
    lst = _EXACT_URLS.setdefault(key, [])
    if url not in lst:
        lst.insert(0, url)


def exact_url_seed_stats() -> dict[str, Any]:
    return {"seeded_mpns": len(_EXACT_URLS), "seeded_urls": sum(len(v) for v in _EXACT_URLS.values())}
