import logging
from io import BytesIO

from flask import Blueprint, Response, abort, render_template, request, send_from_directory, url_for
from flask.typing import ResponseReturnValue
from psycopg.rows import dict_row
from staticmap import CircleMarker, StaticMap

from src.config import STATIC_DIR, get_settings
from src.db import get_osmdb
from src.extensions import cached
from src.routes.spiders import SPIDERS_PAGE_LINKED

logger = logging.getLogger(__name__)

misc_bp = Blueprint("misc", __name__)

# Public pages (outside the OSM OAuth authenticated area), in menu order —
# the single source for sitemap.xml and llms.txt. Their labels live in
# llms.txt, the only one of the two that writes them out.
PUBLIC_PAGES = (
    "misc.home",
    "brands.brands",
    *(("spiders.spiders",) if SPIDERS_PAGE_LINKED else ()),
    "history.history",
    "stats.stats",
    "todo.todo",
    "misc.docs",
    "misc.about",
)


@misc_bp.route("/")
def home() -> str:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        stats = cursor.execute("""
            SELECT
                COALESCE(SUM(items_count), 0) AS total_nodes_updated,
                COUNT(DISTINCT brand_wikidata) FILTER (WHERE status = 'success') AS brands_imported,
                -- A contributor is anyone who moved the project forward:
                -- integrating POIs or reporting a brand missing from ATP.
                (SELECT COUNT(*) FROM (
                    SELECT osm_user_id FROM import_history
                    UNION
                    SELECT osm_user_id FROM todo_brands
                ) u) AS contributors,
                -- Tags per wave: added by wave 1, modified by wave 2.
                COALESCE((SELECT SUM(v.value::int)
                          FROM import_history h,
                               LATERAL jsonb_each_text(COALESCE(h.tags_count, '{}'::jsonb)) v
                          WHERE h.wave = 1), 0) AS tags_added,
                COALESCE((SELECT SUM(v.value::int)
                          FROM import_history h,
                               LATERAL jsonb_each_text(COALESCE(h.tags_count, '{}'::jsonb)) v
                          WHERE h.wave = 2), 0) AS tags_modified
            FROM import_history
        """).fetchone()
        data_imports = cursor.execute("""
            SELECT DISTINCT ON (type) type, date, status, created_at
            FROM data_imports
            ORDER BY type, created_at DESC
        """).fetchall()
    by_type = {str(row["type"]): row for row in data_imports}
    return render_template("home.html", stats=stats, data_imports=by_type)


@misc_bp.route("/docs")
def docs() -> str:
    return render_template("docs.html")


@misc_bp.route("/about")
def about() -> str:
    return render_template("about.html")


@misc_bp.route("/favicon.ico")
def favicon() -> ResponseReturnValue:
    return send_from_directory(STATIC_DIR, "img/logo.svg", mimetype="image/svg+xml")


@misc_bp.route("/google1387dd4d6e23b123.html")
def google_site_verification() -> ResponseReturnValue:
    return send_from_directory(STATIC_DIR, "google1387dd4d6e23b123.html", mimetype="text/html")


@misc_bp.route("/robots.txt")
def robots() -> ResponseReturnValue:
    body = render_template("robots.txt", sitemap_url=url_for("misc.sitemap", _external=True))
    return Response(body, mimetype="text/plain")


@misc_bp.route("/health")
def health() -> ResponseReturnValue:
    # A health check that does not reach the database reports a dead site as
    # alive; a failure here is a 500, which is the answer a probe wants.
    get_osmdb().execute("SELECT 1")
    return {"status": "ok"}


@misc_bp.route("/version")
def version() -> ResponseReturnValue:
    return {"version": get_settings().app_version}


@misc_bp.route("/sitemap.xml")
def sitemap() -> ResponseReturnValue:
    body = render_template("sitemap.xml", pages=PUBLIC_PAGES, root=request.host_url.rstrip("/"))
    return Response(body, mimetype="application/xml")


@misc_bp.route("/llms.txt")
def llms_txt() -> ResponseReturnValue:
    body = render_template("llms.txt", pages=PUBLIC_PAGES)
    return Response(body, mimetype="text/plain")


@misc_bp.route("/staticmap/<long>/<lat>")
@cached(timeout=300, key_prefix="staticmap/", query_string=True)
def staticmap(long: str, lat: str) -> ResponseReturnValue:
    # Anything but a point on Earth is no map: 404, before a tile is asked.
    try:
        point = (float(long), float(lat))
    except ValueError:
        abort(404)
    return _render_map(*point)


MAX_LONGITUDE = 180
MAX_LATITUDE = 90


def _render_map(long: float, lat: float) -> Response:
    if not (-MAX_LONGITUDE <= long <= MAX_LONGITUDE and -MAX_LATITUDE <= lat <= MAX_LATITUDE):
        abort(404)
    m = StaticMap(400, 300, url_template="http://b.tile.osm.org/{z}/{x}/{y}.png")
    m.add_marker(CircleMarker((long, lat), "white", 18))
    m.add_marker(CircleMarker((long, lat), "#0036FF", 12))
    try:
        image = m.render(zoom=17)
    except Exception:
        # The tile server, not us. Raised rather than returned, so the
        # cache above keeps nothing and the next request asks again.
        logger.warning("Tile server unreachable for %s/%s", long, lat, exc_info=True)
        abort(502)

    # In memory image returned directly to the client
    img_io = BytesIO()
    image.save(img_io, "PNG")
    img_io.seek(0)
    return Response(img_io, mimetype="image/png")
