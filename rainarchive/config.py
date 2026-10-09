"""Configuration loading (TOML). All times in the archive are UTC."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .grid import Grid


@dataclass
class Site:
    name: str
    lat: float
    lon: float


@dataclass
class Config:
    data_dir: Path
    poll_minutes: int = 15
    poll_offset_seconds: int = 90
    zoom: int = 7
    west: float = -8.7
    south: float = 54.6
    east: float = -0.6
    north: float = 61.0
    min_request_interval_s: float = 0.8
    timeout_s: float = 20.0
    retries: int = 3
    keep_frames_days: int = 0          # 0 = keep raw frames forever
    user_agent: str = "rain-archive/1.0 (personal Raspberry Pi archive)"
    sites: list[Site] = field(default_factory=list)

    @property
    def grid(self) -> Grid:
        return Grid.from_bbox(self.west, self.south, self.east, self.north, self.zoom)


DEFAULT_SITES = [
    Site("Ben Nevis", 56.7969, -5.0036),
    Site("Cairn Gorm", 57.1167, -3.6444),
    Site("Edinburgh", 55.9533, -3.1883),
]


def load_config(path: str | Path | None) -> Config:
    raw: dict = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    area = raw.get("area", {})
    fetch = raw.get("fetch", {})
    sites = [Site(s["name"], float(s["lat"]), float(s["lon"])) for s in raw.get("sites", [])] or DEFAULT_SITES
    cfg = Config(
        data_dir=Path(raw.get("data_dir", "~/rain-archive-data")).expanduser(),
        poll_minutes=int(raw.get("poll_minutes", 15)),
        poll_offset_seconds=int(raw.get("poll_offset_seconds", 90)),
        zoom=int(area.get("zoom", 7)),
        west=float(area.get("west", -8.7)), south=float(area.get("south", 54.6)),
        east=float(area.get("east", -0.6)), north=float(area.get("north", 61.0)),
        min_request_interval_s=float(fetch.get("min_request_interval_s", 0.8)),
        timeout_s=float(fetch.get("timeout_s", 20)),
        retries=int(fetch.get("retries", 3)),
        keep_frames_days=int(raw.get("keep_frames_days", 0)),
        user_agent=str(fetch.get("user_agent", Config.user_agent)),
        sites=sites,
    )
    if cfg.zoom > 7:
        raise ValueError("RainViewer's free API serves radar tiles up to zoom 7 only")
    if cfg.min_request_interval_s < 0.65:
        raise ValueError("min_request_interval_s below 0.65 risks RainViewer's 100 requests/minute limit")
    return cfg
