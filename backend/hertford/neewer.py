"""Client for the Mac-side Neewer BLE→HTTP bridge.

The hertford container has no Bluetooth, so we delegate to a small bridge
service running on the user's Mac (or future Raspberry Pi). Configure with
the NEEWER_BRIDGE_URL env var (e.g. http://192.168.8.196:8765).

Bridge source lives in bridge/neewer/. Light ids registered there are what
this client passes as `light_id` here — they're stable so we can hardcode
the list of office lights server-side.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)


class NeewerError(Exception):
    pass


class NeewerBridge:
    """Thin HTTP client. Stateless — each method opens its own aiohttp session
    (the bridge sees us infrequently so keepalive isn't worth the complexity).
    """

    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def healthy(self) -> bool:
        """Quick liveness probe — used by the picker to decide whether to
        render the controls or a 'bridge offline' message."""
        if not self.configured:
            return False
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2)
            ) as session:
                async with session.get(f"{self.base_url}/") as r:
                    return r.status == 200
        except Exception:
            return False

    async def power(self, light_id: str, on: bool) -> None:
        await self._post(f"/lights/{light_id}/power", {"on": "true" if on else "false"})

    async def set_cct(self, light_id: str, brightness: int, cct: int) -> None:
        await self._post(
            f"/lights/{light_id}/cct",
            {"brightness": str(brightness), "cct": str(cct)},
        )

    async def set_color(
        self, light_id: str, hue: int, saturation: int, brightness: int
    ) -> None:
        await self._post(
            f"/lights/{light_id}/color",
            {
                "hue": str(hue),
                "saturation": str(saturation),
                "brightness": str(brightness),
            },
        )

    async def _post(self, path: str, data: dict[str, str]) -> None:
        if not self.configured:
            raise NeewerError("NEEWER_BRIDGE_URL not configured")
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                async with session.post(url, data=data) as r:
                    if r.status >= 400:
                        body = await r.text()
                        raise NeewerError(f"{r.status} {url}: {body[:200]}")
        except aiohttp.ClientError as e:
            raise NeewerError(f"bridge unreachable: {e}") from e
