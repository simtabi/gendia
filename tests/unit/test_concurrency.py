"""ThreadPool helper: ordering + exception capture."""

from __future__ import annotations

from gendia.util.concurrency import map_repos


def test_map_returns_pairs() -> None:
    pairs = list(map_repos(lambda x: x * 2, [1, 2, 3], workers=2))
    items, results = zip(*pairs, strict=True)
    assert sorted(items) == [1, 2, 3]
    assert sorted(results) == [2, 4, 6]


def test_map_captures_exceptions() -> None:
    def f(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x

    seen = list(map_repos(f, [1, 2, 3], workers=1))
    by_item = dict(seen)
    assert by_item[1] == 1
    assert isinstance(by_item[2], ValueError)
    assert by_item[3] == 3


def test_map_empty_input() -> None:
    assert list(map_repos(lambda x: x, [], workers=4)) == []
