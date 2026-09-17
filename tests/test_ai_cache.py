"""Cache persistence tests — no OpenAI calls."""

from __future__ import annotations

import uuid

import ai_analysis_cache as cache


def test_cache_key_fits_appsetting_64():
    fp = "a" * 64
    key = cache._cache_key(fp)
    assert len(key) == 64


def test_cache_write_commit_read_roundtrip():
    fp = f"testfp_{uuid.uuid4().hex}"
    # Normalize to 64-hex style fingerprint
    fp = cache.hash_source_material(fp)
    payload = '{"category":"PRODUCT_RESELL","buying":"widgets","advance":true}'
    ok = cache.put_cached_analysis(fp, result_text=payload, meta={"test": True})
    assert ok is True
    got = cache.get_cached_analysis(fp)
    assert got is not None
    assert got["result_text"] == payload
    assert got["fingerprint"] == fp
