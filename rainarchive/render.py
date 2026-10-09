"""Colour rendering of radar grids to RGBA images (CLI previews and the dashboard).

Dry pixels are transparent so a map shows through; no-data pixels are translucent grey.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .decode import NO_DATA, UNIVERSAL_BLUE

ACC_STOPS = [0.1, 0.5, 1, 2, 5, 10, 20, 40, 80]
ACC_COLS = ["#c6e9f5", "#8fd0ec", "#56b1e0", "#2f8fd0", "#2a66b8", "#3f3f9e", "#6a2c91", "#a0247f", "#d4336a"]

_FRAME_LUT = np.zeros((256, 4), np.uint8)
for _d, _h in UNIVERSAL_BLUE.items():
    _FRAME_LUT[_d] = [int(_h[i:i + 2], 16) for i in (0, 2, 4)] + [255]
_FRAME_LUT[NO_DATA] = [128, 128, 128, 120]


def render_frame(dbz: np.ndarray) -> Image.Image:
    """uint8 dBZ codes -> RGBA in RainViewer's Universal Blue colours."""
    return Image.fromarray(_FRAME_LUT[dbz])


def render_mm(mm: np.ndarray, stops=ACC_STOPS, cols=ACC_COLS) -> Image.Image:
    """Accumulation in mm -> RGBA with a stepped colour scale (below the first stop = transparent)."""
    out = np.zeros(mm.shape + (4,), np.uint8)
    v = np.nan_to_num(mm, nan=-1)
    for s, c in zip(stops, cols):
        out[v >= s] = [int(c[i:i + 2], 16) for i in (1, 3, 5)] + [255]
    out[np.isnan(mm)] = [128, 128, 128, 120]
    return Image.fromarray(out)
