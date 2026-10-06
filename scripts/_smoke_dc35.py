from deep_completion_3to5.corpus import freeze_deep_completion_corpus
from deep_completion_3to5.process import process_opportunity

c = freeze_deep_completion_corpus(force=True)
print("corpus", c["material_lines"], c["usable_identities"], c["quote_required"])
items = {i["opportunity_id"]: i for i in c["items"]}
by = {}
for ln in c["lines"]:
    by.setdefault(ln["opportunity_id"], []).append(ln)

for oid in [
    "opengov:go-metro:298984",
    "opengov:dekalbcountyga:286698",
    "opengov:bridgeportct:299806",
    "opengov:daniabeachfl:284328",
    "opengov:collier-county-fl:295143",
]:
    r = process_opportunity(items[oid], by[oid], skip_public_check=True)
    qp = r["quote_packets"]
    t = r["target_economics"]
    print(
        oid.split(":")[-1],
        "packets",
        qp["packet_count"],
        "cov",
        qp["coverage"],
        "ready",
        sum(1 for p in qp["quote_packets"] if p["ready_for_owner_quote_outreach"]),
        "rev",
        r["revenue"]["PASS_FAIL"],
        r["revenue"]["classification"],
        "val",
        r["revenue"].get("value"),
        "5k",
        t.get("MAX_ACQUISITION_COST_FOR_$5K_PROFIT"),
        "exec",
        r["execution"]["PASS_FAIL"],
        "next",
        r["owner_view"]["next_action"],
    )
