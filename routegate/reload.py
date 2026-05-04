"""Service reload commands via the Docker Engine API.

Both functions are synchronous; the orchestration layer runs them in a thread
pool. They take an optional `client` so tests can inject a fake.

The routegate container needs `/var/run/docker.sock` bind-mounted to talk to
the engine.
"""

from __future__ import annotations

from typing import Any


class ReloadError(RuntimeError):
    pass


def _client(client: Any | None) -> Any:
    if client is not None:
        return client
    import docker  # imported lazily so tests don't need the real SDK loaded eagerly

    return docker.from_env()


def reload_caddy(
    container_name: str,
    *,
    config_path: str = "/etc/caddy/Caddyfile",
    client: Any | None = None,
) -> None:
    dc = _client(client)
    try:
        container = dc.containers.get(container_name)
    except Exception as e:
        raise ReloadError(f"caddy container {container_name!r} not found: {e}") from e
    result = container.exec_run(
        ["caddy", "reload", "--config", config_path],
    )
    if result.exit_code != 0:
        output = result.output
        if isinstance(output, (bytes, bytearray)):
            output = output.decode(errors="replace")
        raise ReloadError(
            f"caddy reload exited {result.exit_code}: {output!s}".strip()
        )


def restart_cloudflared(
    container_name: str,
    *,
    client: Any | None = None,
) -> None:
    dc = _client(client)
    try:
        container = dc.containers.get(container_name)
    except Exception as e:
        raise ReloadError(
            f"cloudflared container {container_name!r} not found: {e}"
        ) from e
    try:
        container.restart()
    except Exception as e:
        raise ReloadError(f"cloudflared restart failed: {e}") from e
