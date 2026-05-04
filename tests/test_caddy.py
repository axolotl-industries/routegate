from pathlib import Path

import pytest

from routegate import caddy

FIXTURE = Path(__file__).parent / "fixtures" / "sample_caddyfile"


@pytest.fixture
def doc() -> caddy.CaddyDoc:
    return caddy.parse(FIXTURE.read_text())


def test_parses_expected_route_count(doc):
    hostnames = [r.hostname for r in doc.routes]
    assert "auth.geoffflix.uk" in hostnames
    assert "radarr.geoffflix.uk" in hostnames
    assert "audiobookshelf.geoffflix.uk" in hostnames
    assert "qbittorrent.geoffflix.uk" in hostnames
    assert len(doc.routes) == 18


def test_global_and_snippet_preserved(doc):
    preserved_raws = [
        i.raw for i in doc.items if isinstance(i, caddy.PreservedBlock)
    ]
    # Two preserved top-level blocks: the global block and the (authelia) snippet.
    assert len(preserved_raws) == 2
    assert "auto_https off" in preserved_raws[0]
    assert preserved_raws[1].startswith("(authelia)")
    assert "forward_auth authelia:9091" in preserved_raws[1]


def test_authelia_flag_extracted(doc):
    by_host = {r.hostname: r for r in doc.routes}
    assert by_host["radarr.geoffflix.uk"].authelia is True
    # auth.geoffflix.uk routes to authelia itself, no `import authelia` directive
    assert by_host["auth.geoffflix.uk"].authelia is False
    # audiobookshelf has redir + reverse_proxy but no `import authelia`
    assert by_host["audiobookshelf.geoffflix.uk"].authelia is False


def test_targets_extracted(doc):
    by_host = {r.hostname: r for r in doc.routes}
    assert by_host["radarr.geoffflix.uk"].target == "192.168.1.33:7878"
    assert by_host["auth.geoffflix.uk"].target == "authelia:9091"
    assert by_host["audiobookshelf.geoffflix.uk"].target == "192.168.1.74:13378"
    assert by_host["qbittorrent.geoffflix.uk"].target == "192.168.1.30:8080"


def test_reverse_proxy_options_preserved(doc):
    by_host = {r.hostname: r for r in doc.routes}
    abs_route = by_host["audiobookshelf.geoffflix.uk"]
    assert abs_route.reverse_proxy_options == ["header_up X-Forwarded-Proto https"]
    qb_route = by_host["qbittorrent.geoffflix.uk"]
    assert qb_route.reverse_proxy_options == [
        "header_up -Referer",
        "header_up -Origin",
    ]


def test_extra_directives_preserved(doc):
    by_host = {r.hostname: r for r in doc.routes}
    abs_route = by_host["audiobookshelf.geoffflix.uk"]
    assert abs_route.extra_lines == ["redir / /audiobookshelf/ permanent"]
    # Most routes have no extras
    assert by_host["radarr.geoffflix.uk"].extra_lines == []


def test_scheme_extracted(doc):
    by_host = {r.hostname: r for r in doc.routes}
    assert by_host["radarr.geoffflix.uk"].scheme == "http"


def test_object_level_round_trip(doc):
    """parse -> dump -> parse yields equal objects."""
    text = caddy.dump(doc)
    doc2 = caddy.parse(text)
    assert len(doc.routes) == len(doc2.routes)
    for r1, r2 in zip(doc.routes, doc2.routes):
        assert r1.hostname == r2.hostname
        assert r1.scheme == r2.scheme
        assert r1.target == r2.target
        assert r1.authelia == r2.authelia
        assert r1.reverse_proxy_options == r2.reverse_proxy_options
        assert r1.extra_lines == r2.extra_lines


def test_preserved_blocks_byte_identical_round_trip(doc):
    """PreservedBlock content must round-trip byte-identically."""
    text = caddy.dump(doc)
    doc2 = caddy.parse(text)
    p1 = [i for i in doc.items if isinstance(i, caddy.PreservedBlock)]
    p2 = [i for i in doc2.items if isinstance(i, caddy.PreservedBlock)]
    assert len(p1) == len(p2)
    for a, b in zip(p1, p2):
        assert a.raw == b.raw


def test_mutation_only_affects_target_route(doc):
    radarr = doc.find_route("radarr.geoffflix.uk")
    assert radarr is not None
    radarr.target = "10.0.0.1:9999"
    text = caddy.dump(doc)
    doc2 = caddy.parse(text)
    by_host = {r.hostname: r for r in doc2.routes}
    assert by_host["radarr.geoffflix.uk"].target == "10.0.0.1:9999"
    # No other route was changed
    assert by_host["sonarr.geoffflix.uk"].target == "192.168.1.32:8989"
    assert by_host["audiobookshelf.geoffflix.uk"].target == "192.168.1.74:13378"
    assert (
        by_host["audiobookshelf.geoffflix.uk"].reverse_proxy_options
        == ["header_up X-Forwarded-Proto https"]
    )


def test_add_route_round_trips():
    src = "{\n    auto_https off\n}\n\nhttp://x.example.com {\n    reverse_proxy 1.2.3.4:80\n}\n"
    doc = caddy.parse(src)
    doc.add_route(
        caddy.Route(
            hostname="new.example.com",
            scheme="http",
            target="5.6.7.8:9000",
            authelia=True,
        )
    )
    text = caddy.dump(doc)
    doc2 = caddy.parse(text)
    by_host = {r.hostname: r for r in doc2.routes}
    assert "new.example.com" in by_host
    assert by_host["new.example.com"].target == "5.6.7.8:9000"
    assert by_host["new.example.com"].authelia is True


def test_remove_route_drops_block():
    doc = caddy.parse(FIXTURE.read_text())
    assert doc.remove_route("radarr.geoffflix.uk") is True
    text = caddy.dump(doc)
    assert "radarr.geoffflix.uk" not in text
    # Sibling route still there
    assert "sonarr.geoffflix.uk" in text


def test_unbalanced_braces_raises():
    with pytest.raises(ValueError):
        caddy.parse("foo {\n  bar\n")


def test_route_without_reverse_proxy_is_preserved():
    src = "http://weird.example.com {\n    respond \"hello\"\n}\n"
    doc = caddy.parse(src)
    assert doc.routes == []
    assert any(isinstance(i, caddy.PreservedBlock) for i in doc.items)
