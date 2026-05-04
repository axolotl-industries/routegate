# routegate

Web GUI for managing reverse-proxy routes by manipulating three sources of
truth in concert:

- **Caddyfile** — Caddy reverse proxy
- **cloudflared `config.yml`** — Cloudflare Tunnel ingress
- **Cloudflare DNS** — CNAMEs pointing at the tunnel

Designed to run as a Docker container behind Authelia. No database; the three
configs/APIs are the source of truth.

## What v1 does

- **Read**: lists every hostname routegate knows about, joining the three
  sources. Shows target, Authelia status, presence flags per source, optional
  live-status probe.
- **Write**: create / edit / delete routes via an HTMX form. Each operation
  is *transactional* — if any step fails, prior steps roll back automatically.
- **Service reload**: after a successful change, runs `caddy reload` against
  the caddy container and `docker restart cloudflared`, both via the Docker
  Engine API on the bind-mounted socket.
- **Protection**: hostnames in `PROTECTED_HOSTNAMES` (default `auth.<domain>`)
  are listed but cannot be edited or deleted from the UI. This keeps you from
  accidentally locking yourself out of Authelia.

## What v1 does not do (and probably never will)

- Manage stray DNS records (orphans show in the list with `caddy=✗ tunnel=✗
  dns=✓` so you can fix them by hand).
- Reorder ingress rules in `cloudflared` config.
- Undo / change history.
- Description field is in the form but not persisted in v1 (Caddyfile
  comments would survive parser-write cycles only with extra work).

## Architecture

```
routegate/
├── caddy.py        Caddyfile parser/writer (subset). Brace-counting tokenizer.
├── tunnel.py       cloudflared config.yml round-trip (ruamel.yaml).
├── cloudflare.py   Cloudflare DNS API client (httpx).
├── routes.py       Joins the three sources by hostname.
├── services.py     Orchestrator with snapshot-based rollback.
├── reload.py       caddy reload + cloudflared restart via docker SDK.
├── main.py         FastAPI app. Lifespan owns one CloudflareClient per process.
├── config.py       Env vars.
└── templates/      Jinja2 + HTMX. Inline CSS, dark theme only.
```

### Caddyfile parser

Recognises a small subset and preserves everything else verbatim:

- Global block (`{ ... }`) and named snippets (`(authelia) { ... }`) are
  preserved byte-for-byte.
- Route blocks of the form `[http://]hostname { ... reverse_proxy host:port [{...}] ... }`
  are parsed into structured routes. `import authelia` is recognised; nested
  options on `reverse_proxy` are kept; other top-level directives (e.g.
  `redir`) stay in `extra_lines`.
- A route block containing a nested non-`reverse_proxy` directive is preserved
  verbatim so we never silently lose syntax we don't model.

The writer emits managed routes in a canonical layout. Round-trip equality is
asserted at the **object** level (parse → dump → parse yields equal objects),
not byte level.

### Rollback contract

For each write operation, before mutating a file we take a byte-level
snapshot. If a later step fails, the snapshot is written back. DNS mutations
are undone by inverse API calls. Reload/restart steps have no clean inverse;
when one fails, file/DNS state is rolled back and a final reload runs to
realign the live services with the rolled-back disk state. If the realign
itself fails, the operation raises `PartiallyAppliedError` so the UI can
surface that the system may need manual attention.

The contract is locked down in `tests/test_services.py` (18 tests).

## Configuration

| Var | Default | Required |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | — | yes |
| `CLOUDFLARE_ZONE_ID` | — | yes |
| `TUNNEL_UUID` | — | yes (used to build CNAME targets `<uuid>.cfargotunnel.com`) |
| `DEFAULT_DOMAIN` | `geoffflix.uk` | |
| `CADDYFILE_PATH` | `/config/caddy/Caddyfile` | |
| `CLOUDFLARED_CONFIG_PATH` | `/config/cloudflared/config.yml` | |
| `CADDY_CONTAINER_NAME` | `caddy` | |
| `CLOUDFLARED_CONTAINER_NAME` | `cloudflared` | |
| `PROTECTED_HOSTNAMES` | `auth.<DEFAULT_DOMAIN>` | comma-separated; UI refuses edit/delete |

### Tunnel ingress model

routegate writes ingress entries to `cloudflared/config.yml` **only for
bypass-Caddy routes**. Non-bypass routes ride on whatever catch-all entry
already exists in your cloudflared config (commonly something like
`- service: http://<host-ip>:80` pointing at Caddy). This matches the standard
Caddy + Cloudflare Tunnel pattern: the tunnel funnels everything through Caddy
by default, and Caddy dispatches by Host header. Routes that need to bypass
Caddy entirely (e.g., apps with their own TLS or unusual protocols) get an
explicit ingress entry of their own.

Make sure your cloudflared config has a working catch-all before adding
non-bypass routes through routegate, otherwise the tunnel has no rule for them.

## Deploying

`docker-compose.yaml` ships with the right wiring. Set the secrets via env
vars or a `.env` file alongside it:

```bash
cat > .env <<'EOF'
CLOUDFLARE_API_TOKEN=cf-xxxxxxxx
CLOUDFLARE_ZONE_ID=xxxxxxxx
TUNNEL_UUID=xxxxxxxx
CADDYFILE_HOST_PATH=/srv/caddy/Caddyfile
CLOUDFLARED_CONFIG_HOST_PATH=/srv/cloudflared/config.yml
EOF
docker compose up -d --build
```

Mount the same Caddyfile and cloudflared `config.yml` paths that your `caddy`
and `cloudflared` containers already consume — routegate edits them in place.

`/var/run/docker.sock` is mounted into the routegate container so it can
issue `caddy reload` and `docker restart cloudflared`. The `caddy` and
`cloudflared` container names default to those literals; override via env if
yours differ.

Then put routegate behind Authelia in your Caddyfile, e.g.

```caddyfile
http://routegate.geoffflix.uk {
    import authelia
    reverse_proxy routegate:8000
}
```

## Local development

```bash
/opt/homebrew/bin/python3.14 -m venv .venv   # macOS Homebrew Python
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/uvicorn routegate.main:app --reload
```

The app needs the same env vars listed above. For local poking against
fixture files:

```bash
export CLOUDFLARE_API_TOKEN=dummy CLOUDFLARE_ZONE_ID=zone TUNNEL_UUID=tu
export CADDYFILE_PATH=tests/fixtures/sample_caddyfile
export CLOUDFLARED_CONFIG_PATH=tests/fixtures/sample_config.yml
.venv/bin/uvicorn routegate.main:app --reload
```

(Cloudflare API calls will fail with the dummy token; the read-path will show
the configs but the DNS column will be all `✗`. Use `respx` or hit a real
zone for full local exercise.)

## Tests

```bash
pytest
```

48 tests across 4 files:

- `test_caddy.py` — parser/writer round-trip, mutation isolation, edge cases
- `test_tunnel.py` — round-trip with comment preservation, catch-all positioning
- `test_cloudflare.py` — DNS client (mocked with respx)
- `test_services.py` — rollback contract: every step's failure restores prior state

There are no integration tests against real Cloudflare or a real Docker
daemon. The `respx` and fake-client patterns cover those surfaces.
