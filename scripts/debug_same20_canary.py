"""Fast local SAME-20 canary: 1–3 frozen opps, no full production loop.

Usage:
  python scripts/debug_same20_canary.py
  python scripts/debug_same20_canary.py b40059ab7b97fcd6 ccb8dfe0e5772614
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_engine.same20_corpus import load_corpus, SAME20_STABLE_KEYS
    from bidnet_engine.package_materialization import materialize_attachments, PATCH
    from bidnet_engine.schedule_content_recognition import inspect_package_documents
    from phase_l.l23_full_population_funnel import load_store

    keys = [k for k in sys.argv[1:] if k.strip()] or [
        "b40059ab7b97fcd6",  # Vehicle Items — lines but AUTH missing
        "ccb8dfe0e5772614",  # Welding Lab — classic NO_AUTH
    ]
    keys = keys[:3]
    corpus = load_corpus()
    opps = {str(o.get("stable_key")): o for o in (corpus.get("opportunities") or []) if isinstance(o, dict)}
    store = load_store() or {}

    client = BidNetAuthenticatedClient()
    auth = client.ensure_authenticated()
    if not auth.authenticated:
        print("AUTH_FAIL", auth.status, auth.message)
        return 2
    print("PATCH", PATCH, "keys", keys, flush=True)

    summaries = []
    t_all = time.perf_counter()
    try:
        for sk in keys:
            item = opps.get(sk) or {"stable_key": sk}
            cid = str(item.get("canonical_opportunity_id") or sk)
            row = store.get(cid) or store.get(sk) or {}
            title = str(item.get("title") or row.get("title") or sk)
            detail = (
                item.get("detail_url")
                or row.get("detail_url")
                or row.get("source_url")
                or ""
            )
            docs = list(item.get("attachments_metadata") or row.get("attachments_metadata") or [])
            t0 = time.perf_counter()
            print(f"\n=== {sk} | {title[:70]} ===", flush=True)
            pm = materialize_attachments(
                docs,
                opportunity_id=cid,
                client=client,
                title=title,
                buyer=str(item.get("buyer") or row.get("buyer") or ""),
                detail_url=str(detail or ""),
            )
            mats = pm.get("materialized") or []
            insp = inspect_package_documents(mats) if mats else {}
            elapsed = round(time.perf_counter() - t0, 1)
            summary = {
                "stable_key": sk,
                "title": title[:80],
                "elapsed_s": elapsed,
                "valid_local": pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED")
                or pm.get("VALID_LOCAL_DOCUMENT_COUNT")
                or len(mats),
                "auth": bool(pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND")),
                "extracted": insp.get("EXTRACTED_PRODUCT_LINES") or pm.get("EXTRACTED_PRODUCT_LINES"),
                "classification": insp.get("classification") or pm.get("product_classification"),
                "blocker": pm.get("primary_blocker"),
                "discovery": pm.get("DISCOVERY"),
                "files": [
                    {
                        "filename": m.get("filename"),
                        "bytes": m.get("BYTE_SIZE") or m.get("byte_size"),
                        "high_value": m.get("high_value"),
                        "role": m.get("document_role_content") or m.get("document_role_guess"),
                        "lines": m.get("extracted_line_count"),
                    }
                    for m in mats[:8]
                ],
            }
            summaries.append(summary)
            print(json.dumps(summary, indent=2, default=str)[:2500], flush=True)
            if elapsed > 180:
                print("WARN opp exceeded 180s — stop canary", flush=True)
                break
    finally:
        try:
            client.close()
        except Exception:
            pass

    out = ROOT / "data" / "m3_same20_debug_canary.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "patch": PATCH,
        "total_s": round(time.perf_counter() - t_all, 1),
        "summaries": summaries,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\nTOTAL", payload["total_s"], "s ->", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
