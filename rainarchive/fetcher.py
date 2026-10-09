"""RainViewer Weather Maps API client with a polite, rate-limited request loop.

Free-tier limits (since Jan 2026): 100 requests / IP / minute, max zoom 7, Universal Blue colour
scheme only, past ~2 hours of frames at 10-minute spacing.
"""
from __future__ import annotations

import io
import logging
import time

import numpy as np
import requests
from PIL import Image

from .config import Config
from .decode import NO_DATA, rgba_to_dbz
from .grid import Grid, TILE

log = logging.getLogger(__name__)

INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
COLOR_SCHEME = 2      # Universal Blue
OPTIONS = "0_0"       # no smoothing (exact palette colours), no separate snow colours


class RainViewer:
    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = cfg.user_agent
        self._last = 0.0

    def _get(self, url: str) -> requests.Response:
        """GET with throttling and retries (backs off harder on HTTP 429)."""
        err: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            wait = self._last + self.cfg.min_request_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(url, timeout=self.cfg.timeout_s)
                if r.status_code == 429:
                    raise requests.HTTPError("429 rate limited", response=r)
                r.raise_for_status()
                return r
            except (requests.RequestException, OSError) as e:
                err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                backoff = 65 if status == 429 else 5 * (attempt + 1)
                if attempt < self.cfg.retries:
                    log.warning("GET %s failed (%s); retry in %ss", url, e, backoff)
                    time.sleep(backoff)
        raise err  # type: ignore[misc]

    def index(self) -> dict:
        return self._get(INDEX_URL).json()

    def tile_rgba(self, host: str, path: str, z: int, x: int, y: int) -> np.ndarray:
        url = f"{host}{path}/{TILE}/{z}/{x}/{y}/{COLOR_SCHEME}/{OPTIONS}.png"
        r = self._get(url)
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        if img.size != (TILE, TILE):
            img = img.resize((TILE, TILE), Image.NEAREST)
        return np.asarray(img)

    def fetch_frame(self, host: str, path: str, grid: Grid) -> tuple[np.ndarray, int, int]:
        """Download and decode one radar frame onto the archive grid.

        Returns (dbz uint8 array, tiles_ok, unmatched_pixel_count). Tiles that fail after retries
        are filled with NO_DATA (255) rather than being mistaken for 'dry'.
        """
        out = np.full(grid.shape, NO_DATA, dtype=np.uint8)
        ok = unmatched = 0
        for x, y, r0, c0 in grid.tiles():
            try:
                rgba = self.tile_rgba(host, path, grid.zoom, x, y)
            except Exception as e:  # noqa: BLE001 - one bad tile must not lose the frame
                log.warning("tile %s/%s/%s for %s failed: %s", grid.zoom, x, y, path, e)
                continue
            dbz, um = rgba_to_dbz(rgba)
            out[r0:r0 + TILE, c0:c0 + TILE] = dbz
            ok += 1
            unmatched += um
        return out, ok, unmatched
