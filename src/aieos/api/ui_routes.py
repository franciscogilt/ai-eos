"""Web dashboard: a static single-page app served under /ui.

No build step, no third-party scripts or styles, and a strict Content-Security-Policy: scripts and styles
only from this origin, network calls only to this API and the identity provider, no framing. The app
renders all data with textContent (never as HTML). Sign-in is OIDC authorization code with PKCE in the
browser (a public client; nothing secret is shipped to the browser); the access token lives in
sessionStorage only, so it is gone when the tab closes.
"""

from __future__ import annotations

from importlib import resources
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from aieos.core.settings import Settings

router = APIRouter(include_in_schema=False)

_ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else None


def content_security_policy(settings: Settings) -> str:
    origins = {o for o in (_origin(settings.oidc_issuer),) if o}
    origins |= {o for raw in settings.oidc_ui_extra_origins.split(",") if (o := _origin(raw.strip()))}
    connect = " ".join(["'self'", *sorted(origins)])
    return (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src {connect}; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )


def _asset(name: str, settings: Settings) -> Response:
    body = resources.files("aieos.api").joinpath("ui", name).read_bytes()
    return Response(
        body,
        media_type=_ASSETS[name],
        headers={
            "Content-Security-Policy": content_security_policy(settings),
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/ui")
async def ui_root() -> RedirectResponse:
    return RedirectResponse("/ui/", status_code=307)


@router.get("/ui/")
async def ui_index(request: Request) -> Response:
    return _asset("index.html", request.app.state.settings)


@router.get("/ui/{name}")
async def ui_file(name: str, request: Request) -> Response:
    if name == "config.json":
        s: Settings = request.app.state.settings
        return JSONResponse(
            {
                "issuer": s.oidc_issuer,
                "client_id": s.oidc_ui_client_id,
                "scopes": s.oidc_ui_scopes,
                "audience": s.oidc_ui_audience,
                "token_paste": s.environment in ("local", "test") or s.oidc_ui_client_id is None,
            },
            headers={"Cache-Control": "no-store"},
        )
    if name not in _ASSETS:
        return Response(status_code=404)
    return _asset(name, request.app.state.settings)
