"""Grid geometry: a fixed Web Mercator (EPSG:3857) pixel grid built from z/x/y map tiles.

Every stored frame is a 2-D array on exactly this grid, so pixel (row, col) always means the
same place on the ground. The grid definition is also written to the database (meta table)
so a dashboard can reconstruct it without importing this package.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np

TILE = 256


def lonlat_to_tile_xy(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Fractional tile coordinates for a lon/lat at zoom z."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


@dataclass(frozen=True)
class Grid:
    zoom: int
    x0: int  # first tile column (inclusive)
    y0: int  # first tile row (inclusive)
    nx: int  # number of tile columns
    ny: int  # number of tile rows

    @classmethod
    def from_bbox(cls, west: float, south: float, east: float, north: float, zoom: int) -> "Grid":
        xa, ya = lonlat_to_tile_xy(west, north, zoom)
        xb, yb = lonlat_to_tile_xy(east, south, zoom)
        x0, x1 = int(math.floor(xa)), int(math.floor(xb))
        y0, y1 = int(math.floor(ya)), int(math.floor(yb))
        return cls(zoom, x0, y0, x1 - x0 + 1, y1 - y0 + 1)

    @property
    def width(self) -> int:
        return self.nx * TILE

    @property
    def height(self) -> int:
        return self.ny * TILE

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    def tiles(self):
        """Yield (x, y, row_offset, col_offset) for every tile in the grid."""
        for j in range(self.ny):
            for i in range(self.nx):
                yield self.x0 + i, self.y0 + j, j * TILE, i * TILE

    # --- pixel <-> geographic ---------------------------------------------------------------
    def latlon_to_pixel(self, lat: float, lon: float) -> tuple[int, int]:
        """(row, col) of the pixel containing lat/lon. Raises ValueError if outside the grid."""
        x, y = lonlat_to_tile_xy(lon, lat, self.zoom)
        col = int(math.floor((x - self.x0) * TILE))
        row = int(math.floor((y - self.y0) * TILE))
        if not (0 <= row < self.height and 0 <= col < self.width):
            raise ValueError(f"({lat}, {lon}) is outside the archive grid")
        return row, col

    def pixel_centres(self) -> tuple[np.ndarray, np.ndarray]:
        """1-D arrays: latitude of each row centre, longitude of each column centre."""
        n = TILE * 2 ** self.zoom
        cols = (np.arange(self.width) + 0.5 + self.x0 * TILE) / n
        rows = (np.arange(self.height) + 0.5 + self.y0 * TILE) / n
        lon = cols * 360.0 - 180.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * rows))))
        return lat, lon

    def pixel_area_km2(self) -> np.ndarray:
        """Ground area of each row's pixels (km^2), shape (height,). Mercator pixels shrink with latitude."""
        lat, _ = self.pixel_centres()
        side_m = 40075016.686 / (TILE * 2 ** self.zoom) * np.cos(np.radians(lat))
        return (side_m / 1000.0) ** 2

    def bounds(self) -> dict:
        """Geographic bounds of the grid edges."""
        n = TILE * 2 ** self.zoom

        def lat_of(py):
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / n))))

        return {
            "west": self.x0 * TILE / n * 360 - 180,
            "east": (self.x0 + self.nx) * TILE / n * 360 - 180,
            "north": lat_of(self.y0 * TILE),
            "south": lat_of((self.y0 + self.ny) * TILE),
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(width=self.width, height=self.height, crs="EPSG:3857 (Web Mercator tile pixels)",
                 bounds=self.bounds(),
                 note="row 0 is the north edge; pixel (r,c) spans global tile-pixels "
                      "(x0*256+c, y0*256+r) at this zoom")
        return d
