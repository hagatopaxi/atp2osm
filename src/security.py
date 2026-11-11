"""What every response carries for the browser's protection: cookie flags,
security headers, and the per-client rate limits of the costly routes.
"""

from datetime import timedelta
from typing import Final
from urllib.parse import urlsplit

from flask import Flask, Response

from src.config import Settings
from src.extensions import limiter

SESSION_LIFETIME: Final = timedelta(days=30)

# No inline script anywhere: handlers are declared with data-on-* attributes
# (static/js/actions.js). Styles stay inline-allowed: the Tailwind runtime
# writes the stylesheet it compiles into a <style> element.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

HEADERS: Final = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def init_app(app: Flask, settings: Settings) -> None:
    # The session carries the sealed OSM token: HTTPS only in production
    # (development serves plain HTTP on localhost), never read by a script,
    # never sent along a cross-site POST.
    app.config["SESSION_COOKIE_SECURE"] = not settings.is_dev
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = SESSION_LIFETIME
    if not settings.is_dev:
        # ProxyFix takes the host from X-Forwarded-Host, and the canonical,
        # hreflang and structured-data URLs are built from it: only the public
        # host, and the loopback a probe on the machine uses, are answered.
        app.config["TRUSTED_HOSTS"] = [
            urlsplit(settings.app_base_url).hostname,
            "localhost",
            "127.0.0.1",
        ]
    limiter.init_app(app)

    @app.after_request
    def security_headers(response: Response) -> Response:
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        if not settings.is_dev:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response
