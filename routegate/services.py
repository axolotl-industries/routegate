"""Orchestration of multi-source route changes with rollback.

Each create/update/delete operation touches up to three sources of truth:
Cloudflare DNS, the cloudflared `config.yml`, and the Caddyfile. If any step
fails, prior steps are rolled back in reverse order.

Rollback strategy:
- File mutations (Caddyfile, tunnel config) are undone via byte-level snapshots
  taken before the mutation. Restoring a snapshot is atomic and oblivious to
  what the mutation actually did.
- DNS mutations are undone via inverse API calls (delete-after-create,
  re-create-after-delete).
- Reload/restart of services have no clean inverse. If reload fails, we
  rollback the underlying disk/DNS changes; once disk state matches the prior
  config, we attempt one final reload to bring services into alignment.
- Validation errors are raised before any mutation; nothing to rollback.

The orchestration module is deliberately ignorant of FastAPI/HTTP. The HTTP
handlers translate `RoutegateError` subclasses into responses.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import caddy, cloudflare, tunnel
from .config import Settings

ReloadFn = Callable[[], None]
SubdomainPattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
TargetPattern = re.compile(r"^[A-Za-z0-9_.-]+:\d{1,5}$")


class RoutegateError(RuntimeError):
    """Base class for orchestration errors."""


class ValidationError(RoutegateError):
    """Pre-flight validation failed; nothing was attempted."""

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


class ProtectedHostnameError(RoutegateError):
    """Hostname is in the protected list and may not be modified via the UI."""


class RolledBackError(RoutegateError):
    """An operation failed; prior committed steps were rolled back."""

    def __init__(
        self,
        message: str,
        *,
        original: BaseException,
        rollback_errors: list[BaseException],
    ):
        super().__init__(message)
        self.original = original
        self.rollback_errors = rollback_errors


class PartiallyAppliedError(RoutegateError):
    """Rollback itself failed. System may be in an inconsistent state."""

    def __init__(
        self,
        message: str,
        *,
        original: BaseException,
        rollback_errors: list[BaseException],
    ):
        super().__init__(message)
        self.original = original
        self.rollback_errors = rollback_errors


@dataclass
class CreateRouteRequest:
    subdomain: str
    domain: str
    target: str
    authelia: bool
    bypass_caddy: bool

    @property
    def hostname(self) -> str:
        return f"{self.subdomain}.{self.domain}"


@dataclass
class UpdateRouteRequest:
    hostname: str
    target: str
    authelia: bool
    bypass_caddy: bool


# ----------------------------------------------------------------------------
# public API


async def create_route(
    req: CreateRouteRequest,
    settings: Settings,
    cf: cloudflare.CloudflareClient,
    *,
    on_reload_caddy: ReloadFn | None = None,
    on_restart_cloudflared: ReloadFn | None = None,
) -> None:
    _validate_subdomain(req.subdomain)
    _validate_target(req.target)
    if req.hostname in settings.protected_hostnames:
        raise ProtectedHostnameError(f"{req.hostname} is protected")

    caddy_doc = caddy.load(settings.caddyfile_path)
    tunnel_doc = tunnel.load(settings.cloudflared_config_path)
    if caddy_doc.find_route(req.hostname):
        raise ValidationError(
            f"hostname {req.hostname} already in Caddyfile", field="subdomain"
        )
    if tunnel_doc.find(req.hostname):
        raise ValidationError(
            f"hostname {req.hostname} already in tunnel config", field="subdomain"
        )
    try:
        existing_cname = await _find_cname(
            cf, req.hostname, settings.tunnel_cname_target
        )
    except cloudflare.CloudflareError as e:
        raise RoutegateError(f"Cloudflare lookup failed: {e}") from e
    if existing_cname is not None:
        raise ValidationError(
            f"hostname {req.hostname} already has a tunnel CNAME",
            field="subdomain",
        )

    undo: list[_UndoStep] = []
    services_touched = False
    try:
        # Step 1: Cloudflare CNAME.
        record = await cf.create_cname(
            name=req.hostname, content=settings.tunnel_cname_target, proxied=True
        )
        undo.append(_UndoStep("delete CNAME", _delete_cname_action(cf, record.id)))

        # Step 2: tunnel ingress entry.
        tunnel_target = (
            f"http://{req.target}" if req.bypass_caddy else settings.caddy_tunnel_target
        )
        _add_tunnel_entry(settings, req.hostname, tunnel_target, undo)

        # Step 3: Caddy entry (skipped when bypassing).
        if not req.bypass_caddy:
            _add_caddy_entry(
                settings,
                req.hostname,
                req.target,
                authelia=req.authelia,
                undo=undo,
            )

        # Step 4: reload Caddy.
        if on_reload_caddy and not req.bypass_caddy:
            services_touched = True
            await asyncio.to_thread(on_reload_caddy)

        # Step 5: restart cloudflared.
        if on_restart_cloudflared:
            services_touched = True
            await asyncio.to_thread(on_restart_cloudflared)
    except Exception as e:
        await _rollback_and_realign(
            undo,
            services_touched,
            on_reload_caddy,
            on_restart_cloudflared,
            original=e,
            verb="create",
        )


async def update_route(
    req: UpdateRouteRequest,
    settings: Settings,
    cf: cloudflare.CloudflareClient,
    *,
    on_reload_caddy: ReloadFn | None = None,
    on_restart_cloudflared: ReloadFn | None = None,
) -> None:
    _validate_target(req.target)
    if req.hostname in settings.protected_hostnames:
        raise ProtectedHostnameError(f"{req.hostname} is protected")

    caddy_doc = caddy.load(settings.caddyfile_path)
    tunnel_doc = tunnel.load(settings.cloudflared_config_path)
    try:
        existing_cname = await _find_cname(
            cf, req.hostname, settings.tunnel_cname_target
        )
    except cloudflare.CloudflareError as e:
        raise RoutegateError(f"Cloudflare lookup failed: {e}") from e
    if not (caddy_doc.find_route(req.hostname) or tunnel_doc.find(req.hostname) or existing_cname):
        raise ValidationError(
            f"hostname {req.hostname} not found in any source", field="subdomain"
        )

    undo: list[_UndoStep] = []
    services_touched = False
    try:
        # Tunnel: rewrite the entry. Always touched, since target may change
        # (bypass_caddy flips between caddy and direct target).
        tunnel_target = (
            f"http://{req.target}" if req.bypass_caddy else settings.caddy_tunnel_target
        )
        snap = _snapshot_file(settings.cloudflared_config_path)
        td = tunnel.load(settings.cloudflared_config_path)
        if td.find(req.hostname):
            td.update(req.hostname, service=tunnel_target)
        else:
            td.add(req.hostname, tunnel_target)
        tunnel.save(td, settings.cloudflared_config_path)
        undo.append(_UndoStep("restore tunnel config", _restore_file_action(snap)))

        # Caddy:
        caddy_snap = _snapshot_file(settings.caddyfile_path)
        cd = caddy.load(settings.caddyfile_path)
        if req.bypass_caddy:
            cd.remove_route(req.hostname)
        else:
            existing = cd.find_route(req.hostname)
            if existing is not None:
                existing.target = req.target
                existing.authelia = req.authelia
            else:
                cd.add_route(
                    caddy.Route(
                        hostname=req.hostname,
                        scheme="http",
                        target=req.target,
                        authelia=req.authelia,
                    )
                )
        caddy.save(cd, settings.caddyfile_path)
        undo.append(_UndoStep("restore Caddyfile", _restore_file_action(caddy_snap)))

        # Reloads.
        if on_reload_caddy:
            services_touched = True
            await asyncio.to_thread(on_reload_caddy)
        if on_restart_cloudflared:
            services_touched = True
            await asyncio.to_thread(on_restart_cloudflared)
    except Exception as e:
        await _rollback_and_realign(
            undo,
            services_touched,
            on_reload_caddy,
            on_restart_cloudflared,
            original=e,
            verb="update",
        )


async def delete_route(
    hostname: str,
    settings: Settings,
    cf: cloudflare.CloudflareClient,
    *,
    on_reload_caddy: ReloadFn | None = None,
    on_restart_cloudflared: ReloadFn | None = None,
) -> None:
    if hostname in settings.protected_hostnames:
        raise ProtectedHostnameError(f"{hostname} is protected")

    caddy_doc = caddy.load(settings.caddyfile_path)
    tunnel_doc = tunnel.load(settings.cloudflared_config_path)
    try:
        existing_cname = await _find_cname(cf, hostname, settings.tunnel_cname_target)
    except cloudflare.CloudflareError as e:
        raise RoutegateError(f"Cloudflare lookup failed: {e}") from e

    if not (caddy_doc.find_route(hostname) or tunnel_doc.find(hostname) or existing_cname):
        raise ValidationError(f"hostname {hostname} not found in any source")

    undo: list[_UndoStep] = []
    services_touched = False
    try:
        # 1. Caddy first.
        if caddy_doc.find_route(hostname):
            caddy_snap = _snapshot_file(settings.caddyfile_path)
            cd = caddy.load(settings.caddyfile_path)
            cd.remove_route(hostname)
            caddy.save(cd, settings.caddyfile_path)
            undo.append(_UndoStep("restore Caddyfile", _restore_file_action(caddy_snap)))

        # 2. Tunnel.
        if tunnel_doc.find(hostname):
            tunnel_snap = _snapshot_file(settings.cloudflared_config_path)
            td = tunnel.load(settings.cloudflared_config_path)
            td.remove(hostname)
            tunnel.save(td, settings.cloudflared_config_path)
            undo.append(
                _UndoStep("restore tunnel config", _restore_file_action(tunnel_snap))
            )

        # 3. CNAME.
        if existing_cname is not None:
            cname_snapshot = existing_cname
            await cf.delete_cname(existing_cname.id)
            undo.append(
                _UndoStep(
                    "recreate CNAME",
                    _recreate_cname_action(cf, cname_snapshot),
                )
            )

        # 4. Reloads.
        if on_reload_caddy:
            services_touched = True
            await asyncio.to_thread(on_reload_caddy)
        if on_restart_cloudflared:
            services_touched = True
            await asyncio.to_thread(on_restart_cloudflared)
    except Exception as e:
        await _rollback_and_realign(
            undo,
            services_touched,
            on_reload_caddy,
            on_restart_cloudflared,
            original=e,
            verb="delete",
        )


# ----------------------------------------------------------------------------
# internals


@dataclass
class _UndoStep:
    label: str
    action: Callable[[], Awaitable[None]]


def _validate_subdomain(s: str) -> None:
    if not s:
        raise ValidationError("subdomain is required", field="subdomain")
    if not SubdomainPattern.match(s):
        raise ValidationError(
            "subdomain may only contain lowercase letters, digits, and hyphens",
            field="subdomain",
        )


def _validate_target(t: str) -> None:
    if not t:
        raise ValidationError("target is required", field="target")
    if not TargetPattern.match(t):
        raise ValidationError(
            "target must be host:port (e.g. 192.168.1.10:8080)", field="target"
        )


def _snapshot_file(path: str) -> tuple[str, bytes]:
    return (path, Path(path).read_bytes())


def _restore_file_action(
    snapshot: tuple[str, bytes],
) -> Callable[[], Awaitable[None]]:
    path, data = snapshot

    async def undo() -> None:
        Path(path).write_bytes(data)

    return undo


def _delete_cname_action(
    cf: cloudflare.CloudflareClient, record_id: str
) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        await cf.delete_cname(record_id)

    return undo


def _recreate_cname_action(
    cf: cloudflare.CloudflareClient, record: cloudflare.DnsRecord
) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        await cf.create_cname(
            name=record.name, content=record.content, proxied=record.proxied
        )

    return undo


def _add_tunnel_entry(
    settings: Settings,
    hostname: str,
    tunnel_target: str,
    undo: list[_UndoStep],
) -> None:
    snap = _snapshot_file(settings.cloudflared_config_path)
    td = tunnel.load(settings.cloudflared_config_path)
    td.add(hostname, tunnel_target)
    tunnel.save(td, settings.cloudflared_config_path)
    undo.append(_UndoStep("restore tunnel config", _restore_file_action(snap)))


def _add_caddy_entry(
    settings: Settings,
    hostname: str,
    target: str,
    *,
    authelia: bool,
    undo: list[_UndoStep],
) -> None:
    snap = _snapshot_file(settings.caddyfile_path)
    cd = caddy.load(settings.caddyfile_path)
    cd.add_route(
        caddy.Route(
            hostname=hostname,
            scheme="http",
            target=target,
            authelia=authelia,
        )
    )
    caddy.save(cd, settings.caddyfile_path)
    undo.append(_UndoStep("restore Caddyfile", _restore_file_action(snap)))


async def _find_cname(
    cf: cloudflare.CloudflareClient, hostname: str, tunnel_target: str
) -> cloudflare.DnsRecord | None:
    for r in await cf.list_cname_records(content_filter=tunnel_target):
        if r.name == hostname:
            return r
    return None


async def _rollback_and_realign(
    undo: list[_UndoStep],
    services_touched: bool,
    on_reload_caddy: ReloadFn | None,
    on_restart_cloudflared: ReloadFn | None,
    *,
    original: BaseException,
    verb: str,
) -> None:
    rollback_errors: list[BaseException] = []
    for step in reversed(undo):
        try:
            await step.action()
        except Exception as re:
            rollback_errors.append(re)

    realign_failed = False
    if services_touched:
        if on_reload_caddy is not None:
            try:
                await asyncio.to_thread(on_reload_caddy)
            except Exception as re:
                rollback_errors.append(re)
                realign_failed = True
        if on_restart_cloudflared is not None:
            try:
                await asyncio.to_thread(on_restart_cloudflared)
            except Exception as re:
                rollback_errors.append(re)
                realign_failed = True

    msg = f"{verb} failed: {original}; rolled back"
    if realign_failed:
        raise PartiallyAppliedError(
            f"{msg} but realign reload failed",
            original=original,
            rollback_errors=rollback_errors,
        ) from original
    raise RolledBackError(msg, original=original, rollback_errors=rollback_errors) from original
