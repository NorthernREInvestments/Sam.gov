"""Temporary retrieval lifecycle — DISCOVERED → TEMP_RETRIEVED → ANALYZED → KNOWLEDGE_EXTRACTED → RELEASED."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from application_clock import now_utc
from persistence_policy import (
    KIND_BULK_HTML,
    KIND_BULK_PDF,
    KIND_CONTENT_HASH,
    KIND_URL_REFERENCE,
    TEMPORARY,
    persistence_decision,
)
from procurement_source_knowledge import preserve_signed_query_string

# Lifecycle
LIFE_DISCOVERED = "DISCOVERED"
LIFE_TEMP_RETRIEVED = "TEMP_RETRIEVED"
LIFE_ANALYZED = "ANALYZED"
LIFE_KNOWLEDGE_EXTRACTED = "KNOWLEDGE_EXTRACTED"
LIFE_RELEASED = "RELEASED"
LIFE_RETAINED = "RETAINED"

DEFAULT_TTL_SECONDS = 3600


class TemporaryRetrievalStore:
    """Run-scoped temp workspace. Bulk bodies default TEMPORARY."""

    def __init__(self, root: Path, *, run_id: str | None = None) -> None:
        self.run_id = run_id or now_utc().strftime("%Y%m%dT%H%M%S")
        self.root = root / "on_demand_temp" / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, dict[str, Any]] = {}
        self._url_cache: dict[str, str] = {}  # url -> item_id (dedupe)
        self.stats = {
            "requests_attempted": 0,
            "requests_reused": 0,
            "requests_avoided": 0,
            "requests_failed": 0,
            "auth_blocked": 0,
            "documents_temporarily_retrieved": 0,
            "documents_permanently_retained": 0,
            "documents_released": 0,
            "bytes_temp": 0,
        }

    def _item_id(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    def discover(self, url: str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        url = preserve_signed_query_string(url)
        iid = self._item_id(url)
        if iid in self._items:
            return self._items[iid]
        rec = {
            "item_id": iid,
            "url": url,
            "lifecycle": LIFE_DISCOVERED,
            "meta": meta or {},
            "path": None,
            "sha256": None,
            "discovered_at": now_utc().isoformat(),
            "persistence": persistence_decision(KIND_URL_REFERENCE),
            "operator_retain": False,
        }
        self._items[iid] = rec
        return rec

    def retrieve(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_bytes: int = 15 * 1024 * 1024,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = preserve_signed_query_string(url)
        # Deduplicate within run
        if url in self._url_cache:
            self.stats["requests_reused"] += 1
            self.stats["requests_avoided"] += 1
            return self._items[self._url_cache[url]]

        rec = self.discover(url, meta=meta)
        self.stats["requests_attempted"] += 1
        owns = client is None
        http = client or httpx.Client(timeout=timeout, follow_redirects=True)
        try:
            r = http.get(url)
            if r.status_code in {401, 403}:
                self.stats["auth_blocked"] += 1
                self.stats["requests_failed"] += 1
                rec["lifecycle"] = LIFE_DISCOVERED
                rec["access"] = "AUTH_REQUIRED" if r.status_code == 401 else "ACCESS_DENIED"
                rec["http_status"] = r.status_code
                return rec
            if r.status_code >= 400:
                self.stats["requests_failed"] += 1
                rec["http_status"] = r.status_code
                rec["access"] = "SOURCE_ERROR"
                return rec
            data = r.content[:max_bytes]
            digest = hashlib.sha256(data).hexdigest()
            ext = ".pdf" if "pdf" in (r.headers.get("content-type") or "").lower() or url.lower().split("?")[0].endswith(".pdf") else ".bin"
            path = self.root / f"{rec['item_id']}{ext}"
            path.write_bytes(data)
            kind = KIND_BULK_PDF if ext == ".pdf" else KIND_BULK_HTML
            rec.update(
                {
                    "lifecycle": LIFE_TEMP_RETRIEVED,
                    "path": str(path),
                    "sha256": digest,
                    "bytes": len(data),
                    "http_status": r.status_code,
                    "access": "PUBLIC",
                    "retrieved_at": now_utc().isoformat(),
                    "expires_at": time.time() + ttl_seconds,
                    "persistence": persistence_decision(kind),
                    "content_type": r.headers.get("content-type"),
                }
            )
            # Always persist hash+URL reference knowledge, not necessarily body
            rec["reference_persistence"] = persistence_decision(KIND_CONTENT_HASH)
            self._url_cache[url] = rec["item_id"]
            self.stats["documents_temporarily_retrieved"] += 1
            self.stats["bytes_temp"] += len(data)
            return rec
        except Exception as exc:  # noqa: BLE001
            self.stats["requests_failed"] += 1
            rec["error"] = str(exc)[:200]
            rec["access"] = "SOURCE_ERROR"
            return rec
        finally:
            if owns:
                http.close()

    def mark_analyzed(self, item_id: str) -> None:
        if item_id in self._items:
            self._items[item_id]["lifecycle"] = LIFE_ANALYZED

    def mark_knowledge_extracted(self, item_id: str, knowledge: dict[str, Any]) -> None:
        if item_id in self._items:
            self._items[item_id]["lifecycle"] = LIFE_KNOWLEDGE_EXTRACTED
            self._items[item_id]["extracted_knowledge"] = knowledge

    def mark_operator_retain(self, item_id: str) -> None:
        if item_id in self._items:
            self._items[item_id]["operator_retain"] = True
            self._items[item_id]["lifecycle"] = LIFE_RETAINED
            self.stats["documents_permanently_retained"] += 1

    def cleanup(self, *, force: bool = False) -> dict[str, Any]:
        """Release temporary bulk artifacts unless operator-retained / business record."""
        released = []
        retained = []
        for iid, rec in list(self._items.items()):
            if rec.get("operator_retain") or rec.get("lifecycle") == LIFE_RETAINED:
                retained.append(iid)
                continue
            # Supplier quotes / explicit retain
            if rec.get("meta", {}).get("is_supplier_quote"):
                retained.append(iid)
                self.stats["documents_permanently_retained"] += 1
                continue
            path = rec.get("path")
            if path and Path(path).exists():
                Path(path).unlink(missing_ok=True)
            rec["lifecycle"] = LIFE_RELEASED
            rec["path"] = None
            # Keep url + hash for provenance
            released.append({"item_id": iid, "url": rec.get("url"), "sha256": rec.get("sha256")})
            self.stats["documents_released"] += 1

        # Remove empty run dir if nothing retained on disk
        if force or not any(self.root.iterdir()) if self.root.exists() else True:
            try:
                if self.root.exists() and not any(self.root.iterdir()):
                    self.root.rmdir()
            except OSError:
                pass

        return {
            "kind": "TemporaryRetrievalCleanupReport",
            "run_id": self.run_id,
            "released": released,
            "retained_ids": retained,
            "stats": dict(self.stats),
            "note": "Bulk bodies released; URL/hash provenance may remain",
        }

    def export_manifest(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "items": [
                {
                    "item_id": r["item_id"],
                    "url": r.get("url"),
                    "lifecycle": r.get("lifecycle"),
                    "sha256": r.get("sha256"),
                    "bytes": r.get("bytes"),
                    "operator_retain": r.get("operator_retain"),
                    "has_body_on_disk": bool(r.get("path") and Path(r["path"]).exists()),
                }
                for r in self._items.values()
            ],
            "stats": dict(self.stats),
        }
