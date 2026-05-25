"""Load room/source mappings from YAML and runtime settings from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Room:
    id: str
    label: str
    output: int
    visibility: str  # "guest" or "admin"


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    input: int


@dataclass(frozen=True)
class Config:
    rooms: tuple[Room, ...]
    sources: tuple[Source, ...]
    matrix_host: str
    matrix_port: int
    tapo_host: str | None
    tapo_email: str | None
    tapo_password: str | None
    guest_password: str | None
    wifi_ssid: str | None
    wifi_password: str | None
    wifi_auth: str  # WPA / WEP / nopass

    def room(self, room_id: str) -> Room:
        for r in self.rooms:
            if r.id == room_id:
                return r
        raise KeyError(f"unknown room: {room_id}")

    def source(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(f"unknown source: {source_id}")

    def room_by_output(self, output: int) -> Room | None:
        for r in self.rooms:
            if r.output == output:
                return r
        return None

    def source_by_input(self, input_: int) -> Source | None:
        for s in self.sources:
            if s.input == input_:
                return s
        return None

    @property
    def guest_rooms(self) -> tuple[Room, ...]:
        return tuple(r for r in self.rooms if r.visibility == "guest")


DEFAULT_CONFIG_PATHS = [
    "/config/rooms.yaml",
    "config/rooms.yaml",
    "../config/rooms.yaml",
]


def load_config(path: str | None = None) -> Config:
    yaml_path = _find_config_file(path)
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    rooms = tuple(Room(**r) for r in data["rooms"])
    sources = tuple(Source(**s) for s in data["sources"])

    return Config(
        rooms=rooms,
        sources=sources,
        matrix_host=os.environ.get("MATRIX_HOST", "192.168.8.241"),
        matrix_port=int(os.environ.get("MATRIX_PORT", "23")),
        tapo_host=os.environ.get("TAPO_HOST") or None,
        tapo_email=os.environ.get("TAPO_EMAIL") or None,
        tapo_password=os.environ.get("TAPO_PASSWORD") or None,
        guest_password=os.environ.get("GUEST_PASSWORD") or None,
        wifi_ssid=os.environ.get("WIFI_SSID") or None,
        wifi_password=os.environ.get("WIFI_PASSWORD") or None,
        wifi_auth=os.environ.get("WIFI_AUTH", "WPA"),
    )


def _find_config_file(explicit: str | None) -> Path:
    candidates = [explicit] if explicit else []
    candidates += [os.environ.get("HERTFORD_CONFIG")]
    candidates += DEFAULT_CONFIG_PATHS
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise FileNotFoundError(
        f"could not find rooms.yaml — checked: {[c for c in candidates if c]}"
    )
