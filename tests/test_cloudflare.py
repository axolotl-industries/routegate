import httpx
import pytest
import respx

from routegate.cloudflare import CloudflareClient, CloudflareError, API_BASE

ZONE = "zone123"
TOKEN = "token-abc"
TUNNEL_TARGET = "abc-uuid.cfargotunnel.com"


@pytest.fixture
def client():
    return CloudflareClient(TOKEN, ZONE)


@respx.mock
async def test_list_cname_records_filtered_by_content(client):
    respx.get(f"{API_BASE}/zones/{ZONE}/dns_records").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "result": [
                    {
                        "id": "r1",
                        "name": "radarr.geoffflix.uk",
                        "content": TUNNEL_TARGET,
                        "proxied": True,
                    },
                    {
                        "id": "r2",
                        "name": "sonarr.geoffflix.uk",
                        "content": TUNNEL_TARGET,
                        "proxied": True,
                    },
                ],
                "result_info": {"page": 1, "total_pages": 1},
            },
        )
    )
    records = await client.list_cname_records(content_filter=TUNNEL_TARGET)
    assert [r.name for r in records] == [
        "radarr.geoffflix.uk",
        "sonarr.geoffflix.uk",
    ]
    assert all(r.content == TUNNEL_TARGET for r in records)
    await client.aclose()


@respx.mock
async def test_list_handles_pagination(client):
    route = respx.get(f"{API_BASE}/zones/{ZONE}/dns_records")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "r1", "name": "a.example.com", "content": "x", "proxied": True}
                ],
                "result_info": {"page": 1, "total_pages": 2},
            },
        ),
        httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"id": "r2", "name": "b.example.com", "content": "x", "proxied": True}
                ],
                "result_info": {"page": 2, "total_pages": 2},
            },
        ),
    ]
    records = await client.list_cname_records()
    assert [r.id for r in records] == ["r1", "r2"]
    await client.aclose()


@respx.mock
async def test_create_cname_returns_record(client):
    respx.post(f"{API_BASE}/zones/{ZONE}/dns_records").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "newid",
                    "name": "new.geoffflix.uk",
                    "content": TUNNEL_TARGET,
                    "proxied": True,
                },
            },
        )
    )
    record = await client.create_cname(
        name="new.geoffflix.uk", content=TUNNEL_TARGET, proxied=True
    )
    assert record.id == "newid"
    assert record.name == "new.geoffflix.uk"
    assert record.proxied is True
    await client.aclose()


@respx.mock
async def test_update_cname_uses_put(client):
    route = respx.put(f"{API_BASE}/zones/{ZONE}/dns_records/r1").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "r1",
                    "name": "x.geoffflix.uk",
                    "content": TUNNEL_TARGET,
                    "proxied": False,
                },
            },
        )
    )
    record = await client.update_cname(
        "r1", name="x.geoffflix.uk", content=TUNNEL_TARGET, proxied=False
    )
    assert route.called
    assert record.proxied is False
    await client.aclose()


@respx.mock
async def test_delete_cname_calls_delete(client):
    route = respx.delete(f"{API_BASE}/zones/{ZONE}/dns_records/r1").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"id": "r1"}}
        )
    )
    await client.delete_cname("r1")
    assert route.called
    await client.aclose()


@respx.mock
async def test_error_response_raises(client):
    respx.post(f"{API_BASE}/zones/{ZONE}/dns_records").mock(
        return_value=httpx.Response(
            400,
            json={
                "success": False,
                "errors": [{"code": 9999, "message": "bad request"}],
            },
        )
    )
    with pytest.raises(CloudflareError, match="bad request"):
        await client.create_cname(name="bad.example.com", content="x")
    await client.aclose()


@respx.mock
async def test_auth_header_set():
    route = respx.get(f"{API_BASE}/zones/{ZONE}/dns_records").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": [],
                "result_info": {"page": 1, "total_pages": 1},
            },
        )
    )
    client = CloudflareClient(TOKEN, ZONE)
    await client.list_cname_records()
    assert route.called
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    await client.aclose()
