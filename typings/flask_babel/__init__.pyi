# Stubs for the part of Flask-Babel the site uses. The package ships no
# typing; what is not listed here is not called.
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from babel.core import Locale
from flask import Flask

def gettext(string: str, **variables: Any) -> str: ...
def ngettext(singular: str, plural: str, num: int, **variables: Any) -> str: ...
def format_date(
    date: date | None = None, format: str | None = None, rebase: bool = True
) -> str: ...
def format_datetime(
    datetime: datetime | None = None, format: str | None = None, rebase: bool = True
) -> str: ...
def get_locale() -> Locale | None: ...

class Babel:
    def __init__(self, app: Flask | None = None) -> None: ...
    def init_app(
        self,
        app: Flask,
        default_locale: str = "en",
        default_domain: str = "messages",
        default_translation_directories: str = "translations",
        default_timezone: str = "UTC",
        locale_selector: Callable[[], str | None] | None = None,
        timezone_selector: Callable[[], str | None] | None = None,
    ) -> None: ...
