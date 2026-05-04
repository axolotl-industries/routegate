"""Business logic combining Caddyfile, cloudflared, and Cloudflare DNS.

Builds a unified view of every hostname routegate knows about, joining the
three sources by hostname.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from . import caddy, cloudflare, tunnel
from .config import Settings


@dataclass
class RouteView:
    hostname: str
    target: str | None         # taken from Caddy, falling back to cloudflared
    authelia: bool | None      # from Caddyfile if present, else None
    in_caddy: bool
    in_tunnel: bool
    in_dns: bool
    bypass_caddy: bool         # has tunnel + dns but no caddy entry
    protected: bool
    live_status: str | None = None  # "ok" | "error" | None (not checked yet)


async def list_routes(
    settings: Settings,
    cf: cloudflare.CloudflareClient,
    *,
    check_live: bool = False,
) -> list[RouteView]:
    caddy_doc = caddy.load(settings.caddyfile_path)
    tunnel_doc = tunnel.load(settings.cloudflared_config_path)
    dns_records = await cf.list_cname_records(
        content_filter=settings.tunnel_cname_target
    )

    caddy_by_host = {r.hostname: r for r in caddy_doc.routes}
    tunnel_hosts = set(tunnel_doc.hostnames())
    dns_hosts = {r.name for r in dns_records}

    all_hosts = set(caddy_by_host) | tunnel_hosts | dns_hosts

    views: list[RouteView] = []
    for host in sorted(all_hosts):
        caddy_route = caddy_by_host.get(host)
        tunnel_entry = tunnel_doc.find(host)
        target = None
        if caddy_route:
            target = caddy_route.target
        elif tunnel_entry:
            target = tunnel_entry.get("service")
        in_caddy = caddy_route is not None
        in_tunnel = host in tunnel_hosts
        in_dns = host in dns_hosts
        views.append(
            RouteView(
                hostname=host,
                target=target,
                authelia=caddy_route.authelia if caddy_route else None,
                in_caddy=in_caddy,
                in_tunnel=in_tunnel,
                in_dns=in_dns,
                bypass_caddy=(in_tunnel and in_dns and not in_caddy),
                protected=host in settings.protected_hostnames,
            )
        )

    if check_live:
        await _attach_live_statuses(views)

    return views


async def _attach_live_statuses(views: list[RouteView]) -> None:
    async with httpx.AsyncClient(
        timeout=5.0, follow_redirects=False, verify=True
    ) as client:
        results = await asyncio.gather(
            *[_probe(client, v.hostname) for v in views],
            return_exceptions=True,
        )
    for view, result in zip(views, results):
        if isinstance(result, Exception):
            view.live_status = "error"
        else:
            view.live_status = result


async def _probe(client: httpx.AsyncClient, hostname: str) -> str:
    try:
        r = await client.head(f"https://{hostname}", timeout=5.0)
        return "ok" if r.status_code < 500 else "error"
    except Exception:
        return "error"
