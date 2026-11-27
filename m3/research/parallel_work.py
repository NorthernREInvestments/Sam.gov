"""Bounded parallel helpers — speed without dropping work items.

Rules:
- Every input item is processed exactly once (no silent skips).
- Failures fail closed: first exception cancels outstanding work and raises.
- Default worker cap 10 for OpenAI/IO; NextBid metered HTTP remains gated by
  controller MAX_CONCURRENCY.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_OPENAI_WORKERS = 10
DEFAULT_IO_WORKERS = 10


def map_parallel(
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int = DEFAULT_OPENAI_WORKERS,
    ordered: bool = True,
) -> list[R]:
    """Run fn over all items with up to max_workers threads.

    If ordered=True, return results in the same order as inputs.
    Raises the first exception; does not return partial success quietly.
    """
    seq = list(items)
    if not seq:
        return []
    workers = max(1, min(int(max_workers), len(seq)))
    if workers == 1 or len(seq) == 1:
        return [fn(x) for x in seq]

    results: list[R | None] = [None] * len(seq)
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(seq)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except BaseException as exc:  # noqa: BLE001 — fail closed
                errors.append(exc)
                for other in futures:
                    other.cancel()
                break
    if errors:
        raise errors[0]
    if any(r is None for r in results):
        raise RuntimeError("parallel_map_incomplete_results")
    return results  # type: ignore[return-value]


def map_parallel_keyed(
    fn: Callable[[T], R],
    items: dict[Any, T],
    *,
    max_workers: int = DEFAULT_OPENAI_WORKERS,
) -> dict[Any, R]:
    keys = list(items.keys())
    vals = [items[k] for k in keys]
    out = map_parallel(fn, vals, max_workers=max_workers, ordered=True)
    return {k: out[i] for i, k in enumerate(keys)}
