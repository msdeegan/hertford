"""Elgato Key Light over its local HTTP API.

API surface (port 9123):
  GET  /elgato/lights            → current state
  PUT  /elgato/lights            → set state
  GET  /elgato/accessory-info    → friendly name etc.

State shape (single light):
  {"on": 0|1, "brightness": 0-100, "temperature": 143-344}

`temperature` is the bulb's internal value, inversely proportional to
Kelvin (143 ≈ 6993K cool, 344 ≈ 2907K warm). We expose Kelvin externally
because it's the intuitive unit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

PORT = 9123
KELVIN_MIN = 2900  # warmest
KELVIN_MAX = 7000  # coolest
TEMP_MIN = 143  # raw value at KELVIN_MAX
TEMP_MAX = 344  # raw value at KELVIN_MIN


def kelvin_to_temp(kelvin: int) -> int:
    """Convert Kelvin (~2900-7000) to Elgato's raw 143-344 scale."""
    k = max(KELVIN_MIN, min(KELVIN_MAX, kelvin))
    return max(TEMP_MIN, min(TEMP_MAX, round(1_000_000 / k)))


def temp_to_kelvin(temp: int) -> int:
    if not temp:
        return 0
    return round(1_000_000 / temp)


class KeyLightError(Exception):
    pass


class KeyLight:
    """Per-light wrapper with optional persistent host on disk."""

    def __init__(
        self,
        host: str | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir else None
        self._host_override = host
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._host_path = self.state_dir / "host.txt"
        else:
            self._host_path = None

    # ------------------------------------------------------------ persistence

    @property
    def host(self) -> str | None:
        if self._host_override:
            return self._host_override
        if self._host_path and self._host_path.is_file():
            return self._host_path.read_text().strip() or None
        return None

    def set_host(self, host: str | None) -> None:
        if self._host_override:
            self._host_override = host
            return
        if self._host_path is None:
            raise KeyLightError("no state_dir; cannot persist host")
        if host:
            self._host_path.write_text(host.strip() + "\n")
        elif self._host_path.exists():
            self._host_path.unlink()

    def forget(self) -> None:
        self.set_host(None)

    # ------------------------------------------------------------ http

    async def state(self) -> dict:
        """Return {on, brightness (0-100), temperature (raw), kelvin}."""
        if not self.host:
            raise KeyLightError("no key light configured")
        url = f"http://{self.host}:{PORT}/elgato/lights"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception as e:
            raise KeyLightError(f"GET {url}: {e}") from e
        light = (data.get("lights") or [{}])[0]
        return {
            "on": bool(light.get("on")),
            "brightness": int(light.get("brightness") or 0),
            "temperature": int(light.get("temperature") or 0),
            "kelvin": temp_to_kelvin(int(light.get("temperature") or 0)),
        }

    async def set(
        self,
        *,
        on: bool | None = None,
        brightness: int | None = None,
        kelvin: int | None = None,
        temperature: int | None = None,
    ) -> None:
        """Set one or more properties. Pass either `kelvin` (preferred) OR
        `temperature` (raw 143-344)."""
        if not self.host:
            raise KeyLightError("no key light configured")
        body: dict = {}
        if on is not None:
            body["on"] = 1 if on else 0
        if brightness is not None:
            body["brightness"] = max(0, min(100, int(brightness)))
        if kelvin is not None:
            body["temperature"] = kelvin_to_temp(int(kelvin))
        elif temperature is not None:
            body["temperature"] = max(TEMP_MIN, min(TEMP_MAX, int(temperature)))
        if not body:
            return
        url = f"http://{self.host}:{PORT}/elgato/lights"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.put(url, json={"lights": [body]}) as resp:
                    resp.raise_for_status()
        except Exception as e:
            raise KeyLightError(f"PUT {url} body={body}: {e}") from e

    async def info(self) -> dict:
        """Friendly device info (name, model, firmware)."""
        if not self.host:
            raise KeyLightError("no key light configured")
        url = f"http://{self.host}:{PORT}/elgato/accessory-info"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            raise KeyLightError(f"GET {url}: {e}") from e


# ============================================================ discovery


async def discover_lan(
    subnet: str = "192.168.8.0/24",
    timeout: float = 0.4,
    concurrency: int = 50,
) -> list[dict]:
    """Find Elgato Key Lights on the LAN.

    Two-step: TCP-probe each host for port 9123 open, then hit
    /elgato/accessory-info on each candidate to confirm it's an Elgato
    device and pick up its display name. Returns [{host, name}, ...].
    """
    net = ipaddress.ip_network(subnet, strict=False)
    sem = asyncio.Semaphore(concurrency)

    async def probe(ip: str) -> str | None:
        async with sem:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(ip, PORT), timeout=timeout
                )
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                return ip
            except (asyncio.TimeoutError, OSError):
                return None

    hosts = [str(ip) for ip in net.hosts()]
    candidates = [r for r in await asyncio.gather(*[probe(h) for h in hosts]) if r]

    results: list[dict] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
        for ip in candidates:
            try:
                async with session.get(
                    f"http://{ip}:{PORT}/elgato/accessory-info"
                ) as resp:
                    info = await resp.json()
                # Elgato responses have productName and displayName fields.
                if "productName" not in info and "displayName" not in info:
                    continue
                results.append(
                    {
                        "host": ip,
                        "name": info.get("displayName")
                        or info.get("productName")
                        or ip,
                        "product": info.get("productName"),
                    }
                )
            except Exception as e:
                log.debug("identify %s failed: %s", ip, e)
                continue
    return results
