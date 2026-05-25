"""Async wrapper around plugp100 for controlling Tapo smart devices on the LAN.

Confirmed working against:
- Tapo P100 plug ("TV System") at 192.168.8.109

Credentials are TP-Link cloud account email/password. The plugp100 library
authenticates locally — no traffic actually leaves the LAN — but the device
must have been claimed by that account at some point via the Tapo app.

Recommended: use a dedicated TP-Link account that owns only the house's
automation devices, not your main personal account.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from plugp100.common.credentials import AuthCredential
from plugp100.new.device_factory import DeviceConnectConfiguration, connect

log = logging.getLogger(__name__)


class TapoError(Exception):
    """Raised for Tapo connection or command failures."""


@dataclass
class TapoConfig:
    host: str
    email: str
    password: str

    @classmethod
    def from_env(cls, host: str | None = None) -> "TapoConfig":
        try:
            email = os.environ["TAPO_EMAIL"]
            password = os.environ["TAPO_PASSWORD"]
        except KeyError as e:
            raise TapoError(
                "set TAPO_EMAIL and TAPO_PASSWORD env vars (and optionally TAPO_HOST)"
            ) from e
        host = host or os.environ.get("TAPO_HOST", "192.168.8.109")
        return cls(host=host, email=email, password=password)


class TapoPlug:
    """Thin async wrapper around plugp100 for a single Tapo device.

    plugp100's session is short-lived (it negotiates a fresh handshake each
    `connect()`), so we open and close per call rather than holding a long
    connection. That trades a little latency for resilience — perfect for the
    handful of plug operations the app will do.

    For the future light bulbs, the same wrapper should work since plugp100's
    `connect()` returns a device-typed object with brightness/colour methods.
    """

    def __init__(self, config: TapoConfig) -> None:
        self._config = config

    async def _open(self):
        creds = AuthCredential(self._config.email, self._config.password)
        cfg = DeviceConnectConfiguration(host=self._config.host, credentials=creds)
        try:
            return await connect(cfg)
        except Exception as e:
            raise TapoError(f"failed to connect to {self._config.host}: {e}") from e

    async def info(self) -> dict[str, Any]:
        device = await self._open()
        await device.update()
        di = device.device_info
        # `is_on` is a property on the device object, sourced from the
        # OnOffComponent after update() — not present on DeviceInfo itself.
        return {
            "host": self._config.host,
            "model": getattr(di, "model", None),
            "nickname": getattr(di, "nickname", None),
            "device_id": getattr(di, "device_id", None),
            "on": bool(device.is_on),
            "overheated": getattr(di, "overheated", None),
            "signal_level": getattr(di, "signal_level", None),
            "rssi": getattr(di, "rssi", None),
        }

    async def is_on(self) -> bool:
        device = await self._open()
        await device.update()
        return bool(device.is_on)

    async def on(self) -> None:
        device = await self._open()
        try:
            # update() lazily initializes the OnOffComponent that turn_on
            # delegates to — without it turn_on silently AttributeErrors.
            await device.update()
            await device.turn_on()
        except Exception as e:
            raise TapoError(f"turn_on failed: {e}") from e

    async def off(self) -> None:
        device = await self._open()
        try:
            await device.update()
            await device.turn_off()
        except Exception as e:
            raise TapoError(f"turn_off failed: {e}") from e


# ---------------------------------------------------------------------------- CLI


def _build_argparser():
    import argparse

    p = argparse.ArgumentParser(prog="python -m hertford.tapo")
    p.add_argument("--host", default=None, help="device IP (default: $TAPO_HOST or 192.168.8.109)")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show device info")
    sub.add_parser("status", help="print on/off state")
    sub.add_parser("on", help="turn the plug on")
    sub.add_parser("off", help="turn the plug off")
    sub.add_parser("toggle", help="flip the current state")

    return p


async def _amain(args) -> int:
    cfg = TapoConfig.from_env(args.host)
    plug = TapoPlug(cfg)

    if args.cmd == "info":
        info = await plug.info()
        for k, v in info.items():
            print(f"{k:>14}: {v}")
    elif args.cmd == "status":
        print("on" if await plug.is_on() else "off")
    elif args.cmd == "on":
        await plug.on()
        print("ok")
    elif args.cmd == "off":
        await plug.off()
        print("ok")
    elif args.cmd == "toggle":
        if await plug.is_on():
            await plug.off()
            print("off")
        else:
            await plug.on()
            print("on")
    return 0


def main() -> int:
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_amain(args))
    except TapoError as e:
        print(f"error: {e}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
