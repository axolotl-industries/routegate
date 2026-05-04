"""Tests for the rollback contract in services.py.

The contract (per session plan):
- If any step fails, prior committed steps roll back in reverse order.
- Validation failures don't touch any source.
- After a successful rollback, file contents and DNS records match the state
  before the operation began.
- Protected hostnames are refused before any work is done.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from routegate import caddy, cloudflare, services, tunnel
from routegate.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------------------
# helpers


def _settings(tmp_path: Path) -> Settings:
    caddy_path = tmp_path / "Caddyfile"
    tunnel_path = tmp_path / "config.yml"
    shutil.copy(FIXTURES / "sample_caddyfile", caddy_path)
    shutil.copy(FIXTURES / "sample_config.yml", tunnel_path)
    return Settings(
        caddyfile_path=str(caddy_path),
        cloudflared_config_path=str(tunnel_path),
        cloudflare_api_token="t",
        cloudflare_zone_id="z",
        tunnel_uuid="abc-uuid",
        default_domain="geoffflix.uk",
        caddy_container_name="caddy",
        cloudflared_container_name="cloudflared",
        caddy_tunnel_target="http://caddy:80",
        protected_hostnames=frozenset({"auth.geoffflix.uk"}),
    )


@dataclass
class FakeCFClient:
    """Records every call. Can be configured to raise on a specific method."""

    records: dict[str, cloudflare.DnsRecord] = field(default_factory=dict)
    fail_on: str | None = None
    next_id: int = 100
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def list_cname_records(
        self, *, content_filter: str | None = None
    ) -> list[cloudflare.DnsRecord]:
        self.calls.append(("list", {"content_filter": content_filter}))
        if self.fail_on == "list":
            raise cloudflare.CloudflareError("simulated list failure")
        out = []
        for r in self.records.values():
            if content_filter is None or r.content == content_filter:
                out.append(r)
        return out

    async def create_cname(
        self, *, name: str, content: str, proxied: bool = True
    ) -> cloudflare.DnsRecord:
        self.calls.append(
            ("create", {"name": name, "content": content, "proxied": proxied})
        )
        if self.fail_on == "create":
            raise cloudflare.CloudflareError("simulated create failure")
        rid = f"rec{self.next_id}"
        self.next_id += 1
        rec = cloudflare.DnsRecord(id=rid, name=name, content=content, proxied=proxied)
        self.records[rid] = rec
        return rec

    async def delete_cname(self, record_id: str) -> None:
        self.calls.append(("delete", {"record_id": record_id}))
        if self.fail_on == "delete":
            raise cloudflare.CloudflareError("simulated delete failure")
        self.records.pop(record_id, None)


def _read(p: str) -> bytes:
    return Path(p).read_bytes()


# ----------------------------------------------------------------------------
# create — happy path


async def test_create_writes_all_three_sources(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()

    reload_calls = []
    restart_calls = []
    await services.create_route(
        services.CreateRouteRequest(
            subdomain="newapp",
            domain="geoffflix.uk",
            target="192.168.1.50:8000",
            authelia=True,
            bypass_caddy=False,
        ),
        s,
        cf,
        on_reload_caddy=lambda: reload_calls.append(1),
        on_restart_cloudflared=lambda: restart_calls.append(1),
    )

    # Caddy
    cd = caddy.load(s.caddyfile_path)
    new = cd.find_route("newapp.geoffflix.uk")
    assert new is not None
    assert new.target == "192.168.1.50:8000"
    assert new.authelia is True

    # Tunnel — points at caddy, not direct target
    td = tunnel.load(s.cloudflared_config_path)
    entry = td.find("newapp.geoffflix.uk")
    assert entry is not None
    assert entry["service"] == "http://caddy:80"

    # CNAME
    assert any(r.name == "newapp.geoffflix.uk" for r in cf.records.values())

    # Reloads called once each
    assert reload_calls == [1]
    assert restart_calls == [1]


async def test_create_with_bypass_caddy_skips_caddy_and_uses_direct_tunnel(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    reload_calls, restart_calls = [], []

    await services.create_route(
        services.CreateRouteRequest(
            subdomain="emby",
            domain="geoffflix.uk",
            target="192.168.1.99:8096",
            authelia=False,
            bypass_caddy=True,
        ),
        s,
        cf,
        on_reload_caddy=lambda: reload_calls.append(1),
        on_restart_cloudflared=lambda: restart_calls.append(1),
    )

    cd = caddy.load(s.caddyfile_path)
    assert cd.find_route("emby.geoffflix.uk") is None

    td = tunnel.load(s.cloudflared_config_path)
    assert td.find("emby.geoffflix.uk")["service"] == "http://192.168.1.99:8096"

    # Caddy reload not called (no caddy work to apply); cloudflared restart yes.
    assert reload_calls == []
    assert restart_calls == [1]


# ----------------------------------------------------------------------------
# create — validation


async def test_create_validation_invalid_subdomain(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)
    with pytest.raises(services.ValidationError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="UPPER",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    assert cf.calls == []


async def test_create_validation_invalid_target(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ValidationError) as ei:
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="ok",
                domain="geoffflix.uk",
                target="not-a-target",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )
    assert ei.value.field == "target"


async def test_create_validation_existing_caddy_route(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ValidationError) as ei:
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="radarr",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )
    assert "already in Caddyfile" in str(ei.value)


async def test_create_protected_hostname_rejected(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ProtectedHostnameError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="auth",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )


# ----------------------------------------------------------------------------
# create — rollback


async def test_create_rollback_when_cname_fails(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient(fail_on="create")
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)

    with pytest.raises(services.RolledBackError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="newapp",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )

    # Files unchanged
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    # No CNAMEs ever existed
    assert cf.records == {}


async def test_create_rollback_when_tunnel_save_fails(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)

    real_save = tunnel.save
    calls = {"n": 0}

    def flaky_save(doc, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated tunnel save failure")
        return real_save(doc, path)

    monkeypatch.setattr(tunnel, "save", flaky_save)
    monkeypatch.setattr(services.tunnel, "save", flaky_save)

    with pytest.raises(services.RolledBackError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="newapp",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )

    # CNAME was created and then deleted
    create_calls = [c for c in cf.calls if c[0] == "create"]
    delete_calls = [c for c in cf.calls if c[0] == "delete"]
    assert len(create_calls) == 1
    assert len(delete_calls) == 1
    assert cf.records == {}

    # Files unchanged
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before


async def test_create_rollback_when_caddy_save_fails(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)

    def flaky_save(doc, path):
        raise RuntimeError("simulated caddy save failure")

    monkeypatch.setattr(services.caddy, "save", flaky_save)

    with pytest.raises(services.RolledBackError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="newapp",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )

    # All sources match prior state
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    assert cf.records == {}


async def test_create_rollback_when_caddy_reload_fails(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)

    reload_attempts = []

    def reload_caddy():
        reload_attempts.append(1)
        if len(reload_attempts) == 1:
            raise RuntimeError("simulated caddy reload failure")

    with pytest.raises(services.RolledBackError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="newapp",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
            on_reload_caddy=reload_caddy,
            on_restart_cloudflared=lambda: None,
        )

    # Files restored
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    assert cf.records == {}
    # Reload was attempted twice: the failing original and the realign after rollback
    assert len(reload_attempts) == 2


async def test_create_partially_applied_when_realign_reload_also_fails(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()

    def reload_caddy():
        raise RuntimeError("reload always fails")

    with pytest.raises(services.PartiallyAppliedError):
        await services.create_route(
            services.CreateRouteRequest(
                subdomain="newapp",
                domain="geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
            on_reload_caddy=reload_caddy,
            on_restart_cloudflared=lambda: None,
        )


# ----------------------------------------------------------------------------
# update


async def test_update_changes_target_and_reverts_on_failure(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)

    # Reload fails first time, succeeds on realign.
    attempts = []

    def reload_caddy():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("simulated reload failure")

    with pytest.raises(services.RolledBackError):
        await services.update_route(
            services.UpdateRouteRequest(
                hostname="radarr.geoffflix.uk",
                target="10.0.0.99:7878",
                authelia=True,
                bypass_caddy=False,
            ),
            s,
            cf,
            on_reload_caddy=reload_caddy,
            on_restart_cloudflared=lambda: None,
        )

    # Both files reverted
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    assert len(attempts) == 2  # original + realign


async def test_update_happy_path_changes_caddy_target(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    await services.update_route(
        services.UpdateRouteRequest(
            hostname="radarr.geoffflix.uk",
            target="10.0.0.99:7878",
            authelia=True,
            bypass_caddy=False,
        ),
        s,
        cf,
    )
    cd = caddy.load(s.caddyfile_path)
    assert cd.find_route("radarr.geoffflix.uk").target == "10.0.0.99:7878"


async def test_update_protected_rejected(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ProtectedHostnameError):
        await services.update_route(
            services.UpdateRouteRequest(
                hostname="auth.geoffflix.uk",
                target="1.2.3.4:80",
                authelia=False,
                bypass_caddy=False,
            ),
            s,
            cf,
        )


# ----------------------------------------------------------------------------
# delete


async def test_delete_removes_from_all_three_sources(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient(
        records={
            "rec1": cloudflare.DnsRecord(
                id="rec1",
                name="radarr.geoffflix.uk",
                content=s.tunnel_cname_target,
                proxied=True,
            )
        }
    )
    await services.delete_route("radarr.geoffflix.uk", s, cf)
    cd = caddy.load(s.caddyfile_path)
    assert cd.find_route("radarr.geoffflix.uk") is None
    td = tunnel.load(s.cloudflared_config_path)
    assert td.find("radarr.geoffflix.uk") is None
    assert "rec1" not in cf.records


async def test_delete_rollback_on_cname_failure_restores_files(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient(
        records={
            "rec1": cloudflare.DnsRecord(
                id="rec1",
                name="radarr.geoffflix.uk",
                content=s.tunnel_cname_target,
                proxied=True,
            )
        },
        fail_on="delete",
    )
    caddy_before = _read(s.caddyfile_path)
    tunnel_before = _read(s.cloudflared_config_path)
    with pytest.raises(services.RolledBackError):
        await services.delete_route("radarr.geoffflix.uk", s, cf)
    assert _read(s.caddyfile_path) == caddy_before
    assert _read(s.cloudflared_config_path) == tunnel_before
    # CNAME still present
    assert "rec1" in cf.records


async def test_delete_protected_rejected(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ProtectedHostnameError):
        await services.delete_route("auth.geoffflix.uk", s, cf)


async def test_delete_unknown_hostname_raises_validation(tmp_path):
    s = _settings(tmp_path)
    cf = FakeCFClient()
    with pytest.raises(services.ValidationError):
        await services.delete_route("nope.geoffflix.uk", s, cf)
