"""Neewer BLE → HTTP bridge.

Runs on the Mac (or any host with a Bluetooth radio). Exposes a small HTTP
API so the Hertford container — which has no Bluetooth itself — can control
nearby Neewer lights.

Quickstart:
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python neewer_bridge.py

API:
    POST /discover                       → BLE scan, returns devices found
    POST /lights/register                → save a discovered light under an id
    GET  /lights                         → list registered lights
    POST /lights/{id}/power              → form: on=true|false
    POST /lights/{id}/cct                → form: brightness=0-100, cct=32-56
    POST /lights/{id}/color              → form: hue=0-360, saturation=0-100, brightness=0-100
    POST /lights/{id}/forget             → remove

State (registered lights with MAC addresses) is persisted at
    ~/.hertford-neewer.json

Note: The Neewer BLE protocol implemented here is the common "RGB" family
(used by RGB660, CB60 RGB, RGB190, etc.) — write to characteristic
69400002-... with a small command frame ending in an XOR-or-sum checksum.
If a command doesn't work on your model, compare against the up-to-date
protocol notes at https://github.com/taburineagle/NeewerLite-Python and
tweak `cmd_*` below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice


class BleakNotFound(Exception):
    """Raised when a stored address can't be located via a fresh BLE scan."""
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("neewer-bridge")

# Neewer BLE service + characteristics — common across the RGB family.
SERVICE_UUID = "69400001-b5a3-f393-e0a9-e50e24dcca99"
WRITE_CHAR_UUID = "69400002-b5a3-f393-e0a9-e50e24dcca99"
NOTIFY_CHAR_UUID = "69400003-b5a3-f393-e0a9-e50e24dcca99"

CONFIG_PATH = Path.home() / ".hertford-neewer.json"
PORT = int(os.environ.get("PORT", "8765"))


# ====================================================================== protocol


def _checksum(payload: bytes) -> int:
    """Neewer frames end with `sum(bytes) & 0xFF`."""
    return sum(payload) & 0xFF


def cmd_power(on: bool) -> bytes:
    payload = bytes([0x78, 0x81, 0x01, 0x01 if on else 0x02])
    return payload + bytes([_checksum(payload)])


def cmd_cct(brightness: int, cct: int) -> bytes:
    """CCT mode (white temp + brightness).

    brightness: 0-100 (%)
    cct: 32-56 in Neewer scale ≈ 3200K-5600K (some panels go wider — adjust if so).
    """
    b = max(0, min(100, int(brightness)))
    c = max(32, min(56, int(cct)))
    payload = bytes([0x78, 0x87, 0x02, b, c])
    return payload + bytes([_checksum(payload)])


def cmd_hsi(hue: int, saturation: int, brightness: int) -> bytes:
    """HSI (full colour) mode.

    hue: 0-360
    saturation: 0-100
    brightness: 0-100
    """
    h = max(0, min(360, int(hue)))
    s = max(0, min(100, int(saturation)))
    b = max(0, min(100, int(brightness)))
    payload = bytes(
        [0x78, 0x86, 0x04, h & 0xFF, (h >> 8) & 0xFF, s, b]
    )
    return payload + bytes([_checksum(payload)])


# ====================================================================== device


class NeewerLight:
    def __init__(self, address: str, name: str = "") -> None:
        self.address = address
        self.name = name
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> BleakClient:
        if self._client is not None and self._client.is_connected:
            return self._client
        # macOS Core Bluetooth requires the peripheral to have been seen in a
        # recent scan; opening BleakClient straight from a stored UUID often
        # fails with "Device with address ... was not found". Do a short
        # rediscovery scan first to refresh the peripheral reference.
        log.info("rediscovering %s (%s)…", self.address, self.name)
        device = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        if device is None:
            raise BleakNotFound(
                f"could not find {self.address} via BLE scan — "
                "is the light on and in range?"
            )
        log.info("connecting to %s (%s)…", self.address, self.name)
        client = BleakClient(device, timeout=10)
        await client.connect()
        self._client = client
        return client

    async def send(self, payload: bytes) -> None:
        async with self._lock:
            try:
                client = await self._ensure()
                await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)
            except Exception as e:
                # Many BLE errors are transient — drop the cached client and retry once.
                log.warning("write to %s failed (%s) — reconnecting", self.address, e)
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                self._client = None
                client = await self._ensure()
                await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=False)

    async def power(self, on: bool) -> None:
        await self.send(cmd_power(on))

    async def cct(self, brightness: int, cct: int) -> None:
        await self.send(cmd_cct(brightness, cct))

    async def color(self, hue: int, saturation: int, brightness: int) -> None:
        await self.send(cmd_hsi(hue, saturation, brightness))

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None


# ====================================================================== state


LIGHTS: dict[str, NeewerLight] = {}


def _load_state() -> None:
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        log.warning("config read failed: %s", e)
        return
    for entry in data.get("lights", []):
        lid = entry.get("id")
        addr = entry.get("address")
        if lid and addr:
            LIGHTS[lid] = NeewerLight(address=addr, name=entry.get("name", ""))
    log.info("loaded %d light(s) from %s", len(LIGHTS), CONFIG_PATH)


def _save_state() -> None:
    payload = {
        "lights": [
            {"id": lid, "address": l.address, "name": l.name}
            for lid, l in LIGHTS.items()
        ]
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2))


# ====================================================================== HTTP


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_state()
    yield
    for light in LIGHTS.values():
        await light.aclose()


app = FastAPI(title="Neewer Bridge", lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "lights": [
            {"id": lid, "address": l.address, "name": l.name}
            for lid, l in LIGHTS.items()
        ]
    }


@app.post("/discover")
async def discover():
    """BLE scan for Neewer devices.

    Returns devices whose advertised name starts with NEEWER (case-insensitive).
    Scans for 10s — some Neewer panels advertise at a low duty cycle and
    can be missed by shorter scans.
    """
    log.info("scanning for ~10s…")
    devices: list[BLEDevice] = await BleakScanner.discover(timeout=10.0)
    found: list[dict[str, Any]] = []
    for d in devices:
        name = d.name or ""
        if name.upper().startswith("NEEWER") or "NEEWER" in name.upper():
            found.append({"address": d.address, "name": name})
    return {"devices": found}


@app.post("/lights/register")
async def register(
    id: str = Form(...), address: str = Form(...), name: str = Form("")
):
    LIGHTS[id] = NeewerLight(address=address, name=name)
    _save_state()
    return {"ok": True, "id": id}


@app.post("/lights/{light_id}/forget")
async def forget(light_id: str):
    if light_id not in LIGHTS:
        raise HTTPException(404)
    await LIGHTS[light_id].aclose()
    del LIGHTS[light_id]
    _save_state()
    return {"ok": True}


def _light(light_id: str) -> NeewerLight:
    light = LIGHTS.get(light_id)
    if light is None:
        raise HTTPException(404, detail=f"no light registered as {light_id!r}")
    return light


@app.post("/lights/{light_id}/power")
async def power(light_id: str, on: bool = Form(...)):
    try:
        await _light(light_id).power(on)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True}


@app.post("/lights/{light_id}/cct")
async def set_cct(
    light_id: str, brightness: int = Form(...), cct: int = Form(...)
):
    try:
        await _light(light_id).cct(brightness, cct)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True}


@app.post("/lights/{light_id}/color")
async def set_color(
    light_id: str,
    hue: int = Form(...),
    saturation: int = Form(...),
    brightness: int = Form(...),
):
    try:
        await _light(light_id).color(hue, saturation, brightness)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return {"ok": True}


# ====================================================================== main


def main():
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    log.info("starting neewer bridge on %s:%s", host, PORT)
    uvicorn.run(app, host=host, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
