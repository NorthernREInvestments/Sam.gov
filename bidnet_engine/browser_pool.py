"""Bounded reusable BidNet authenticated browser pool.

Logical workers may exceed browser workers; only browser_workers Chromium
clients exist, reused across opportunities.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from bidnet_engine.thread_limits import apply_thread_limits

apply_thread_limits(n=1)


class BrowserPool:
    def __init__(self, size: int | None = None) -> None:
        max_proc = int(os.environ.get("BIDNET_MAX_BROWSER_PROCESSES") or 2)
        configured = int(os.environ.get("BIDNET_BROWSER_WORKERS") or size or 2)
        self.size = max(1, min(configured, max_proc, int(size or configured)))
        self._sem = threading.Semaphore(self.size)
        self._lock = threading.Lock()
        self._idle: list[Any] = []
        self._created = 0
        self._checkouts = 0
        self._auth_failures = 0

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_browser_concurrency": self.size,
                "chromium_clients_created": self._created,
                "idle_clients": len(self._idle),
                "checkouts": self._checkouts,
                "auth_failures": self._auth_failures,
                "approx_active": max(0, self._created - len(self._idle)),
            }

    def _new_client(self) -> Any:
        from bidnet_auth.client import BidNetAuthenticatedClient

        client = BidNetAuthenticatedClient()
        auth = client.ensure_authenticated()
        if not auth.authenticated:
            client.close()
            self._auth_failures += 1
            raise RuntimeError(f"browser pool auth failed: {auth.status} {auth.message}")
        self._created += 1
        return client

    @contextmanager
    def client(self) -> Iterator[Any]:
        self._sem.acquire()
        held = None
        try:
            with self._lock:
                if self._idle:
                    held = self._idle.pop()
                else:
                    held = None
            if held is None:
                held = self._new_client()
            elif not held.is_authenticated:
                try:
                    held.close()
                except Exception:
                    pass
                held = self._new_client()
            self._checkouts += 1
            yield held
        finally:
            if held is not None:
                with self._lock:
                    if len(self._idle) < self.size:
                        self._idle.append(held)
                    else:
                        try:
                            held.close()
                        except Exception:
                            pass
                        self._created = max(0, self._created - 1)
            self._sem.release()

    def close(self) -> None:
        with self._lock:
            while self._idle:
                c = self._idle.pop()
                try:
                    c.close()
                except Exception:
                    pass
            self._created = 0
