"""The Flask-Caching instance, and its decorators with their types spelled out.

Flask-Caching types its decorators as `(...) -> Unknown`, which would erase
the signature of every function they wrap. These keep it.
"""

from collections.abc import Callable
from typing import ParamSpec, TypeVar, cast

from flask_caching import Cache

cache = Cache()

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
