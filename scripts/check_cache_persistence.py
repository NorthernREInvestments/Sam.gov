"""Standalone cache persistence check — no OpenAI."""
from ai_analysis_cache import get_cached_analysis, hash_source_material, put_cached_analysis

fp = hash_source_material("cache-persistence-self-test-v2")
payload = '{"ok":true,"v":2}'
ok = put_cached_analysis(fp, result_text=payload, meta={"t": "persist"})
got = get_cached_analysis(fp)
passed = bool(ok and got and got.get("result_text") == payload)
print("CACHE_PERSISTENCE_TEST", "PASS" if passed else "FAIL")
print("write_ok", ok)
print("read_ok", bool(got))
raise SystemExit(0 if passed else 1)
