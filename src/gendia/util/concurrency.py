"""Bounded thread-pool helpers.

Operations that fan out per-repo (sync, audit, status, cleanup) run via
`map_repos` which guarantees exception propagation, ordered results, and a
configurable concurrency cap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor


def map_repos[T, R](
    fn: Callable[[T], R],
    items: Iterable[T],
    *,
    workers: int,
) -> Iterator[tuple[T, R | Exception]]:
    """Run `fn(item)` across `items` with at most `workers` in flight.

    Yields `(item, result)` pairs in completion order. Exceptions are caught
    and yielded inline so the caller can decide per-item how to react.
    """
    workers = max(1, workers)
    items_list = list(items)
    if not items_list:
        return

    if workers == 1:
        for item in items_list:
            try:
                yield item, fn(item)
            except Exception as exc:  # noqa: BLE001 — surface to caller
                yield item, exc
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gendia") as pool:
        future_to_item = {pool.submit(fn, item): item for item in items_list}
        for fut, item in future_to_item.items():
            try:
                yield item, fut.result()
            except Exception as exc:  # noqa: BLE001 — surface to caller
                yield item, exc
