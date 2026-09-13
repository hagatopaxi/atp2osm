"""What every template can read — the filters and globals of the site — and
the pages an error renders.

Wired here rather than in `src/app.py` so that a test can build an app on the
real templates — importing `src.app` runs the migrations against the
development database, which a test must never touch.
"""

import json
import logging

from flask import render_template
from flask_babel import gettext as _
from psycopg.errors import UndefinedTable

from src.error_reasons import ERROR_REASONS
from src.matching import WAVES_BY_NUMBER
from src.routes.spiders import SPIDERS_PAGE_LINKED


def parse_comment(value):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


logger = logging.getLogger(__name__)


def init_app(app, settings):
    app.add_template_filter(parse_comment, "parse_comment")

    @app.errorhandler(403)
    def not_authorized(error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(error):
        return render_template("errors/500.html"), 500

    @app.errorhandler(502)
    def bad_gateway(error):
        return render_template("errors/500.html"), 502

    @app.errorhandler(UndefinedTable)
    def data_not_ready(error):
        """A table the pipeline builds is not there yet: the instance is new
        and its first refresh has not run. Not an error of ours to crash on."""
        logger.warning("Data not ready: %s", error)
        return render_template(
            "errors/503.html",
            message=_("The data is being prepared, this instance has not run its first refresh yet."),
        ), 503

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
