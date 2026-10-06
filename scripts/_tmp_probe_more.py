"""Quick probe activeplumbing + homelectrical + gsistore patterns."""
from exact_product_url_discovery.validate_identity import validate_product_page

probes = [
    ("Sioux Chief", "695-G", "https://www.activeplumbing.com/buy/product/695-g/"),
    ("Sioux Chief", "695-G", "https://www.activeplumbing.com/buy/product/695-g/130000"),
    ("Oatey", "30241", "https://www.activeplumbing.com/buy/product/30241/"),
    ("Oatey", "31016", "https://www.activeplumbing.com/buy/product/31016/"),
    ("Watts", "LF777M2-QT", "https://www.activeplumbing.com/buy/product/lf777m2-qt/"),
    ("Watts", "LFN45BMU-3/4", "https://www.activeplumbing.com/buy/product/lfn45bmu-3-4/"),
    ("3M", "2097", "https://www.homelectrical.com/particulate-filter-p100-w-nuisance-ov.mmm-2097.1.html"),
    ("3M", "60926", "https://www.homelectrical.com/multi-gas-vapor-cartridge-filter-p100.mmm-60926.1.html"),
    ("3M", "08884", "https://www.homelectrical.com/search?q=08884"),
    ("Honeywell", "RTH2300B", "https://www.gsistore.com/products/honeywell-rth2300b"),
    ("Honeywell", "RTH2300B", "https://www.gsistore.com/products/rth2300b"),
    ("CRC", "05005", "https://www.homelectrical.com/search?q=CRC+05005"),
]

for mfr, mpn, url in probes:
    v = validate_product_page(url, mpn=mpn, manufacturer=mfr, allow_browser=False)
    print(("PASS" if v.get("identity_match") else "FAIL"), mpn, v.get("reason") or v.get("via"), (v.get("title") or "")[:55], url[:80])
