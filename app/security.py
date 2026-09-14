"""Protect the loopback API from requests initiated by other websites."""
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse

CSRF_TOKEN = secrets.token_urlsafe(32)
_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def protect_local_api(request: Request, call_next):
    if request.url.hostname not in _HOSTS:
        return PlainTextResponse("Invalid host", status_code=400)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return PlainTextResponse("Cross-site request rejected", status_code=403)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
            return PlainTextResponse("Invalid origin", status_code=403)
    if request.method not in _SAFE_METHODS:
        token = request.headers.get("x-gamepile-token", "")
        # Native setup/settings forms carry a hidden field. Reading body()
        # first lets Starlette replay the body to the actual route handler.
        if not token and request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            body = await request.body()
            if len(body) <= 65536:
                token = (await request.form()).get("csrf_token", "")
        if not isinstance(token, str) or not secrets.compare_digest(token, CSRF_TOKEN):
            return PlainTextResponse("Session expired. Reload GamePile and try again.", status_code=403)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response
