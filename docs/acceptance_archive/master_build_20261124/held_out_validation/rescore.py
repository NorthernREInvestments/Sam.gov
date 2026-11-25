"""Re-score a blind sample against the CURRENT inventory (fixed engine) without touching labels.

Usage: p25_rescore.py SRC_DIR OUT_DIR LABEL_A LABEL_B
Writes OUT_DIR/answer_key.json with engine fields refreshed from inventory.jsonl by staged_id,
copies blind items, maps the two label files to labels_A_batch_1 / labels_B_batch_1, and prints
precision of the current engine's EXPLICIT / LIKELY / retained decisions (stratum-weighted).
"""
import collections
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
INV = r"C:\Users\M_gra\m3-worktrees\repair_data\staged_inventory\inventory.jsonl"
src, out, la, lb = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
out.mkdir(parents=True, exist_ok=True)
key = json.load(open(src / "answer_key.json", encoding="utf-8"))
want = {k["staged_id"] for k in key["items"]}
cur = {}
with open(INV, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        if r["staged_id"] in want and not r.get("duplicate_of"):
            cur[r["staged_id"]] = r
RETAINED = {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT", "POSSIBLE_PRODUCT", "INSUFFICIENT_EVIDENCE"}
missing = 0
for k in key["items"]:
    r = cur.get(k["staged_id"])
    if not r or not r.get("classification"):
        missing += 1
        k["engine_presence_now"] = None
        continue
    c = r["classification"]
    k["engine_presence_before_fix"] = k.get("engine_presence")
    k["engine_presence"] = c["product_presence"]
    k["engine_retained"] = c["product_presence"] in RETAINED
    k["engine_detected"] = c["product_presence"] in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT"}
    k["engine_category"] = c["procurement_category"]
    k["intake_decision"] = (r.get("qualification") or {}).get("intake_decision")
    k["identity_level"] = (r.get("sourcing") or {}).get("identity_level")
print("items", len(key["items"]), "not found in current inventory", missing)
json.dump(key, open(out / "answer_key.json", "w", encoding="utf-8"), indent=1)
shutil.copy(src / "blind_items.json", out / "blind_items.json")
shutil.copy(src / la, out / "labels_A_batch_1.json")
shutil.copy(src / lb, out / "labels_B_batch_1.json")

A = {x["item"]: x for x in json.load(open(src / la, encoding="utf-8"))}
B = {x["item"]: x for x in json.load(open(src / lb, encoding="utf-8"))}
PROD = {"PRODUCT_PURCHASE", "PRODUCT_WITH_SERVICE"}
strata_n = collections.Counter(k["stratum"] for k in key["items"])
W = {s: key["population"][s] / n for s, n in strata_n.items()}


def prod(i, mode):
    a, b = A[i]["acquisition_type"] in PROD, B[i]["acquisition_type"] in PROD
    return (a and b) if mode == "both" else (a or b)


def potential(i):
    def one(l):
        return l["acquisition_type"] in PROD or (l["acquisition_type"] == "UNCLEAR" and l["product_evidence"] != "NONE")
    return one(A[i]) or one(B[i])


items = [k for k in key["items"] if k.get("engine_presence") and k["item"] in A and k["item"] in B]
for mode in ("both", "either"):
    print(f"--- consensus {mode}")
    for label, sel in (("EXPLICIT", lambda k: k["engine_presence"] == "EXPLICIT_PRODUCT"),
                       ("LIKELY", lambda k: k["engine_presence"] == "LIKELY_PRODUCT"),
                       ("PROCEED(EXPLICIT+LIKELY)", lambda k: k["engine_detected"])):
        s = [k for k in items if sel(k)]
        wt = sum(W[k["stratum"]] for k in s)
        wp = sum(W[k["stratum"]] for k in s if prod(k["item"], mode))
        print(f"{label}: n={len(s)} raw_precision={sum(prod(k['item'], mode) for k in s)}/{len(s)} weighted={wp / wt:.3f}" if s else f"{label}: n=0")
    pos = [k for k in items if prod(k["item"], mode)]
    wpos = sum(W[k["stratum"]] for k in pos)
    print("retention recall weighted", round(sum(W[k["stratum"]] for k in pos if k["engine_retained"]) / wpos, 4) if wpos else None,
          "| detection recall weighted", round(sum(W[k["stratum"]] for k in pos if k["engine_detected"]) / wpos, 4) if wpos else None,
          "| positives n", len(pos))
    ret = [k for k in items if k["engine_retained"]]
    print("retained potential-precision weighted",
          round(sum(W[k["stratum"]] for k in ret if potential(k["item"])) / sum(W[k["stratum"]] for k in ret), 4))
    fn = [k for k in items if prod(k["item"], mode) and not k["engine_retained"]]
    print("false negatives (rejected but product):", [(k["item"], k["title"][:70] if "title" in k else "", A[k["item"]]["note"]) for k in fn])
fp = [k for k in items if k["engine_detected"] and not prod(k["item"], "either")]
print("detected but neither annotator says product:")
for k in fp:
    print("  ", k["item"], k["engine_presence"], k.get("engine_presence_before_fix"), "|", A[k["item"]]["acquisition_type"], B[k["item"]]["acquisition_type"], "|", A[k["item"]]["note"])
changed = collections.Counter((k.get("engine_presence_before_fix"), k["engine_presence"]) for k in items if k.get("engine_presence_before_fix") != k["engine_presence"])
print("presence changes since draw", dict(changed))
