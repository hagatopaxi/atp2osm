import logging

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from src import i18n, migrate, templating
from src.config import CACHE_DIR, STATIC_DIR, TEMPLATE_DIR, get_settings
from src.db import teardown_osmdb
from src.extensions import cache
from src.routes.auth import auth_bp
from src.routes.brands import brands_bp
from src.routes.export import export_bp
from src.routes.history import history_bp
from src.routes.misc import misc_bp
from src.routes.spiders import spiders_bp
from src.routes.stats import stats_bp
from src.routes.todo import todo_bp

logger = logging.getLogger(__name__)

settings = get_settings()  # fail fast at startup if any required env var is missing

# The pages that exist in every language. Everything else — assets, the API,
# the OAuth callback, robots.txt — stays language-free.
TRANSLATED_PATHS = ("/", "/brands", "/spiders", "/history", "/stats", "/todo", "/docs", "/about")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = settings.secret_key

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = (
    0 if settings.is_dev else 31536000
)  # dev: always revalidate — prod: cache for a year
app.config["CACHE_TYPE"] = "FileSystemCache"
app.config["CACHE_DIR"] = CACHE_DIR
app.config["CACHE_THRESHOLD"] = 1000
app.config["CACHE_DEFAULT_TIMEOUT"] = 0  # Infinite cache duration

cache.init_app(app)  # pyright: ignore[reportUnknownMemberType] — Flask-Caching types `config` loosely
i18n.init_app(
    app,
    settings.country.locales,
    TRANSLATED_PATHS,
    settings.country.timezone,
    settings.translations_dir,
)

app.register_blueprint(auth_bp)
app.register_blueprint(brands_bp)
app.register_blueprint(spiders_bp)
app.register_blueprint(export_bp)
app.register_blueprint(history_bp)
app.register_blueprint(misc_bp)
app.register_blueprint(stats_bp)
app.register_blueprint(todo_bp)

app.teardown_appcontext(teardown_osmdb)
templating.init_app(app, settings)


# Production migrates once per deploy, before the container restarts (see
# deploy/run): gunicorn workers never touch the schema. Development has no
# deploy step, so the server does it at boot.
if settings.is_dev:
    migrate.main()


if __name__ == "__main__":
    app.run(port=settings.port)
