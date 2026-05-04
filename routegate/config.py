import os
from dataclasses import dataclass


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
    caddy_tunnel_target: str
    protected_hostnames: frozenset[str]

    @property
    def tunnel_cname_target(self) -> str:
        return f"{self.tunnel_uuid}.cfargotunnel.com"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value


def load() -> Settings:
    domain = os.environ.get("DEFAULT_DOMAIN", "geoffflix.uk")
    protected_default = f"auth.{domain}"
    protected_raw = os.environ.get("PROTECTED_HOSTNAMES", protected_default)
    protected = frozenset(h.strip() for h in protected_raw.split(",") if h.strip())
    caddy_container = os.environ.get("CADDY_CONTAINER_NAME", "caddy")
    return Settings(
        caddyfile_path=os.environ.get("CADDYFILE_PATH", "/config/caddy/Caddyfile"),
        cloudflared_config_path=os.environ.get(
            "CLOUDFLARED_CONFIG_PATH", "/config/cloudflared/config.yml"
        ),
        cloudflare_api_token=_required("CLOUDFLARE_API_TOKEN"),
        cloudflare_zone_id=_required("CLOUDFLARE_ZONE_ID"),
        tunnel_uuid=_required("TUNNEL_UUID"),
        default_domain=domain,
        caddy_container_name=caddy_container,
        cloudflared_container_name=os.environ.get(
            "CLOUDFLARED_CONTAINER_NAME", "cloudflared"
        ),
        caddy_tunnel_target=os.environ.get(
            "CADDY_TUNNEL_TARGET", f"http://{caddy_container}:80"
        ),
        protected_hostnames=protected,
    )
