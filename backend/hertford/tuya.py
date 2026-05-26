"""Tuya / SmartLife smart-bulb wrapper using tinytuya local-control.

Tuya devices speak a proprietary protocol over LAN. To control them
without a cloud round-trip we need three things per bulb:

- **device_id**: visible as "Virtual ID" in the SmartLife app's Device Info.
- **local_key**: 16-char per-device encryption key. NOT visible in the app.
  Extracted via a one-time `tinytuya wizard` flow against a free Tuya IoT
  developer account.
- **ip**: the bulb's LAN IP. `tinytuya scan` finds it.

Pass these via env vars per bulb (e.g. for the Lounge bulb):
    TUYA_LOUNGE_DEVICE_ID=bf27791cce57ddc95450lq
    TUYA_LOUNGE_LOCAL_KEY=...16 chars...
    TUYA_LOUNGE_IP=192.168.8.x
    TUYA_LOUNGE_VERSION=3.4   # optional, defaults to 3.4 for modern firmware

tinytuya is synchronous, so calls are wrapped in run_in_executor.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


class TuyaError(Exception):
    pass


@dataclass(frozen=True)
class TuyaBulbConfig:
    id: str
    label: str
    device_id: str
    local_key: str
    ip: str
    version: str = "3.4"

    @classmethod
    def from_env(cls, bulb_id: str, label: str, env_prefix: str) -> "TuyaBulbConfig | None":
        device_id = os.environ.get(f"{env_prefix}_DEVICE_ID")
        local_key = os.environ.get(f"{env_prefix}_LOCAL_KEY")
        ip = os.environ.get(f"{env_prefix}_IP")
        version = os.environ.get(f"{env_prefix}_VERSION", "3.4")
        if not (device_id and local_key and ip):
            return None
        return cls(
            id=bulb_id,
            label=label,
            device_id=device_id,
            local_key=local_key,
            ip=ip,
            version=version,
        )


class TuyaBulb:
    """Wrapper around tinytuya.BulbDevice for one bulb."""

    def __init__(self, config: TuyaBulbConfig) -> None:
        self.config = config

    def _device(self):
        import tinytuya  # local import — keeps the rest of the app importable
                         # even if tinytuya isn't installed.

        d = tinytuya.BulbDevice(
            self.config.device_id, self.config.ip, self.config.local_key
        )
        try:
            d.set_version(float(self.config.version))
        except Exception:
            pass
        d.set_socketRetryLimit(1)
        d.set_socketTimeout(3)
        return d

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args))
        except Exception as e:
            raise TuyaError(f"{self.config.id}: {e}") from e

    # ----------------------------------------------------------------- state

    async def status(self) -> dict:
        """Return {on, brightness, kelvin, error}. Best-effort parsing of
        Tuya's DPS dict; bulbs vary in which DPS keys they expose."""
        data = await self._run(self._status_sync)
        # data["dps"] is { "20": True, "22": 800, "23": 500, ... } typical mapping:
        #   20: on/off
        #   21: mode ("white" or "colour"/"colour")
        #   22: brightness (white mode) 10-1000
        #   23: colour temp (white mode) 0-1000 — Tuya's normalised range
        #   24: HSV colour string
        dps = (data or {}).get("dps") or {}
        on = dps.get("20")
        if on is None and dps:
            # some devices use string keys differently
            on = dps.get(20)
        bri = dps.get("22") or dps.get(22)
        try:
            bri_pct = round(int(bri) / 10) if bri else None
        except Exception:
            bri_pct = None
        kelvin = dps.get("23") or dps.get(23)
        try:
            # Tuya 0-1000 ↔ ~2700K-6500K typically. Linear approximation:
            k = round(2700 + (int(kelvin) / 1000) * (6500 - 2700)) if kelvin is not None else None
        except Exception:
            k = None
        return {
            "on": bool(on) if on is not None else None,
            "brightness": bri_pct,
            "kelvin": k,
            "raw_dps": dps,
        }

    def _status_sync(self):
        d = self._device()
        return d.status()

    # ----------------------------------------------------------------- writes

    async def power(self, on: bool) -> None:
        await self._run(self._power_sync, on)

    def _power_sync(self, on: bool):
        d = self._device()
        if on:
            return d.turn_on()
        return d.turn_off()

    async def set_brightness(self, brightness_pct: int) -> None:
        """0-100 percent."""
        await self._run(self._set_brightness_sync, brightness_pct)

    def _set_brightness_sync(self, pct: int):
        d = self._device()
        # tinytuya brightness takes 10-1000; map from 0-100.
        v = max(10, min(1000, round(pct * 10)))
        return d.set_brightness(v)

    async def set_kelvin(self, kelvin: int) -> None:
        await self._run(self._set_kelvin_sync, kelvin)

    def _set_kelvin_sync(self, kelvin: int):
        d = self._device()
        # Map Kelvin 2700-6500 → Tuya 0-1000.
        k = max(2700, min(6500, int(kelvin)))
        v = round((k - 2700) / (6500 - 2700) * 1000)
        return d.set_colourtemp(v)
