"""diskcache wrapper. SQLite-backed key-value cache with TTL."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from diskcache import Cache

from .config import ensure_home


_TTL_SEARCH = 60 * 60 * 24  # 24h
_TTL_PRODUCT = 60 * 60  # 1h


@contextmanager
def cache() -> Iterator[Cache]:
    p = ensure_home()
    c = Cache(str(p.cache_db))
    try:
        yield c
    finally:
        c.close()


def get(key: str) -> Optional[Any]:
    with cache() as c:
        return c.get(key)


def set_(key: str, value: Any, ttl: int) -> None:
    with cache() as c:
        c.set(key, value, expire=ttl)


def search_key(query: str, limit: int) -> str:
    return f"search:{query.lower().strip()}:{limit}"


def product_key(product_id: str) -> str:
    return f"product:{product_id}"


__all__ = [
    "cache",
    "get",
    "set_",
    "search_key",
    "product_key",
    "_TTL_SEARCH",
    "_TTL_PRODUCT",
]
