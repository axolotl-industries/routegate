import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    caddyfile_path: str
    cloudflared_config_path: str
    cloudflare_api_token: str
    cloudflare_zone_id: str
    tunnel_uuid: str
    default_domain: str
    caddy_container_name: str
    cloudflared_container_name: str
    protected_hostnames: frozenset[str]

    @property
    def tunnel_cname_target(self) -> str:
        return f"{self.tunnel_uuid}.cfargotunnel.com"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value


def _validate_config_file(path: str, env_var: str) -> None:
    """Fail fast if a config-file path doesn't resolve to a regular file.

    Catches the Docker bind-mount foot-gun where a non-existent host path
    becomes an empty directory at the destination, causing a confusing
    IsADirectoryError on first request instead of a clear startup failure.
    """
    p = Path(path)
    if not p.exists():
        raise RuntimeError(
            f"{env_var}={path} does not exist. "
            "If running under Docker, check your bind mount points at a real "
            "file on the host (Docker auto-creates an empty directory for "
            "missing host paths)."
        )
    if p.is_dir():
        raise RuntimeError(
            f"{env_var}={path} is a directory, not a file. "
            "If running under Docker, this usually means the host path didn't "
            "exist when the container started and Docker created an empty "
            "directory in its place. Stop the container, remove the empty "
            "directory, point the env var at the real config file, and try "
            "again."
        )


def load() -> Settings:
    domain = os.environ.get("DEFAULT_DOMAIN", "geoffflix.uk")
    protected_default = f"auth.{domain}"
    protected_raw = os.environ.get("PROTECTED_HOSTNAMES", protected_default)
    protected = frozenset(h.strip() for h in protected_raw.split(",") if h.strip())
    caddy_container = os.environ.get("CADDY_CONTAINER_NAME", "caddy")
    caddyfile_path = os.environ.get("CADDYFILE_PATH", "/config/caddy/Caddyfile")
    cloudflared_config_path = os.environ.get(
        "CLOUDFLARED_CONFIG_PATH", "/config/cloudflared/config.yml"
    )
    _validate_config_file(caddyfile_path, "CADDYFILE_PATH")
    _validate_config_file(cloudflared_config_path, "CLOUDFLARED_CONFIG_PATH")
    return Settings(
        caddyfile_path=caddyfile_path,
        cloudflared_config_path=cloudflared_config_path,
        cloudflare_api_token=_required("CLOUDFLARE_API_TOKEN"),
        cloudflare_zone_id=_required("CLOUDFLARE_ZONE_ID"),
        tunnel_uuid=_required("TUNNEL_UUID"),
        default_domain=domain,
        caddy_container_name=caddy_container,
        cloudflared_container_name=os.environ.get(
            "CLOUDFLARED_CONTAINER_NAME", "cloudflared"
        ),
        protected_hostnames=protected,
    )
