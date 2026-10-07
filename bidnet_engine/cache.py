"""Cache metrics for avoided expensive work."""

from __future__ import annotations

from typing import Any


class CacheMetrics:
    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.detail_fetches_avoided = 0
        self.document_parses_avoided = 0
        self.browser_renders_avoided = 0
        self.ai_calls_avoided = 0
        self.ai_calls = 0

    def hit(self, *, kind: str = "generic") -> None:
        self.hits += 1
        if kind == "detail":
            self.detail_fetches_avoided += 1
            self.browser_renders_avoided += 1
        elif kind == "document":
            self.document_parses_avoided += 1
        elif kind == "ai":
            self.ai_calls_avoided += 1

    def miss(self) -> None:
        self.misses += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "detail_fetches_avoided": self.detail_fetches_avoided,
            "document_parses_avoided": self.document_parses_avoided,
            "browser_renders_avoided": self.browser_renders_avoided,
            "ai_calls_avoided": self.ai_calls_avoided,
            "ai_calls": self.ai_calls,
        }
