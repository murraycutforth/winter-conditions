"""RainViewer 'Universal Blue' colour scheme  <->  radar reflectivity (dBZ)  <->  rain rate.

Storage encoding for frame grids (uint8):
    0          no echo (dry)
    5..95      reflectivity in dBZ
    255        no data (tile missing / failed to download)
"""
from __future__ import annotations

import numpy as np

NO_ECHO = 0
NO_DATA = 255

# Rain colours for scheme 2 (Universal Blue) from RainViewer's published colour table
# (https://www.rainviewer.com/files/rainviewer_api_colors_table.csv). dBZ -> RRGGBBAA.
UNIVERSAL_BLUE = {
    5: "92887164", 6: "9e93756e", 7: "aa9e7978", 8: "b6a97e82", 9: "c2b4828c", 10: "cec08796",
    11: "d2c48ba0", 12: "d6c88faa", 13: "dacc93b4", 14: "ded097be", 15: "88ddeeff", 16: "6cd1ebff",
    17: "51c5e8ff", 18: "36bae5ff", 19: "1baee2ff", 20: "00a3e0ff", 21: "009ad5ff", 22: "0091caff",
    23: "0088bfff", 24: "007fb4ff", 25: "0077aaff", 26: "0070a3ff", 27: "00699cff", 28: "006295ff",
    29: "005b8eff", 30: "005588ff", 31: "005180ff", 32: "004e78ff", 33: "004a70ff", 34: "004768ff",
    35: "ffee00ff", 36: "ffe000ff", 37: "ffd200ff", 38: "ffc500ff", 39: "ffb700ff", 40: "ffaa00ff",
    41: "ff9f00ff", 42: "ff9500ff", 43: "ff8b00ff", 44: "ff8100ff", 45: "ff4400ff", 46: "f23600ff",
    47: "e62800ff", 48: "d91b00ff", 49: "cd0d00ff", 50: "c10000ff", 51: "a80000ff", 52: "8f0000ff",
    53: "760000ff", 54: "5d0000ff", 55: "ffaaffff", 56: "ff9fffff", 57: "ff95ffff", 58: "ff8bffff",
    59: "ff81ffff", 60: "ff77ffff", 61: "ff6cffff", 62: "ff62ffff", 63: "ff58ffff", 64: "ff4effff",
    65: "ffffffff",
}

_DBZ = np.array(list(UNIVERSAL_BLUE.keys()), dtype=np.uint8)
_RGB = np.array([[int(h[i:i + 2], 16) for i in (0, 2, 4)] for h in UNIVERSAL_BLUE.values()], dtype=np.int32)
_KEYS = (_RGB[:, 0] << 16) | (_RGB[:, 1] << 8) | _RGB[:, 2]
_ORDER = np.argsort(_KEYS)
_KEYS_SORTED = _KEYS[_ORDER]
_DBZ_SORTED = _DBZ[_ORDER]

MAX_NEAREST_DIST2 = 900  # colours further than this from any table entry are treated as dry


def rgba_to_dbz(rgba: np.ndarray) -> tuple[np.ndarray, int]:
    """Decode an (H, W, 4) uint8 RGBA tile to uint8 dBZ codes.

    Returns (dbz, n_unmatched) where n_unmatched counts opaque pixels whose colour had no exact
    table match (resolved by nearest colour) - useful as a data-quality signal.
    """
    rgba = np.asarray(rgba)
    a = rgba[..., 3]
    key = (rgba[..., 0].astype(np.int32) << 16) | (rgba[..., 1].astype(np.int32) << 8) | rgba[..., 2]
    out = np.zeros(a.shape, dtype=np.uint8)
    wet = a >= 40
    if not wet.any():
        return out, 0
    k = key[wet]
    idx = np.clip(np.searchsorted(_KEYS_SORTED, k), 0, len(_KEYS_SORTED) - 1)
    exact = _KEYS_SORTED[idx] == k
    vals = np.where(exact, _DBZ_SORTED[idx], 0).astype(np.uint8)
    n_unmatched = int((~exact).sum())
    if n_unmatched:
        miss = np.unique(k[~exact])
        rgb = np.stack([(miss >> 16) & 255, (miss >> 8) & 255, miss & 255], axis=1)
        d2 = ((rgb[:, None, :] - _RGB[None, :, :]) ** 2).sum(axis=2)
        best = d2.argmin(axis=1)
        mapped = np.where(d2[np.arange(len(miss)), best] <= MAX_NEAREST_DIST2, _DBZ[best], 0).astype(np.uint8)
        lut = dict(zip(miss.tolist(), mapped.tolist()))
        sub = k[~exact]
        vals[~exact] = np.fromiter((lut[v] for v in sub.tolist()), dtype=np.uint8, count=sub.size)
    out[wet] = vals
    return out, n_unmatched


# --- reflectivity -> rain rate ------------------------------------------------------------------
# Marshall-Palmer Z = 200 R^1.6, reflectivity capped at 55 dBZ to limit hail/clutter spikes.
DBZ_CAP = 55


def _build_rate_lut() -> np.ndarray:
    lut = np.zeros(256, dtype=np.float32)
    d = np.arange(5, 96, dtype=np.float64)
    lut[5:96] = (10 ** (np.minimum(d, DBZ_CAP) / 10) / 200) ** (1 / 1.6)
    lut[NO_DATA] = np.nan
    return lut


RATE_LUT = _build_rate_lut()  # index with uint8 dBZ code -> mm/h (NaN for no-data)


def dbz_to_rate(dbz: np.ndarray) -> np.ndarray:
    """uint8 dBZ codes -> rain rate in mm/h (float32; NaN where no data)."""
    return RATE_LUT[dbz]
