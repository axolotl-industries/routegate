"""FastAPI application: list, create, edit, delete routes via HTMX.

Templates render either as full pages (initial load) or as panel partials
(HTMX swaps). The HX-Request header decides which.

A single CloudflareClient lives on app.state for the process lifetime,
managed by the lifespan context.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import caddy, cloudflare, config, reload, routes, services, tunnel

TEMPLATE_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = config.load()
    app.state.settings = settings
    app.state.cf = cloudflare.CloudflareClient(
        settings.cloudflare_api_token, settings.cloudflare_zone_id
    )
    try:
        yield
    finally:
        await app.state.cf.aclose()


app = FastAPI(title="routegate", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ----------------------------------------------------------------------------
# render helpers


def _is_hx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _panel(
    request: Request,
    *,
    routes_views: list[routes.RouteView],
    settings: config.Settings,
    live: bool,
    flash: dict | None = None,
    status_code: int = 200,
):
    template = "_panel.html" if _is_hx(request) else "list.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "routes": routes_views,
            "settings": settings,
            "live": live,
            "flash": flash,
        },
        status_code=status_code,
    )


def _form_panel(
    request: Request,
    *,
    mode: str,
    form: dict,
    errors: dict | None = None,
    flash: dict | None = None,
    status_code: int = 200,
):
    template = "_form.html" if _is_hx(request) else "form.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "mode": mode,
            "form": form,
            "errors": errors or {},
            "flash": flash,
        },
        status_code=status_code,
    )


def _form_defaults(settings: config.Settings) -> dict:
    return {
        "subdomain": "",
        "domain": settings.default_domain,
        "target": "",
        "authelia": True,
        "bypass_caddy": False,
        "description": "",
        "hostname": "",
    }


def _checkbox(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")


def _settings(request: Request) -> config.Settings:
    return request.app.state.settings


def _cf(request: Request) -> cloudflare.CloudflareClient:
    return request.app.state.cf


async def _list_views(
    request: Request, *, live: bool = False
) -> list[routes.RouteView]:
    return await routes.list_routes(_settings(request), _cf(request), check_live=live)


def _flash_for_routegate_error(verb: str, hostname: str, e: Exception) -> dict:
    if isinstance(e, services.ProtectedHostnameError):
        return {"kind": "error", "message": f"{hostname} is protected"}
    if isinstance(e, services.PartiallyAppliedError):
        return {
            "kind": "error",
            "message": (
                f"Failed to {verb} {hostname}: {e.original}. Rollback also reported "
                f"errors — system may be inconsistent. Check Caddy and cloudflared logs."
            ),
        }
    if isinstance(e, services.RolledBackError):
        return {
            "kind": "error",
            "message": f"Failed to {verb} {hostname}: {e.original}. No changes made.",
        }
    return {"kind": "error", "message": f"Failed to {verb} {hostname}: {e}"}


# ----------------------------------------------------------------------------
# read endpoints


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, live: bool = False):
    views = await _list_views(request, live=live)
    return _panel(
        request,
        routes_views=views,
        settings=_settings(request),
        live=live,
    )


@app.get("/routes/new", response_class=HTMLResponse)
async def new_route_form(request: Request):
    return _form_panel(
        request,
        mode="create",
        form=_form_defaults(_settings(request)),
    )


@app.get("/routes/{hostname}/edit", response_class=HTMLResponse)
async def edit_route_form(request: Request, hostname: str):
    settings = _settings(request)
    if hostname in settings.protected_hostnames:
        raise HTTPException(403, "hostname is protected")
    caddy_doc = caddy.load(settings.caddyfile_path)
    tunnel_doc = tunnel.load(settings.cloudflared_config_path)
    caddy_route = caddy_doc.find_route(hostname)
    tunnel_entry = tunnel_doc.find(hostname)
    if not caddy_route and not tunnel_entry:
        raise HTTPException(404, "route not found")

    if caddy_route:
        target = caddy_route.target
        authelia = caddy_route.authelia
        bypass = False
    else:
        target = _strip_scheme(tunnel_entry.get("service", ""))
        authelia = False
        bypass = True

    subdomain, _, domain = hostname.partition(".")
    return _form_panel(
        request,
        mode="edit",
        form={
            "hostname": hostname,
            "subdomain": subdomain,
            "domain": domain or settings.default_domain,
            "target": target,
            "authelia": authelia,
            "bypass_caddy": bypass,
            "description": "",
        },
    )


@app.get("/routes/{hostname}/row", response_class=HTMLResponse)
async def get_route_row(request: Request, hostname: str, live: bool = False):
    """Re-render a single row. Used by the delete-confirmation 'Cancel' button."""
    views = await _list_views(request, live=live)
    target = next((v for v in views if v.hostname == hostname), None)
    if target is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "_row.html",
        {"request": request, "r": target, "live": live},
    )


@app.get("/routes/{hostname}/confirm-delete", response_class=HTMLResponse)
async def confirm_delete_row(request: Request, hostname: str, live: bool = False):
    settings = _settings(request)
    if hostname in settings.protected_hostnames:
        raise HTTPException(403, "hostname is protected")
    views = await _list_views(request, live=live)
    target = next((v for v in views if v.hostname == hostname), None)
    if target is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "_row_confirm.html",
        {"request": request, "r": target, "live": live},
    )


# ----------------------------------------------------------------------------
# write endpoints


@app.post("/routes", response_class=HTMLResponse)
async def create_route(
    request: Request,
    subdomain: str = Form(""),
    domain: str = Form(""),
    target: str = Form(""),
    authelia: str | None = Form(None),
    bypass_caddy: str | None = Form(None),
    description: str = Form(""),
):
    settings = _settings(request)
    domain = (domain or settings.default_domain).strip()
    subdomain = subdomain.strip().lower()
    target = target.strip()
    form = {
        "subdomain": subdomain,
        "domain": domain,
        "target": target,
        "authelia": _checkbox(authelia),
        "bypass_caddy": _checkbox(bypass_caddy),
        "description": description,
        "hostname": f"{subdomain}.{domain}",
    }

    req = services.CreateRouteRequest(
        subdomain=subdomain,
        domain=domain,
        target=target,
        authelia=form["authelia"],
        bypass_caddy=form["bypass_caddy"],
    )
    try:
        await services.create_route(
            req,
            settings,
            _cf(request),
            on_reload_caddy=lambda: reload.reload_caddy(settings.caddy_container_name),
            on_restart_cloudflared=lambda: reload.restart_cloudflared(
                settings.cloudflared_container_name
            ),
        )
    except services.ValidationError as e:
        return _form_panel(
            request,
            mode="create",
            form=form,
            errors={e.field or "_": str(e)},
            status_code=400,
        )
    except services.ProtectedHostnameError as e:
        return _form_panel(
            request,
            mode="create",
            form=form,
            flash={"kind": "error", "message": str(e)},
            status_code=403,
        )
    except services.RoutegateError as e:
        return _form_panel(
            request,
            mode="create",
            form=form,
            flash=_flash_for_routegate_error("create", req.hostname, e),
            status_code=500,
        )

    views = await _list_views(request)
    return _panel(
        request,
        routes_views=views,
        settings=settings,
        live=False,
        flash={"kind": "success", "message": f"Created {req.hostname}"},
    )


@app.put("/routes/{hostname}", response_class=HTMLResponse)
async def update_route(
    request: Request,
    hostname: str,
    target: str = Form(""),
    authelia: str | None = Form(None),
    bypass_caddy: str | None = Form(None),
    description: str = Form(""),
):
    settings = _settings(request)
    target = target.strip()
    subdomain, _, domain = hostname.partition(".")
    form = {
        "subdomain": subdomain,
        "domain": domain or settings.default_domain,
        "target": target,
        "authelia": _checkbox(authelia),
        "bypass_caddy": _checkbox(bypass_caddy),
        "description": description,
        "hostname": hostname,
    }
    req = services.UpdateRouteRequest(
        hostname=hostname,
        target=target,
        authelia=form["authelia"],
        bypass_caddy=form["bypass_caddy"],
    )
    try:
        await services.update_route(
            req,
            settings,
            _cf(request),
            on_reload_caddy=lambda: reload.reload_caddy(settings.caddy_container_name),
            on_restart_cloudflared=lambda: reload.restart_cloudflared(
                settings.cloudflared_container_name
            ),
        )
    except services.ValidationError as e:
        return _form_panel(
            request,
            mode="edit",
            form=form,
            errors={e.field or "_": str(e)},
            status_code=400,
        )
    except services.ProtectedHostnameError as e:
        return _form_panel(
            request,
            mode="edit",
            form=form,
            flash={"kind": "error", "message": str(e)},
            status_code=403,
        )
    except services.RoutegateError as e:
        return _form_panel(
            request,
            mode="edit",
            form=form,
            flash=_flash_for_routegate_error("update", hostname, e),
            status_code=500,
        )

    views = await _list_views(request)
    return _panel(
        request,
        routes_views=views,
        settings=settings,
        live=False,
        flash={"kind": "success", "message": f"Updated {hostname}"},
    )


@app.delete("/routes/{hostname}", response_class=HTMLResponse)
async def delete_route(request: Request, hostname: str):
    settings = _settings(request)
    try:
        await services.delete_route(
            hostname,
            settings,
            _cf(request),
            on_reload_caddy=lambda: reload.reload_caddy(settings.caddy_container_name),
            on_restart_cloudflared=lambda: reload.restart_cloudflared(
                settings.cloudflared_container_name
            ),
        )
        flash = {"kind": "success", "message": f"Deleted {hostname}"}
    except services.ProtectedHostnameError as e:
        return _panel(
            request,
            routes_views=await _list_views(request),
            settings=settings,
            live=False,
            flash={"kind": "error", "message": str(e)},
            status_code=403,
        )
    except services.ValidationError as e:
        return _panel(
            request,
            routes_views=await _list_views(request),
            settings=settings,
            live=False,
            flash={"kind": "error", "message": str(e)},
            status_code=400,
        )
    except services.RoutegateError as e:
        return _panel(
            request,
            routes_views=await _list_views(request),
            settings=settings,
            live=False,
            flash=_flash_for_routegate_error("delete", hostname, e),
            status_code=500,
        )

    views = await _list_views(request)
    return _panel(
        request, routes_views=views, settings=settings, live=False, flash=flash
    )


# ----------------------------------------------------------------------------
# misc


def _strip_scheme(svc: str) -> str:
    for p in ("http://", "https://"):
        if svc.startswith(p):
            return svc[len(p) :]
    return svc


def run() -> None:
    import uvicorn

    uvicorn.run("routegate.main:app", host="0.0.0.0", port=8000)
