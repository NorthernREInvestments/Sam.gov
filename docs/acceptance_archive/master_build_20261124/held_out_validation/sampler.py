"""Phase 14 held-out sample (v3): stratified by engine output, excluding every record already inspected.

Usage: p14_sample.py OUT_DIR [--bidnet-only]
"""
import json, os, random, re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("SECRET_KEY", "offline")
sys.path.insert(0, r"C:\Users\M_gra\m3-worktrees\procurement-engine-repair")
from m3.config import get_settings
from m3.research import staged_inventory as si

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
bidnet_only = "--bidnet-only" in sys.argv


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


# Whole-population listings (never labelled or inspected record-by-record)
POPULATION_DUMPS = {"compact_inscope.jsonl", "freesource_open_population.jsonl"}
corpus_parts = []
for base in (Path(r"C:\Users\M_gra\m3-worktrees\review_scratch"), Path(r"C:\Users\M_gra\m3-worktrees\procurement-engine-repair\tests"),
             Path(r"C:\Users\M_gra\.cursor\projects\c-Users-M-gra-OneDrive-Desktop-Sam-Gov-App\agent-tools")):
    for p in base.rglob("*"):
        if p.is_file() and p.stat().st_size < 3_000_000 and "blind_v3" not in p.parts and "staged_dev" not in p.parts and p.name not in POPULATION_DUMPS and p.suffix in {".py", ".json", ".jsonl", ".txt", ".out", ".md", ".csv", ""}:
            try:
                corpus_parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
corpus = "\n".join(corpus_parts)
ncorpus = norm(corpus)
print("exclusion corpus chars", len(corpus))

inv = [json.loads(l) for l in open(r"C:\Users\M_gra\m3-worktrees\repair_data\staged_inventory\inventory.jsonl", encoding="utf-8")]
cand = []
excluded = 0
for r in inv:
    c = r.get("classification")
    if not c or r.get("duplicate_of"):
        continue
    if bidnet_only != (r.get("source") == "bidnet"):
        continue
    t = norm(r.get("title"))
    ids = [x for x in (r.get("source_record_id"), r.get("staged_id"), r.get("provider_record_id")) if x]
    if any(str(x) in corpus for x in ids) or (len(t) >= 18 and t[:60] in ncorpus):
        excluded += 1
        continue
    cand.append(r)
print("candidates", len(cand), "excluded as previously inspected", excluded)


def stratum(r):
    c = r["classification"]
    if c["product_presence"] == "EXPLICIT_PRODUCT":
        return "S1_explicit"
    if c["product_presence"] == "LIKELY_PRODUCT":
        return "S2_likely"
    if c["discovery_retained"]:
        return "S3_retained_other"
    return "S4_rejected"


sizes = {"S1_explicit": 120, "S2_likely": 60, "S3_retained_other": 50, "S4_rejected": 120}
if bidnet_only:
    sizes = {"S1_explicit": 25, "S2_likely": 15, "S3_retained_other": 15, "S4_rejected": 25}
pop = {}
by = {}
for r in cand:
    by.setdefault(stratum(r), []).append(r)
random.seed(20261124 + (7 if bidnet_only else 0))
pick = []
for s, n in sizes.items():
    pool = by.get(s, [])
    pop[s] = len(pool)
    pick += random.sample(pool, min(n, len(pool)))
random.shuffle(pick)
print("population by stratum", pop)

s = get_settings()
want = {r["source_record_id"] for r in pick}
text = {}
loaders = [lambda: si.load_bidnet_snapshot(s.bidnet_snapshot_root())] if bidnet_only else [
    lambda: si.load_legacy_bidnet(s.legacy_paths()), lambda: si.load_nextbid(s.data_root()), lambda: si.load_free_sources(s.data_root())]
for loader in loaders:
    recs = [r for r in loader()["records"] if r["source_record_id"] in want]
    si.attach_documents(recs, s.staged_inventory_root() / "documents")
    for r in recs:
        text[r["source_record_id"]] = r
missing = want - set(text)
print("text found", len(text), "missing", len(missing))

blind, key = [], []
n = 0
for r in pick:
    t = text.get(r["source_record_id"])
    if not t:
        continue
    n += 1
    blind.append({"item": n, "title": t.get("title"), "buyer": t.get("buyer"), "state": t.get("state"),
                  "description": (t.get("description") or "")[:700], "document_excerpt": (t.get("document_text") or "")[:700]})
    c = r["classification"]
    key.append({"item": n, "source_record_id": r["source_record_id"], "staged_id": r["staged_id"], "source": r["source"],
                "stratum": stratum(r), "engine_presence": c["product_presence"], "engine_detected": c["product_detected"],
                "engine_retained": c["discovery_retained"], "engine_category": c["procurement_category"],
                "evidence": c["evidence_completeness"], "identity_level": (r.get("sourcing") or {}).get("identity_level"),
                "queue": r.get("owner_queue")})
(out / "blind_items.json").write_text(json.dumps(blind, indent=1, ensure_ascii=False), encoding="utf-8")
(out / "answer_key.json").write_text(json.dumps({"population": pop, "sizes": sizes, "excluded_inspected": excluded, "items": key}, indent=1), encoding="utf-8")
half = len(blind) // 2
(out / "batch_1.json").write_text(json.dumps(blind[:half], indent=1, ensure_ascii=False), encoding="utf-8")
(out / "batch_2.json").write_text(json.dumps(blind[half:], indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote", len(blind), "items; batches", half, len(blind) - half)
