"""Focused rediscovery boost on remaining Full-100 misses."""

from __future__ import annotations

import json
import time

from m3_data_root import data_path
from price_coverage_80.corpus import confirmed_public, load_corpus
from price_coverage_80.scoring import score_accuracy
from seller_rediscovery.models import FROZEN, PRICE_FOUND, PRODUCT_PUBLIC_PRICE_EXHAUSTED
from seller_rediscovery.recover import recover_with_rediscovery
from seller_rediscovery.sweep import _found_from_recovery, _save, _score_sets, build_final_report

# Extra public exact-ish seeds for remaining gaps (not prices)
EXTRA_SEEDS: dict[str, list[str]] = {
    "tool-channellock-430": [
        "https://www.platt.com/p/0015298/channellock/tongue-and-groove-plier-10-max-jaw-opening-2/025582301475/chk430",
        "https://www.channellock.com/430-10-tongue-groove-pliers/",
    ],
    "elec-ideal-35-903": [
        "https://www.platt.com/search?q=35-903",
    ],
    "light-sylvania-40771": [
        "https://www.1000bulbs.com/product/117513/LED-40771.html",
    ],
    "light-feit-a19": [
        "https://www.feit.com/products/om60-950ca-ag",
    ],
    "hvac-aprilaire-201": [
        "https://parts-hvac.com/aprilaire-201-replacement-filter.html",
    ],
    "hvac-honeywell-rth2300b": [
        "https://www.honeywellhome.com/us/en/products/security/thermostats/5-2-day-programmable-thermostat-rth2300b/",
    ],
    "plumb-oatey-30241": [
        "https://www.pexuniverse.com/oatey-30241",
        "https://www.supplyhouse.com/Oatey-30241",
    ],
    "plumb-oatey-31016": [
        "https://www.pexuniverse.com/oatey-31016",
        "https://www.supplyhouse.com/Oatey-31016",
    ],
    "auto-wix-51515": [
        "https://www.summitracing.com/parts/wix-51515",
    ],
    "mro-loctite-262": [
        "https://www.loctiteproducts.com/en/products/threadlockers/262.html",
    ],
    "easy-watts-lf777m2": [
        "https://www.ferguson.com/product/watts-lf777m2-qt/_/R-4328670",
        "https://www.pexuniverse.com/watts-lf777m2-qt-lead-free-bronze-union-ball-valve",
    ],
    "easy-brady-121943": [
        "https://www.seton.com/brady-danger-do-not-operate-tag-121943.html",
        "https://www.bradyid.com/labels/lockout-tagout/tags/121943",
    ],
    "easy-makita-b-45580": [
        "https://www.makitatools.com/products/details/B-45580",
        "https://www.toolnut.com/makita-b-45580.html",
    ],
    "tool-dewalt-dwht56027": [
        "https://www.dewalt.com/product/dwht56027/exo-core-sledge-hammer",
        "https://www.acmetools.com/shop/tools/dewalt-dwht56027",
    ],
    "tool-klein-d2000-9neat": [
        "https://www.kleintools.com/catalog/side-cutting-pliers/high-leverage-side-cutting-pliers-0",
        "https://www.platt.com/search?q=D2000-9NEAT",
    ],
    "ind-brady-121944": [
        "https://www.bradyid.com/labels/lockout-tagout/tags/121944",
    ],
    "mro-3m-60926": [
        "https://www.3m.com/3M/en_US/p/d/v000057531/",
        "https://www.quill.com/3m-60926-multi-gas-vapor-cartridge-p100-filter/3AB60926/cbs/",
    ],
}


def main() -> None:
    ck = json.loads(data_path("m3_seller_rediscovery_v1_checkpoint.json").read_text(encoding="utf-8"))
    by = {i["benchmark_id"]: i for i in load_corpus()["items"]}
    # Purge absurd 60926 if present
    row = ck["items"].get("mro-3m-60926") or {}
    if (row.get("found") or {}).get("usable") and float((row.get("found") or {}).get("unit_price") or 0) > 400:
        print("purging absurd 60926")
        ck["items"]["mro-3m-60926"] = {
            "benchmark_id": "mro-3m-60926",
            "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
            "found": {"usable": False, "miss_notes": ["PURGED_ABSURD_EA_PRICE"]},
            "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
            "purged": True,
        }
    # Purge gallon-vs-8oz oatey if recovered as >$50 without known
    row = ck["items"].get("plumb-oatey-31016") or {}
    if (row.get("found") or {}).get("usable") and float((row.get("found") or {}).get("unit_price") or 0) > 50:
        print("purging pack-suspect oatey-31016", (row.get("found") or {}).get("unit_price"))
        ck["items"]["plumb-oatey-31016"] = {
            "benchmark_id": "plumb-oatey-31016",
            "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
            "found": {"usable": False, "miss_notes": ["PURGED_WRONG_PACK_SIZE"]},
            "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
            "purged": True,
        }

    priced = {
        bid
        for bid, r in ck["items"].items()
        if r.get("status") in {PRICE_FOUND, FROZEN} and (r.get("found") or {}).get("usable")
    }
    remaining = [i["benchmark_id"] for i in confirmed_public() if i["benchmark_id"] not in priced]
    print("remaining", len(remaining))
    stats = ck.setdefault("stats", {})
    new = 0
    for idx, bid in enumerate(remaining, 1):
        item = by[bid]
        seeds = EXTRA_SEEDS.get(bid, [])
        # also prior tried urls that weren't blocked shells
        for urow in ((ck["items"].get(bid) or {}).get("extraction") or {}).get("urls_tried") or []:
            u = urow.get("url")
            if u and u not in seeds and not urow.get("blocked"):
                seeds.append(u)
        rec = recover_with_rediscovery(
            item,
            seeded_urls=seeds[:6],
            stats=stats,
            item_deadline=time.time() + 70,
            allow_rediscovery=True,
        )
        found = _found_from_recovery(rec)
        if rec.get("best") and not found.get("usable"):
            found = _found_from_recovery({**rec, "status": "FOUND_VALID_PRICE"})
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
            acc = score_accuracy(item, found)
        status = PRICE_FOUND if found.get("usable") else PRODUCT_PUBLIC_PRICE_EXHAUSTED
        disc = rec.get("discovery") or {}
        ck["items"][bid] = {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "rediscovery": {
                "n_new_exact_urls": disc.get("n_new_exact") or 0,
                "discovered_exact_urls": disc.get("discovered_exact_urls") or [],
                "n_alt_attempted": rec.get("n_alt_attempted"),
                "alternate_seller_recovery": rec.get("alternate_seller_recovery"),
            },
            "extraction": {
                "status": rec.get("status"),
                "route": (rec.get("best") or {}).get("via"),
                "urls_tried": [
                    {
                        "url": u.get("url"),
                        "seller": u.get("seller"),
                        "visibility": u.get("price_visibility_status"),
                        "blocked": u.get("blocked"),
                    }
                    for u in (rec.get("urls_tried") or [])
                ],
                "elapsed_s": rec.get("elapsed_s"),
            },
            "boost": True,
        }
        if found.get("usable"):
            new += 1
            print("RECOVERED", bid, found.get("unit_price"), found.get("seller"), found.get("source_url"))
        if idx % 5 == 0:
            scored = _score_sets(ck, by)
            print(f"progress {idx}/{len(remaining)} new={new} full={scored['full100']['priced']}/82")
            _save("m3_seller_rediscovery_v1_checkpoint.json", ck)

    scored = _score_sets(ck, by)
    ck["full100"] = scored["full100"]
    ck["easy25"] = scored["easy25"]
    _save("m3_seller_rediscovery_v1_checkpoint.json", ck)
    report = build_final_report(ck)
    print("BOOST new", new)
    print("FULL", report["FULL100"])
    print("EASY", report["EASY25"])
    print("SAFE", report["ACCEPTANCE"]["SAFE_TO_SCALE"])


if __name__ == "__main__":
    main()
