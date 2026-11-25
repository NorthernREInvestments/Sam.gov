"""Persistent NextBid response cache — never pay twice for unchanged data."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_root() -> Path:
    p = get_settings().data_root() / "nextbid_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _key(kind: str, *parts: str) -> str:
    raw = "|".join([kind] + [str(p) for p in parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(kind: str, *parts: str) -> dict[str, Any] | None:
    path = cache_root() / kind / f"{_key(kind, *parts)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def cache_put(kind: str, payload: dict[str, Any], *parts: str) -> Path:
    d = cache_root() / kind
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_key(kind, *parts)}.json"
    wrapper = {
        "cached_at": _utc(),
        "kind": kind,
        "parts": list(parts),
        "payload": payload,
    }
    if path.exists():
        # A paid response is evidence: a different payload for the same request keeps the earlier one
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            prev = None
        if prev is not None and prev.get("payload") != payload:
            stamp = str(prev.get("cached_at") or _utc()).replace(":", "").replace("+", "_")
            (d / "versions").mkdir(exist_ok=True)
            path.replace(d / "versions" / f"{path.stem}.{stamp}.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wrapper, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def cache_stats() -> dict[str, Any]:
    root = cache_root()
    by_kind: dict[str, int] = {}
    total = 0
    for kind_dir in root.iterdir() if root.exists() else []:
        if not kind_dir.is_dir():
            continue
        n = sum(1 for p in kind_dir.glob("*.json"))
        by_kind[kind_dir.name] = n
        total += n
    return {"total": total, "by_kind": by_kind, "root": str(root)}
