"""Quick probe rediscover+extract on a few hard items."""
from known_pdp_price_extraction.process import process_product
from price_coverage_80.corpus import load_corpus

corpus = {i["benchmark_id"]: i for i in load_corpus()["items"]}
for bid in ["easy-philips-led", "light-ge-93129788", "mro-crc-05005", "tool-klein-d2000-9neat", "mro-3m-08884"]:
    item = dict(corpus[bid])
    item["pdps"] = []  # force rediscover path
    print("===", bid)
    r = process_product(item, stats={})
    print(r.get("status"), r.get("price"), r.get("seller"), r.get("url"), "attempts", r.get("n_attempts"))
