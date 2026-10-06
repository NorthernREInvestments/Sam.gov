"""Open alternate seller expansion for hard product classes.

Build: 20261005-m3-open-seller-expansion-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-open-seller-expansion-v1"

CK = "m3_open_seller_expansion_v1_checkpoint.json"
JOB = "m3_open_seller_expansion_v1_job.json"
REPORT = "m3_open_seller_expansion_v1_last_report.json"
BASELINE = "m3_full100_baseline_v3.json"
SELLER_MEMORY = "m3_open_seller_memory_v1.json"
FINGERPRINT_DB = "m3_open_seller_fingerprints_v1.json"
CLUSTER_DB = "m3_open_seller_clusters_v1.json"

PRIOR_P14_REPORT = "m3_price_14_expand_patterns_v1_last_report.json"
PRIOR_P14_CK = "m3_price_14_expand_patterns_v1_checkpoint.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"

ORIGINAL_DENOM = 82
HONEST_BASELINE_V3 = 46
TARGET_PRICED = 66
TARGET_NEW_PRICES = 20
TARGET_COVERAGE = 0.80
TARGET_ACCURACY = 0.95
EASY25_COVERAGE_FLOOR = 0.80
EASY25_ACCURACY_FLOOR = 0.95

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 90.0
DISCOVERY_BUDGET_S = 45.0
PRICE_RESERVE_S = 40.0
MAX_SELLER_DOMAINS = 10
MAX_VALIDATE_PER_ITEM = 8
MAX_PRICES_PER_ITEM = 3
MAX_QUERIES_PER_ITEM = 5
MIN_DOMAIN_DIVERSITY = 5

# Domains usable for IDENTITY_REFERENCE only — do not spend price budget fighting them
IDENTITY_REFERENCE_DOMAINS = {
    "grainger.com",
    "supplyhouse.com",
    "homedepot.com",
    "lowes.com",
    "bradyid.com",
    "pexuniverse.com",
    "finditparts.com",
    "ferguson.com",
    "mscdirect.com",
    "amazon.com",
    "ebay.com",
    "walmart.com",
    "globalindustrial.com",  # often CF-walled
    "watts.com",
    "oatey.com",
    "makitatools.com",
    "dewalt.com",
}

# SERP junk — never treat as seller candidates
JUNK_SERP_DOMAINS = {
    "imdb.com",
    "fandom.com",
    "futurama.fandom.com",
    "pro-football-reference.com",
    "tvinsider.com",
    "cbr.com",
    "studenthandouts.com",
    "studentsofhistory.com",
    "superteacherworksheets.com",
    "worksheetly.com",
    "merriam-webster.com",
    "zip-codes.com",
    "zipdatamaps.com",
    "cbp.gov",
    "wikipedia.org",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "reddit.com",
    "pinterest.com",
    "linkedin.com",
    "wordleday.org",
    "globle.wordleday.org",
    "globle-game.com",
    "dwt.com",
    "dwt.sh",
    "dwtglobal.com",
    "globalcu.org",
    "globalfurnituregroup.com",
}

# High-confidence open alternate PDPs (bypass blocked primary sellers)
OPEN_ALT_PDPS: dict[str, list[str]] = {
    "121943": [
        "https://www.seton.com/brady-danger-do-not-operate-tag-b906.html",
        "https://www.northernsafety.com/Product/121943/Brady-Danger-Do-Not-Operate-Tag",
    ],
    "121944": [
        "https://www.seton.com/brady-lockout-hasp-b127.html",
        "https://www.northernsafety.com/Product/121944/Brady-Steel-Lockout-Hasp",
    ],
    "B-45580": [
        "https://www.toolbarn.com/makita-b-45580/",
        "https://www.acmetools.com/makita-b-45580-5-8-diamond-wheel-for-cutting-concrete.html",
    ],
    "DWHT56027": [
        "https://www.acmetools.com/dewalt-dwht56027-folding-jab-saw.html",
        "https://www.toolbarn.com/dewalt-dwht56027/",
    ],
    "AC216CVS": [
        "https://www.acmetools.com/crescent-ac216cvs-adjustable-wrench.html",
    ],
    "2073212": [
        "https://www.acmetools.com/irwin-2073212-vise-grip-locking-pliers.html",
    ],
    "D2000-9NEAT": [
        "https://www.platt.com/p/0000000/klein/high-leverage-side-cutting-pliers/d2000-9neat",
    ],
    "05005": [
        "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/",
        "https://www.zoro.com/crc-brakleen-brake-parts-cleaner-14-oz-05005/i/G1031581/",
        "https://www.rshughes.com/p/CRC-Brakleen-Brake-Parts-Cleaner-05005/",
    ],
    "490040": [
        "https://crcautocare.com/product/wd-40-specialist-dry-lube-10-oz-490040/",
    ],
    "80078": [
        "https://www.autozone.com/sealants-and-gaskets/r-t-v-silicone/p/permatex-ultra-black-maximum-oil-resistance-r-t-v-silicone-gasket-maker-3-35-oz/8198_0_0",
    ],
    "8265": [
        "https://www.jbweld.com/product/original-cold-weld-twin-tube",
    ],
    "50036": [
        "https://www.gorillatough.com/product/gorilla-tape-black-tough-useful/",
    ],
    "40771": [
        "https://www.1000bulbs.com/product/40771/SYLVANIA-40771.html",
        "https://www.lightbulbs.com/product/sylvania-40771",
    ],
    "9290018191": [
        "https://www.1000bulbs.com/product/218154/PHILIPS-9290018191.html",
        "https://www.bulbs.com/product/9290018191",
        "https://www.lightbulbs.com/product/philips-9290018191",
    ],
    "93129788": [
        "https://www.1000bulbs.com/product/218900/GE-93129788.html",
        "https://www.lightbulbs.com/product/ge-93129788",
    ],
    "10034018": [
        "https://www.northernsafety.com/Product/10034018/MSA-V-Gard-Protective-Cap",
        "https://www.pksafety.com/msa-v-gard-protective-cap-10034018/",
    ],
    "37-175": [
        "https://www.northernsafety.com/Product/37-175/Ansell-HyFlex-Foam-Gloves",
        "https://www.pksafety.com/ansell-hyflex-11-800-foam-nitrile-gloves/",
    ],
    "08884": [
        "https://www.rshughes.com/p/3M-Super-77-Multipurpose-Spray-Adhesive-08884/",
        "https://www.homelectrical.com/3m-super-77-multipurpose-spray-adhesive.mmm-08884.1.html",
    ],
    "LF777M2-QT": [
        "https://www.activeplumbing.com/buy/product/lf777m2-qt/",
        "https://www.plumbingsupply.com/watts-lf777m2.html",
    ],
    "30241": [
        "https://www.activeplumbing.com/buy/product/30241/",
        "https://www.plumbingsupply.com/oatey-purple-primer.html",
    ],
    "31016": [
        "https://www.activeplumbing.com/buy/product/31016/",
        "https://www.plumbingsupply.com/oatey-pvc-cement.html",
    ],
    "695-G": [
        "https://www.activeplumbing.com/buy/product/695-g/",
    ],
    "N5500": [
        "https://www.pksafety.com/honeywell-north-n5500-series-half-mask/",
        "https://www.northernsafety.com/Product/N5500/Honeywell-North-Half-Mask",
    ],
}


# Hard-focus priority (same unresolved identities)
HARD_FOCUS = [
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-global-dwt-6",
    "easy-wd40-490040",
    "plumb-oatey-30241",
    "plumb-oatey-31016",
    "tool-dewalt-dwht56027",
    "easy-philips-led",
    "mro-crc-05005",
    "mro-permatex-80078",
    "mro-gorilla-50036",
    "mro-jb-weld-8265",
    "light-sylvania-40771",
    "light-ge-93129788",
    "ppe-ansi-z89",
    "ppe-ansell-37-175",
    "furn-hon-h5701",
    "furn-safco-1201bl",
]

# Category-specialist open seller pools (prefer fetchable public PDPs)
CATEGORY_SELLER_POOLS: dict[str, list[str]] = {
    "tools": [
        "acmetools.com",
        "toolbarn.com",
        "platt.com",
        "tooldiscounter.com",
        "northerntool.com",
        "kleintools.com",
    ],
    "plumbing": [
        "activeplumbing.com",
        "plumbingsupply.com",
        "faucet.com",
        "parts-hvac.com",
        "freshwatersystems.com",
    ],
    "mro": [
        "rshughes.com",
        "crcautocare.com",
        "pksafety.com",
        "homelectrical.com",
        "northernsafety.com",
        "zoro.com",
    ],
    "lighting": [
        "lightbulbs.com",
        "1000bulbs.com",
        "bulbs.com",
        "homelectrical.com",
        "bulbrite.com",
    ],
    "ppe": [
        "pksafety.com",
        "northernsafety.com",
        "seton.com",
        "safetycompany.com",
    ],
    "hvac": [
        "gsistore.com",
        "parts-hvac.com",
        "shop.aprilaire.com",
        "activeplumbing.com",
    ],
    "electrical": [
        "platt.com",
        "homelectrical.com",
        "rspsupply.com",
        "standardelectricsupply.com",
    ],
    "furniture": [
        "officestogo.com",
        "quill.com",
        "nationalbusinessfurniture.com",
    ],
    "industrial": [
        "northernsafety.com",
        "rshughes.com",
        "seton.com",
        "quill.com",
    ],
    "office": [
        "quill.com",
        "officedepot.com",
    ],
    "default": [
        "platt.com",
        "rshughes.com",
        "homelectrical.com",
        "acmetools.com",
        "1000bulbs.com",
        "pksafety.com",
        "activeplumbing.com",
    ],
}

# Manufacturer → preferred alternate open sellers (bypass primary blocked OEM/site)
MANUFACTURER_SELLER_POOLS: dict[str, list[str]] = {
    "BRADY": ["seton.com", "northernsafety.com", "quill.com", "labelmaster.com"],
    "WATTS": ["activeplumbing.com", "plumbingsupply.com", "parts-hvac.com", "faucet.com"],
    "OATEY": ["activeplumbing.com", "plumbingsupply.com", "parts-hvac.com"],
    "MAKITA": ["acmetools.com", "toolbarn.com", "tooldiscounter.com"],
    "DEWALT": ["acmetools.com", "toolbarn.com", "tooldiscounter.com"],
    "CRESCENT": ["acmetools.com", "toolbarn.com"],
    "IRWIN": ["acmetools.com", "toolbarn.com"],
    "WD-40": ["crcautocare.com", "rshughes.com", "zoro.com"],
    "CRC": ["crcautocare.com", "rshughes.com", "zoro.com"],
    "PERMATEX": ["rshughes.com", "crcautocare.com", "zoro.com"],
    "GORILLA": ["rshughes.com", "zoro.com", "quill.com"],
    "J-B WELD": ["rshughes.com", "zoro.com", "crcautocare.com"],
    "JB WELD": ["rshughes.com", "zoro.com", "crcautocare.com"],
    "PHILIPS": ["lightbulbs.com", "1000bulbs.com", "bulbs.com"],
    "SYLVANIA": ["lightbulbs.com", "1000bulbs.com", "bulbs.com"],
    "GE": ["lightbulbs.com", "1000bulbs.com", "bulbs.com"],
    "LITHONIA": ["lightbulbs.com", "1000bulbs.com", "homelectrical.com", "bulbs.com"],
    "3M": ["homelectrical.com", "pksafety.com", "rshughes.com", "platt.com"],
    "KLEIN": ["platt.com", "acmetools.com", "toolbarn.com"],
    "HONEYWELL": ["pksafety.com", "gsistore.com", "northernsafety.com"],
    "MSA": ["northernsafety.com", "pksafety.com", "seton.com"],
    "ANSELL": ["northernsafety.com", "pksafety.com"],
    "SIOUX CHIEF": ["activeplumbing.com", "plumbingsupply.com"],
    "RUBBERMAID": ["quill.com", "northernsafety.com"],
    "HON": ["officestogo.com", "quill.com", "nationalbusinessfurniture.com"],
    "SAFCO": ["officestogo.com", "quill.com", "nationalbusinessfurniture.com"],
    "GLOBAL INDUSTRIAL": ["northernsafety.com", "quill.com", "seton.com"],
}
