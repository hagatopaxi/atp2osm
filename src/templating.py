"""What every template can read: the filters and globals of the site.

Wired here rather than in `src/app.py` so that a test can build an app on the
real templates — importing `src.app` runs the migrations against the
development database, which a test must never touch.
"""

import json

from src.error_reasons import ERROR_REASONS
from src.matching import WAVES_BY_NUMBER
from src.routes.spiders import SPIDERS_PAGE_LINKED


def parse_comment(value):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def init_app(app, settings):
    app.add_template_filter(parse_comment, "parse_comment")

    @app.context_processor
    def inject_globals():
        return {
            "api_url": settings.api_url,
            "app_version": settings.app_version,
            "is_dev": settings.is_dev,
            "country_code": settings.country.code.upper(),
            "source_repo_url": settings.source_repo_url,
            "error_reasons": ERROR_REASONS,
            # The waves themselves — numbers and batch sizes are data; their
            # labels are in _wave.html, where a locale exists to resolve them.
            "waves": WAVES_BY_NUMBER,
            "spiders_page_linked": SPIDERS_PAGE_LINKED,
        }
