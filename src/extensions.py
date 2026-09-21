"""The Flask-Caching instance, and its decorators with their types spelled out.

Flask-Caching types its decorators as `(...) -> Unknown`, which would erase
the signature of every function they wrap. These keep it.
"""

from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, cast

from flask_caching import Cache

cache = Cache()

P = ParamSpec("P")
R = TypeVar("R")
Decorator = Callable[[Callable[P, R]], Callable[P, R]]


def memoize(timeout: int) -> Decorator[P, R]:
    """`cache.memoize`, keyed on the function and its arguments."""
    return cast("Decorator[P, R]", cache.memoize(timeout=timeout))  # pyright: ignore[reportUnknownMemberType]


def delete_memoized(f: Callable[..., Any], *args: Any) -> None:  # noqa: ANN401 — the memoized call's own arguments
    cache.delete_memoized(f, *args)  # pyright: ignore[reportUnknownMemberType]


def cached(timeout: int, key_prefix: str, *, query_string: bool = False) -> Decorator[P, R]:
    """`cache.cached`, keyed on the request."""
    return cast(
        "Decorator[P, R]",
        cache.cached(timeout=timeout, key_prefix=key_prefix, query_string=query_string),  # pyright: ignore[reportUnknownMemberType]
    )
