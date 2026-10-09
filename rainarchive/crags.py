"""Winter climbing venues tracked by the weather extension.

lat/lon: a point on or just below the main cliffs (WGS84).
elev_m:  a representative height of the climbing (mid-crag). Open-Meteo downscales 2 m temperature
         to this height, and it matters far more than a few hundred metres of position.
aspect:  the compass direction the main faces look towards (0 = faces north, 90 = faces east).
         Used to tell whether the wind blows onto the face (rime) or over the top of it (loading).

Positions checked on a map (Sept 2026). Heights and aspects are judgement calls; adjust in config.toml.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Crag:
    name: str
    lat: float
    lon: float
    elev_m: float
    aspect: float
    area: str = ""


DEFAULT_CRAGS = [
    Crag("Northern Corries", 57.1040, -3.6753, 1050, 340, "Northern Cairngorms"),       # Coire an t-Sneachda
    Crag("Ben Nevis", 56.8010, -5.0060, 1150, 20, "Lochaber"),                           # North Face
    Crag("Lochnagar", 56.9559, -3.2380, 1000, 30, "Southern Cairngorms"),               # NE Corrie
    Crag("Creag Meagaidh", 56.9570, -4.5850, 950, 60, "Creag Meagaidh"),                # Coire Ardair
    Crag("Beinn Eighe", 57.5852, -5.4307, 850, 320, "Torridon"),                          # Triple Buttress
    Crag("Beinn Bhan", 57.4480, -5.6655, 700, 75, "Torridon"),                          # Applecross corries
    Crag("Shelter Stone Crag", 57.0922, -3.6502, 900, 45, "Northern Cairngorms"),       # Loch Avon basin
    Crag("Church Door Buttress", 56.6455, -5.0290, 1050, 0, "Glencoe"),                 # Bidean nam Bian
    Crag("Stob Coire nan Lochan", 56.6515, -5.0130, 1000, 45, "Glencoe"),
    Crag("Beinn Dorain", 56.5030, -4.7250, 850, 290, "Southern Highlands"),             # west face
    Crag("Beinn a' Bhuird", 57.0950, -3.4729, 1000, 20, "Southern Cairngorms"),         # Garbh Choire
    Crag("A' Mhaighdean", 57.7195, -5.3475, 850, 315, "Fisherfield"),
]


def load_crags(config_path: str | Path | None) -> list[Crag]:
    if config_path:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        if raw.get("crags"):
            return [Crag(c["name"], float(c["lat"]), float(c["lon"]), float(c["elev_m"]),
                         float(c["aspect"]), c.get("area", "")) for c in raw["crags"]]
    return list(DEFAULT_CRAGS)
