import logging
import json
import psycopg

from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from src.config import TEMPLATE_DIR, STATIC_DIR, CACHE_DIR, get_settings
from src.db import teardown_osmdb
from src.extensions import cache
from src import i18n
from src.migrate import run_migrations
from src.phone import ensure_normalize_phone
from src.routes.auth import auth_bp
from src.routes.brands import brands_bp
from src.error_reasons import ERROR_REASONS
from src.routes.export import export_bp
from src.routes.history import history_bp
from src.routes.misc import misc_bp
from src.routes.stats import stats_bp
from src.routes.todo import todo_bp

logger = logging.getLogger(__name__)

settings = get_settings()  # fail fast at startup if any required env var is missing

# The pages that exist in every language. Everything else — assets, the API,
# the OAuth callback, robots.txt — stays language-free.
TRANSLATED_PATHS = ("/", "/brands", "/history", "/stats", "/todo", "/docs")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = settings.secret_key

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0 if settings.is_dev else 31536000  # dev: always revalidate — prod: cache for a year
app.config["CACHE_TYPE"] = "FileSystemCache"
app.config["CACHE_DIR"] = CACHE_DIR
app.config["CACHE_THRESHOLD"] = 1000
app.config["CACHE_DEFAULT_TIMEOUT"] = 0  # Infinite cache duration

cache.init_app(app)
i18n.init_app(
    app, settings.country.locales, TRANSLATED_PATHS, settings.country.timezone
)

app.register_blueprint(auth_bp)
app.register_blueprint(brands_bp)
app.register_blueprint(export_bp)
app.register_blueprint(history_bp)
app.register_blueprint(misc_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(todo_bp)

app.teardown_appcontext(teardown_osmdb)


@app.template_filter("parse_comment")
def parse_comment(value):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def run_startup_tasks():
    """Run migrations at server startup."""
    try:
        with psycopg.connect(**settings.db.connect_kwargs) as conn:
            run_migrations(conn)
            # Generated from the country, not migrated into the schema: a new
            # country costs a configuration file, never a migration.
            ensure_normalize_phone(conn)
    except Exception:
        logger.exception("Startup tasks failed.")
        raise


run_startup_tasks()


@app.context_processor
def inject_globals():
    return {
        "api_url": settings.api_url,
        "app_version": settings.app_version,
        "is_dev": settings.is_dev,
        "country_code": settings.country.code.upper(),
        "source_repo_url": settings.source_repo_url,
        "error_reasons": ERROR_REASONS,
    }


@app.errorhandler(500)
def internal_error(error):
    return render_template("errors/500.html"), 500


@app.errorhandler(403)
def not_authorized_error(error):
    return render_template("errors/403.html"), 403


@app.errorhandler(404)
def not_found_error(error):
    return render_template("errors/404.html"), 404


if __name__ == "__main__":
    app.run(port=settings.port)
