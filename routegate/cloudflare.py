"""Minimal Cloudflare DNS API client for tunnel CNAME records.

Only implements what routegate needs: list/create/update/delete CNAME records
that point at our tunnel target. Uses httpx directly rather than the heavy
official SDK.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


@dataclass(frozen=True)
class DnsRecord:
    id: str
    name: str       # full hostname e.g. radarr.geoffflix.uk
    content: str    # CNAME target
    proxied: bool


class CloudflareClient:
    def __init__(
        self,
        api_token: str,
        zone_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self._zone_id = zone_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "CloudflareClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def list_cname_records(
        self, *, content_filter: str | None = None
    ) -> list[DnsRecord]:
        """List CNAME records in the zone, optionally filtered by exact content match."""
        params: dict = {"type": "CNAME", "per_page": 1000}
        if content_filter:
            params["content"] = content_filter
        records: list[DnsRecord] = []
        page = 1
        while True:
            params["page"] = page
            r = await self._client.get(
                f"/zones/{self._zone_id}/dns_records", params=params
            )
            payload = self._unwrap(r)
            for item in payload["result"]:
                records.append(
                    DnsRecord(
                        id=item["id"],
                        name=item["name"],
                        content=item["content"],
                        proxied=item.get("proxied", False),
                    )
                )
            info = payload.get("result_info", {})
            if page >= info.get("total_pages", 1):
                break
            page += 1
        return records

    async def create_cname(
        self, *, name: str, content: str, proxied: bool = True
    ) -> DnsRecord:
        r = await self._client.post(
            f"/zones/{self._zone_id}/dns_records",
            json={
                "type": "CNAME",
                "name": name,
                "content": content,
                "proxied": proxied,
                "ttl": 1,
            },
        )
        item = self._unwrap(r)["result"]
        return DnsRecord(
            id=item["id"],
            name=item["name"],
            content=item["content"],
            proxied=item.get("proxied", False),
        )

    async def update_cname(
        self,
        record_id: str,
        *,
        name: str,
        content: str,
        proxied: bool = True,
    ) -> DnsRecord:
        r = await self._client.put(
            f"/zones/{self._zone_id}/dns_records/{record_id}",
            json={
                "type": "CNAME",
                "name": name,
                "content": content,
                "proxied": proxied,
                "ttl": 1,
            },
        )
        item = self._unwrap(r)["result"]
        return DnsRecord(
            id=item["id"],
            name=item["name"],
            content=item["content"],
            proxied=item.get("proxied", False),
        )

    async def delete_cname(self, record_id: str) -> None:
        r = await self._client.delete(
            f"/zones/{self._zone_id}/dns_records/{record_id}"
        )
        self._unwrap(r)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as e:
            raise CloudflareError(
                f"non-JSON response (status={response.status_code}): {response.text[:200]}"
            ) from e
        if response.status_code >= 400 or not payload.get("success", False):
            errors = payload.get("errors") or [{"message": response.text}]
            raise CloudflareError(
                f"cloudflare api error (status={response.status_code}): {errors}"
            )
        return payload
