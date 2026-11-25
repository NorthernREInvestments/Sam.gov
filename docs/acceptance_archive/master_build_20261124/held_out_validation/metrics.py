"""Phase 14 metrics on the v3 held-out sample (provisional AI annotation, two blind annotators).

Usage: p14_metrics.py DIR [DIR ...]   (each DIR has answer_key.json, blind_items.json, labels_{A,B}_batch_{1,2}.json)
"""
import csv, json, math, random, sys, collections
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
PROD = {"PRODUCT_PURCHASE", "PRODUCT_WITH_SERVICE"}
dirs = [Path(d) for d in sys.argv[1:]]


def load(d):
    key = json.load(open(d / "answer_key.json", encoding="utf-8"))
    blind = {x["item"]: x for x in json.load(open(d / "blind_items.json", encoding="utf-8"))}
    lab = {}
    for who in ("A", "B"):
        lab[who] = {}
        for b in (1, 2):
            p = d / f"labels_{who}_batch_{b}.json"
            if p.exists():
                for x in json.load(open(p, encoding="utf-8")):
                    lab[who][int(x["item"])] = x
    return key, blind, lab


items = []
pops = collections.Counter()
for d in dirs:
    key, blind, lab = load(d)
    tag = d.name
    for s, n in key["population"].items():
        pops[(tag, s)] += n
    for k in key["items"]:
        i = k["item"]
        a, b = lab["A"].get(i), lab["B"].get(i)
        if not a or not b:
            continue
        items.append({**k, "set": tag, "A": a, "B": b, "text": blind[i]})
print("labelled items with both annotators:", len(items))
strata = collections.Counter((x["set"], x["stratum"]) for x in items)
W = {s: pops[s] / n for s, n in strata.items()}


def is_prod(lbl):
    return lbl["acquisition_type"] in PROD


def truth(x, concept, mode):
    a, b = x["A"], x["B"]
    if concept == "product":
        va, vb = is_prod(a), is_prod(b)
    elif concept == "confirmed":
        va, vb = is_prod(a) and a["product_evidence"] == "EXPLICIT", is_prod(b) and b["product_evidence"] == "EXPLICIT"
    elif concept == "potential":
        va = is_prod(a) or (a["acquisition_type"] == "UNCLEAR" and a["product_evidence"] != "NONE")
        vb = is_prod(b) or (b["acquisition_type"] == "UNCLEAR" and b["product_evidence"] != "NONE")
    elif concept == "quotable":
        va = is_prod(a) and a["identity_level"] in {"EXACT", "SPECIFIED"}
        vb = is_prod(b) and b["identity_level"] in {"EXACT", "SPECIFIED"}
    else:
        raise ValueError(concept)
    return (va and vb) if mode == "both" else (va or vb)


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {"k": k, "n": n, "p": round(p, 4), "ci95": [round(c - h, 4), round(c + h, 4)]}


def kappa(f):
    n = len(items)
    agree = sum(1 for x in items if f(x["A"]) == f(x["B"]))
    ca = collections.Counter(f(x["A"]) for x in items)
    cb = collections.Counter(f(x["B"]) for x in items)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return round((agree / n - pe) / (1 - pe), 3) if pe < 1 else None, round(agree / n, 3)


def weighted_recall(sample, concept, mode, engine_flag):
    tp = sum(W[(x["set"], x["stratum"])] for x in sample if truth(x, concept, mode) and x[engine_flag])
    pos = sum(W[(x["set"], x["stratum"])] for x in sample if truth(x, concept, mode))
    return tp / pos if pos else None, tp, pos


def boot(concept, mode, flag, reps=2000):
    by = collections.defaultdict(list)
    for x in items:
        by[(x["set"], x["stratum"])].append(x)
    rnd = random.Random(7)
    vals = []
    for _ in range(reps):
        s = [rnd.choice(v) for v in by.values() for _ in v]
        r, _, _ = weighted_recall(s, concept, mode, flag)
        if r is not None:
            vals.append(r)
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 4), round(vals[int(0.975 * len(vals)) - 1], 4)] if vals else None


res = {"provisional": "Two blind AI annotators (same model family); not human ground truth", "n_items": len(items),
       "population_by_stratum": {f"{a}:{b}": n for (a, b), n in pops.items()},
       "sample_by_stratum": {f"{a}:{b}": n for (a, b), n in strata.items()}}
res["inter_annotator"] = {
    "product_vs_not": kappa(lambda l: is_prod(l)),
    "acquisition_type": kappa(lambda l: l["acquisition_type"]),
    "product_evidence": kappa(lambda l: l["product_evidence"]),
    "identity_level": kappa(lambda l: l["identity_level"]),
    "quantity_stated": kappa(lambda l: l["quantity_stated"]),
}
S1 = [x for x in items if x["stratum"] == "S1_explicit"]
ret = [x for x in items if x["engine_retained"]]
rej = [x for x in items if not x["engine_retained"]]
det = [x for x in items if x["engine_detected"]]
for mode in ("both", "either"):
    m = {}
    r, tp, pos = weighted_recall(items, "product", mode, "engine_retained")
    m["broad_discovery_recall(retention)"] = {"estimate": round(r, 4), "weighted_tp": round(tp), "weighted_positives": round(pos),
                                               "ci95_bootstrap": boot("product", mode, "engine_retained"), "target": 0.98}
    r, tp, pos = weighted_recall(items, "product", mode, "engine_detected")
    m["product_detection_recall"] = {"estimate": round(r, 4), "weighted_tp": round(tp), "weighted_positives": round(pos),
                                     "ci95_bootstrap": boot("product", mode, "engine_detected")}
    m["confirmed_precision(EXPLICIT->explicit product)"] = {**wilson(sum(truth(x, "confirmed", mode) for x in S1), len(S1)), "target": 0.95}
    m["explicit_is_product_precision(EXPLICIT->any product)"] = wilson(sum(truth(x, "product", mode) for x in S1), len(S1))
    wp = sum(W[(x["set"], x["stratum"])] for x in ret if truth(x, "potential", mode))
    wn = sum(W[(x["set"], x["stratum"])] for x in ret)
    m["potential_precision(retained->potential product)"] = {"estimate": round(wp / wn, 4), "target": 0.80,
                                                             "by_stratum": {s: wilson(sum(truth(x, "potential", mode) for x in ret if x["stratum"] == s),
                                                                                      sum(1 for x in ret if x["stratum"] == s)) for s in ("S1_explicit", "S2_likely", "S3_retained_other")}}
    m["rejection_npv"] = wilson(sum(not truth(x, "product", mode) for x in rej), len(rej))
    cm = collections.Counter()
    for x in items:
        cm[("retained" if x["engine_retained"] else "rejected", "product" if truth(x, "product", mode) else "not_product")] += W[(x["set"], x["stratum"])]
    m["confusion_matrix_weighted"] = {f"{a}|{b}": round(v) for (a, b), v in cm.items()}
    cms = collections.Counter(("retained" if x["engine_retained"] else "rejected", "product" if truth(x, "product", mode) else "not_product") for x in items)
    m["confusion_matrix_sample"] = {f"{a}|{b}": v for (a, b), v in cms.items()}
    res[f"consensus_{mode}"] = m

# Engine presence x annotator acquisition type (A and B agree only)
ct = collections.Counter()
for x in items:
    if x["A"]["acquisition_type"] == x["B"]["acquisition_type"]:
        ct[(x["engine_presence"], x["A"]["acquisition_type"])] += 1
res["engine_presence_x_agreed_type"] = {f"{a}|{b}": v for (a, b), v in sorted(ct.items())}


def slice_metrics(field):
    out = {}
    for v in sorted({str(x.get(field)) for x in items}):
        sub = [x for x in items if str(x.get(field)) == v]
        if len(sub) < 15:
            continue
        s1 = [x for x in sub if x["stratum"] == "S1_explicit"]
        pos = [x for x in sub if truth(x, "product", "both")]
        out[v] = {"n": len(sub), "explicit_confirmed_precision": wilson(sum(truth(x, "confirmed", "both") for x in s1), len(s1)),
                  "sample_recall_unweighted": wilson(sum(x["engine_retained"] for x in pos), len(pos))}
    return out


res["by_source"] = slice_metrics("source")
res["by_evidence"] = slice_metrics("evidence")
res["by_engine_category"] = slice_metrics("engine_category")
# Identity: engine sourcing identity vs annotator quotable identity (both)
idm = collections.Counter((str(x.get("identity_level")), "quotable" if truth(x, "quotable", "both") else "not") for x in items if x["engine_retained"])
res["identity_engine_x_annotator"] = {f"{a}|{b}": v for (a, b), v in sorted(idm.items())}

d0 = dirs[0]
(d0 / "metrics_v3.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
print(json.dumps({k: v for k, v in res.items() if k not in {"by_source", "by_engine_category"}}, indent=1))

# Human-review package: disagreements, false negatives, explicit false positives
rows = []
for x in items:
    a, b = x["A"], x["B"]
    reasons = []
    if is_prod(a) != is_prod(b):
        reasons.append("annotator_disagreement")
    if not x["engine_retained"] and (is_prod(a) or is_prod(b)):
        reasons.append("possible_false_negative")
    if x["stratum"] == "S1_explicit" and not (truth(x, "confirmed", "either")):
        reasons.append("explicit_not_confirmed")
    if reasons:
        rows.append({"review_reason": "+".join(reasons), "set": x["set"], "item": x["item"], "source": x["source"], "staged_id": x["staged_id"],
                     "engine_presence": x["engine_presence"], "engine_category": x["engine_category"],
                     "A_type": a["acquisition_type"], "A_evidence": a["product_evidence"], "B_type": b["acquisition_type"],
                     "B_evidence": b["product_evidence"], "A_note": a.get("note"), "B_note": b.get("note"),
                     "title": x["text"]["title"], "buyer": x["text"]["buyer"], "description": (x["text"]["description"] or "")[:400],
                     "human_label": "", "human_comment": ""})
rows.sort(key=lambda r: ("possible_false_negative" not in r["review_reason"], "explicit_not_confirmed" not in r["review_reason"]))
with (d0 / "human_review_package.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["none"])
    w.writeheader()
    w.writerows(rows)
print("human review rows:", len(rows), collections.Counter(r["review_reason"] for r in rows))
