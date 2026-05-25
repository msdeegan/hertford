"""Async client for the Blustream C66CS 6x6 HDBaseT matrix over telnet.

The matrix listens on TCP port 23. On connect it sends a one-time greeting
`Please Input Your Command :\\r\\n` then accepts commands terminated with
CR/LF. After each command it streams the response then goes silent — there is
no closing prompt or delimiter, so we frame replies by reading until a short
idle period.

Reference: C66CS.txt in the repo root for the full command list.

Confirmed against firmware V1.0.0d:
- `FWVERSION` returns nothing (listed in help but not implemented).
- `OUTSTA` returns a banner + system status + an "Output FromIn ..." table.
- `STATUS` / `INSTA` return banner + system status + input table.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

log = logging.getLogger(__name__)

DEFAULT_PORT = 23
NUM_OUTPUTS = 6
NUM_INPUTS = 6
# Treat the response as complete after this much silence following any bytes.
IDLE_GAP = 0.3

# Commands we refuse to send. GUEST ON disconnects every active telnet session
# (per C66CS.txt line 29) — sending it would kick the API off the matrix.
FORBIDDEN_PATTERNS = [
    re.compile(r"\bGUEST\s+ON\b", re.IGNORECASE),
    re.compile(r"\bRESET\b", re.IGNORECASE),
    re.compile(r"\bNET\s+RB\b", re.IGNORECASE),
]


class MatrixError(Exception):
    """Raised for matrix protocol or connection failures."""


class MatrixClient:
    """Persistent async telnet client for the Blustream matrix.

    Designed to be used as a long-lived singleton inside the API process: one
    connection per client instance, serialized command access via an asyncio
    lock. Reconnects transparently if the socket drops.

    Also works fine for one-shot CLI use — connect, run a few commands, close.

    Usage:
        async with MatrixClient("192.168.8.241") as m:
            print(await m.status())
            await m.route(output=1, input_=3)
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = 3.0,
        command_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "MatrixClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._writer is not None:
            return
        log.debug("connecting to matrix %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
            # Drain the greeting ("Please Input Your Command :\r\n").
            await self._read_idle()
        except (OSError, asyncio.TimeoutError) as e:
            self._reader = self._writer = None
            raise MatrixError(f"failed to connect to {self.host}:{self.port}: {e}") from e

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    # -------------------------------------------------------------------- io core

    async def _read_idle(self, idle_gap: float = IDLE_GAP) -> bytes:
        """Read bytes until the stream has been silent for `idle_gap` seconds.

        Bounded by `command_timeout` overall.
        """
        assert self._reader is not None
        chunks: list[bytes] = []
        deadline = asyncio.get_event_loop().time() + self.command_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            wait = idle_gap if chunks else min(self.command_timeout, remaining)
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=wait)
            except asyncio.TimeoutError:
                if chunks:
                    break  # idle gap reached — response is complete
                raise MatrixError("timed out waiting for matrix to respond")
            if not chunk:
                # peer closed
                if chunks:
                    break
                raise MatrixError("matrix closed connection unexpectedly")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _send_raw(self, command: str) -> str:
        """Send one command, return the response body."""
        if self._writer is None:
            await self.connect()
        assert self._writer is not None
        cmd = command.strip()
        log.debug("→ %s", cmd)
        self._writer.write((cmd + "\r\n").encode("ascii"))
        await self._writer.drain()
        raw = await self._read_idle()
        text = raw.decode("ascii", errors="replace")
        # Some commands echo the command back as the first line — strip if so.
        lines = text.splitlines()
        if lines and lines[0].strip().upper() == cmd.upper():
            lines = lines[1:]
        result = "\n".join(lines).strip()
        log.debug("← (%d bytes) %s", len(raw), result[:200])
        return result

    async def send(self, command: str) -> str:
        """Send a raw command. Refuses anything in FORBIDDEN_PATTERNS.

        Reconnects once on socket error before giving up.
        """
        for pat in FORBIDDEN_PATTERNS:
            if pat.search(command):
                raise MatrixError(f"refusing dangerous command: {command!r}")
        async with self._lock:
            try:
                return await self._send_raw(command)
            except MatrixError:
                # Reconnect and try once more.
                await self.close()
                await self.connect()
                return await self._send_raw(command)

    # ------------------------------------------------------------------- commands

    async def fw_version(self) -> str:
        """Parse FW version from the OUTSTA banner (the standalone FWVERSION
        command is listed in help but returns nothing on firmware V1.0.0d)."""
        body = await self.send("OUTSTA")
        for line in body.splitlines():
            m = re.search(r"FW Version:\s*(\S+)", line)
            if m:
                return m.group(1)
        return "unknown"

    async def status(self) -> dict[int, int]:
        """Return {output: input} routing table parsed from OUTSTA.

        The OUTSTA response is a multi-line dump: banner, system status, then
        a table that includes the routing in columns 1 and 2. We locate the
        table by its header line and parse the following rows until we hit a
        blank line. Output/input numbers are 1-based.
        """
        body = await self.send("OUTSTA")
        result: dict[int, int] = {}
        in_table = False
        for line in body.splitlines():
            if not in_table:
                if re.match(r"\s*Output\s+FromIn\b", line):
                    in_table = True
                continue
            if not line.strip():
                break
            m = re.match(r"\s*(\d{1,2})\s+(\d{1,2})\b", line)
            if not m:
                continue
            out, inp = int(m.group(1)), int(m.group(2))
            if 1 <= out <= NUM_OUTPUTS:
                result[out] = inp
        if not result:
            raise MatrixError(f"could not parse OUTSTA response: {body!r}")
        return result

    async def route(self, output: int, input_: int) -> str:
        """Route output (1-6, or 0 for all) to input (1-6)."""
        _check_output(output, allow_all=True)
        _check_input(input_)
        return await self.send(f"OUT{output:02d} FR {input_:02d}")

    async def cec_power(self, output: int, on: bool) -> str:
        """CEC power on/off. output=0 means all outputs."""
        _check_output(output, allow_all=True)
        verb = "PON" if on else "POFF"
        return await self.send(f"OUT{output:02d} CEC {verb}")

    async def preset_save(self, slot: int) -> str:
        _check_preset(slot)
        return await self.send(f"PRESET {slot:02d} SAVE")

    async def preset_apply(self, slot: int) -> str:
        _check_preset(slot)
        return await self.send(f"PRESET {slot:02d} APPLY")


def _check_output(n: int, *, allow_all: bool) -> None:
    lo = 0 if allow_all else 1
    if not (lo <= n <= NUM_OUTPUTS):
        raise ValueError(f"output {n} out of range [{lo}, {NUM_OUTPUTS}]")


def _check_input(n: int) -> None:
    if not (1 <= n <= NUM_INPUTS):
        raise ValueError(f"input {n} out of range [1, {NUM_INPUTS}]")


def _check_preset(n: int) -> None:
    if not (1 <= n <= 9):
        raise ValueError(f"preset {n} out of range [1, 9]")


@asynccontextmanager
async def matrix(host: str, port: int = DEFAULT_PORT) -> AsyncIterator[MatrixClient]:
    """Convenience context manager: `async with matrix(host) as m: ...`"""
    client = MatrixClient(host, port)
    try:
        await client.connect()
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------- CLI


def _build_argparser():
    import argparse

    p = argparse.ArgumentParser(prog="python -m hertford.matrix")
    p.add_argument("--host", default="192.168.8.241", help="matrix IP (default: %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show current routing table (OUTSTA)")
    sub.add_parser("fw", help="show firmware version")

    r = sub.add_parser("route", help="route an output to an input")
    r.add_argument("output", type=int, help="output number 1-6 (0 = all)")
    r.add_argument("input", type=int, help="input number 1-6")

    on = sub.add_parser("tv-on", help="CEC power on")
    on.add_argument("output", type=int, help="output number 1-6 (0 = all)")
    off = sub.add_parser("tv-off", help="CEC power off")
    off.add_argument("output", type=int, help="output number 1-6 (0 = all)")

    raw = sub.add_parser("raw", help="send a raw command (dangerous ones are still blocked)")
    raw.add_argument("command", help='e.g. "OUTSTA"')

    return p


async def _amain(args) -> int:
    async with matrix(args.host, args.port) as m:
        if args.cmd == "status":
            table = await m.status()
            print(f"{'Output':<8}{'FromIn':<8}")
            for out in sorted(table):
                print(f"{out:<8}{table[out]:<8}")
        elif args.cmd == "fw":
            print(await m.fw_version())
        elif args.cmd == "route":
            print(await m.route(args.output, args.input))
        elif args.cmd == "tv-on":
            print(await m.cec_power(args.output, True))
        elif args.cmd == "tv-off":
            print(await m.cec_power(args.output, False))
        elif args.cmd == "raw":
            print(await m.send(args.command))
    return 0


def main() -> int:
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(_amain(args))
    except MatrixError as e:
        print(f"error: {e}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
