"""Validate newly discovered open product URL candidates."""
from exact_product_url_discovery.validate_identity import validate_product_page

cands = [
    ("hvac-aprilaire-201", "Aprilaire", "201", "https://shop.aprilaire.com/products/aprilaire-201-replacement-filter"),
    ("hvac-resideo-th6220u2000", "Resideo", "TH6220U2000", "https://www.gsistore.com/products/resideo-th6220u2000-thermostat"),
    ("plumb-sioux-886-GP", "Sioux Chief", "886-GP", "https://siouxchief.com/Products/886-GP"),
    ("plumb-sioux-886-GP", "Sioux Chief", "886-GP", "https://www.activeplumbing.com/buy/product/886-gp/130861"),
    ("ppe-ansi-z89", "MSA", "10034018", "https://us.msasafety.com/pn/10034018"),
    ("ppe-ansell-37-175", "Ansell", "37-175", "https://pksafety.com/products/ansell-flock-lined-nitrile-glove-37-175-12-pairs"),
    ("light-lithonia-2gtl4", "Lithonia", "2GTL4", "https://www.stateelectric.com/products/lithonia-lighting-2gtl4-a12-120-lp840"),
    ("mro-jb-weld-8265", "J-B Weld", "8265", "https://www.jbweld.com/product/j-b-weld-twin-tube"),
    ("mro-3m-5p71", "3M", "5P71", "https://www.homelectrical.com/6000-7000-series-half-and-full-facepiece-cartridges-filters.mco-5p71.1.html"),
    ("mro-3m-60926", "3M", "60926", "https://www.veronasafety.com/3m-replacement-cartridges-and-filters--12"),
    ("easy-3m-2097", "3M", "2097", "https://www.3m.com/3M/en_US/p/d/b5005625029/"),
    ("tool-channellock-430", "Channellock", "430", "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier/025582301475/chk430"),
    ("elec-klein-11055", "Klein", "11055", "https://www.platt.com/p/0497361/klein/wire-stripper-cutter-10-18-awg/092644740572/kle11055"),
    ("auto-wix-51515", "WIX", "51515", "https://www.rockauto.com/en/parts/wix,51515,oil+filter,5340"),
]

for bid, mfr, mpn, url in cands:
    v = validate_product_page(url, mpn=mpn, manufacturer=mfr, allow_browser=True)
    print(
        "PASS" if v.get("identity_match") else "FAIL",
        bid,
        v.get("reason") or v.get("via"),
        (v.get("title") or "")[:50],
        url[:70],
    )
