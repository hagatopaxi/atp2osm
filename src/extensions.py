"""The Flask-Caching and Flask-Limiter instances, and their decorators with
their types spelled out.

Flask-Caching types its decorators as `(...) -> Unknown`, which would erase
the signature of every function they wrap. These keep it.
"""

from collections.abc import Callable
from typing import ParamSpec, TypeVar, cast

from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

cache = Cache()
# Per client address, in each worker's memory: a bound on what one client
# costs, not an exact quota. The address is the proxy's X-Forwarded-For,
# which ProxyFix trusts (src/app.py).
limiter = Limiter(get_remote_address, storage_uri="memory://")

P = ParamSpec("P")
R = TypeVar("R")
Decorator = Callable[[Callable[P, R]], Callable[P, R]]


def remember(key: str, value: object, timeout: int) -> None:
    cache.set(key, value, timeout=timeout)  # pyright: ignore[reportUnknownMemberType]


def recall(key: str) -> object:
    """What `remember` kept under `key`, or None once it is gone."""
    return cache.get(key)  # pyright: ignore[reportUnknownMemberType]


def cached(timeout: int, key_prefix: str, *, query_string: bool = False) -> Decorator[P, R]:
    """`cache.cached`, keyed on the request."""
    return cast(
        "Decorator[P, R]",
        cache.cached(timeout=timeout, key_prefix=key_prefix, query_string=query_string),  # pyright: ignore[reportUnknownMemberType]
    )


def limit(rate: str) -> Decorator[P, R]:
    """`limiter.limit`, with the wrapped signature kept."""
    return cast("Decorator[P, R]", limiter.limit(rate))
