"""OpenGovPublicDiscoveryClient — public project-list harvest without auth browser."""

from __future__ import annotations

from typing import Any

import httpx

from opengov_discovery.public_harvest import (
    UA,
    candidate_public_urls,
    harvest_portal_public,
    run_opengov_public_discovery,
)


class OpenGovPublicDiscoveryClient:
    """Fetch public OpenGov opportunity lists/details; avoid auth unless needed."""

    def __init__(self, *, timeout: float = 35.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"},
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "OpenGovPublicDiscoveryClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def public_list_candidates(self, portal_url: str) -> list[str]:
        return candidate_public_urls(portal_url)

    def harvest_entity(
        self,
        portal: dict[str, Any],
        *,
        max_pages: int = 8,
    ) -> dict[str, Any]:
        return harvest_portal_public(portal, client=self._client, max_pages=max_pages)

    def harvest_registry(
        self,
        *,
        max_entities: int | None = 50,
        max_pages: int = 6,
        persist: bool = True,
        run_id: str | None = None,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        return run_opengov_public_discovery(
            max_entities=max_entities,
            max_pages=max_pages,
            persist=persist,
            run_id=run_id,
            on_progress=on_progress,
        )
