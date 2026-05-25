"""Apple TV remote via pyatv (Companion protocol).

Same shape as gtv.py: discover → pair (PIN) → save credentials →
send key. Lives as a singleton on app.state.atv.

State on disk (under `state_dir`):
- `credentials.txt`: opaque per-protocol credential string from pyatv.
- `host.txt`: last-known TV IP.
- `name.txt`: friendly name (display only).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path

import pyatv
from pyatv.const import Protocol

log = logging.getLogger(__name__)

CLIENT_NAME = "Hertford Road"
AIRPLAY_PORT = 7000  # TCP probe target for LAN discovery


class ATVError(Exception):
    pass


class AppleTV:
    """Persistent-connection Apple TV remote wrapper."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.creds_path = self.state_dir / "credentials.txt"
        self.host_path = self.state_dir / "host.txt"
        self.name_path = self.state_dir / "name.txt"
        self._pairing = None
        self._pairing_conf = None
        self._device = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ persistence

    @property
    def paired(self) -> bool:
        return self.creds_path.is_file() and self.host_path.is_file()

    @property
    def host(self) -> str | None:
        return self.host_path.read_text().strip() if self.host_path.is_file() else None

    @property
    def name(self) -> str | None:
        return self.name_path.read_text().strip() if self.name_path.is_file() else None

    def forget(self) -> None:
        for p in (self.creds_path, self.host_path, self.name_path):
            if p.exists():
                p.unlink()
        self._device_close()

    def _device_close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
        self._device = None

    # ------------------------------------------------------------ pairing

    async def start_pairing(self, host: str) -> None:
        loop = asyncio.get_running_loop()
        atvs = await pyatv.scan(loop, hosts=[host], timeout=5)
        if not atvs:
            raise ATVError(f"no Apple TV responded at {host}")
        conf = atvs[0]
        try:
            self._pairing = await pyatv.pair(
                conf, Protocol.Companion, loop, name=CLIENT_NAME
            )
            await self._pairing.begin()
        except Exception as e:
            raise ATVError(f"pair start failed: {e}") from e
        self._pairing_conf = conf
        log.info("atv pair started with %s (%s)", host, conf.name)

    async def finish_pairing(self, pin: str) -> None:
        if self._pairing is None:
            raise ATVError("no pairing in progress — call start_pairing first")
        try:
            self._pairing.pin(pin.strip())
            await self._pairing.finish()
        except Exception as e:
            raise ATVError(f"pair finish failed: {e}") from e
        if not self._pairing.has_paired:
            raise ATVError("pair did not complete — wrong PIN?")
        service = self._pairing.service
        creds = service.credentials if service else None
        if not creds:
            raise ATVError("pair completed but no credentials returned")
        self.creds_path.write_text(creds)
        self.host_path.write_text(self._pairing_conf.address.compressed + "\n")
        self.name_path.write_text((self._pairing_conf.name or "") + "\n")
        try:
            await self._pairing.close()
        except Exception:
            pass
        self._pairing = None
        self._pairing_conf = None
        self._device_close()
        log.info("atv pair complete")

    # ------------------------------------------------------------ connection

    async def _ensure_connected(self):
        if not self.paired:
            raise ATVError("not paired — visit /atv to pair")
        if self._device is not None:
            return self._device
        loop = asyncio.get_running_loop()
        atvs = await pyatv.scan(loop, hosts=[self.host], timeout=5)
        if not atvs:
            raise ATVError(f"can't reach Apple TV at {self.host}")
        conf = atvs[0]
        conf.set_credentials(Protocol.Companion, self.creds_path.read_text().strip())
        try:
            self._device = await pyatv.connect(conf, loop)
        except Exception as e:
            raise ATVError(f"connect failed: {e}") from e
        return self._device

    async def send_key(self, key_name: str) -> None:
        async with self._lock:
            try:
                device = await self._ensure_connected()
                method = getattr(device.remote_control, key_name)
                await method()
            except Exception as e:
                # one reconnect attempt
                log.warning("atv key %s failed (%s) — reconnecting", key_name, e)
                self._device_close()
                device = await self._ensure_connected()
                method = getattr(device.remote_control, key_name)
                await method()

    async def shutdown(self) -> None:
        if self._pairing is not None:
            try:
                await self._pairing.close()
            except Exception:
                pass
            self._pairing = None
        self._device_close()


# ============================================================ discovery


async def discover_lan(
    subnet: str = "192.168.8.0/24",
    port: int = AIRPLAY_PORT,
    timeout: float = 0.4,
    concurrency: int = 50,
) -> list[dict]:
    """Find Apple TVs on the LAN.

    Two-step: TCP-probe each host for port 7000 (AirPlay) open, then ask
    pyatv to identify each candidate via unicast mDNS so we can show a
    friendly name in the picker. Returns [{host, name}, ...].
    """
    net = ipaddress.ip_network(subnet, strict=False)
    sem = asyncio.Semaphore(concurrency)

    async def probe(ip_str: str) -> str | None:
        async with sem:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(ip_str, port), timeout=timeout
                )
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
                return ip_str
            except (asyncio.TimeoutError, OSError):
                return None

    hosts = [str(ip) for ip in net.hosts()]
    candidates = [r for r in await asyncio.gather(*[probe(h) for h in hosts]) if r]

    # Enrich with pyatv name via unicast mDNS (limit concurrent scans to be polite)
    loop = asyncio.get_running_loop()
    sem2 = asyncio.Semaphore(8)

    async def identify(ip: str) -> dict | None:
        async with sem2:
            try:
                atvs = await pyatv.scan(loop, hosts=[ip], timeout=3)
                if not atvs:
                    return None
                return {"host": ip, "name": atvs[0].name or ip}
            except Exception:
                return None

    results = [r for r in await asyncio.gather(*[identify(c) for c in candidates]) if r]
    return results
