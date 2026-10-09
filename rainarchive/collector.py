"""The always-on collector loop."""
from __future__ import annotations

import logging
import signal
import threading
import time

from .config import Config
from .fetcher import RainViewer
from .store import Store, utc

log = logging.getLogger(__name__)


def poll_once(cfg: Config, store: Store, rv: RainViewer) -> dict:
    """Fetch every past frame RainViewer offers that isn't archived yet. Returns a summary."""
    idx = rv.index()
    host = idx["host"]
    frames = (idx.get("radar") or {}).get("past") or []
    new = [f for f in frames if not store.have_frame(int(f["time"]))]
    saved, partial = 0, 0
    for f in new:
        t = int(f["time"])
        dbz, ok, unmatched = rv.fetch_frame(host, f["path"], store.grid)
        total = store.grid.nx * store.grid.ny
        if ok == 0:
            log.error("frame %s: no tiles downloaded, will retry next poll", utc(t))
            continue
        if ok < total:
            partial += 1
            # keep partial frames only if they are about to fall out of RainViewer's 2 h window
            if t > frames[-1]["time"] - 90 * 60:
                log.warning("frame %s: %d/%d tiles, will retry next poll", utc(t), ok, total)
                continue
        stats = store.save_frame(t, dbz, ok, unmatched)
        saved += 1
        log.info("saved %s  tiles %d/%d  wet %.1f%%  mean %.2f mm/h  max %d dBZ",
                 utc(t).strftime("%Y-%m-%d %H:%M"), ok, total, stats["wet_frac"] * 100,
                 stats["mean_rate"], stats["max_dbz"])
    gap = None
    if frames:
        oldest = int(frames[0]["time"])
        last_before = store.db.execute("SELECT MAX(time) FROM frames WHERE time < ?", (oldest,)).fetchone()[0]
        if last_before and oldest - last_before > 600:
            gap = f"{utc(last_before):%Y-%m-%d %H:%M} to {utc(oldest):%Y-%m-%d %H:%M} UTC"
            log.warning("gap in archive: %s (RainViewer only keeps ~2 h)", gap)
    return {"offered": len(frames), "new": len(new), "saved": saved, "partial": partial}


def run_forever(cfg: Config) -> None:
    store = Store(cfg)
    rv = RainViewer(cfg)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    interval = cfg.poll_minutes * 60
    errors = 0
    last_ok = None
    log.info("collector started: grid %dx%d px (%d tiles, zoom %d), data in %s",
             store.grid.width, store.grid.height, store.grid.nx * store.grid.ny, store.grid.zoom, cfg.data_dir)
    while not stop.is_set():
        started = time.time()
        summary: dict = {}
        try:
            summary = poll_once(cfg, store, rv)
            errors = 0
            last_ok = started
        except Exception as e:  # noqa: BLE001 - keep running whatever happens
            errors += 1
            log.exception("poll failed (%d in a row): %s", errors, e)
        try:
            now = int(time.time())
            for d in store.days_needing_build(now):
                log.info("daily total %s", store.build_daily(d))
            pruned = store.prune_frames(cfg.keep_frames_days, now)
            if pruned:
                log.info("pruned %d old frame files", pruned)
        except Exception:  # noqa: BLE001
            log.exception("daily/prune step failed")
        store.write_status(last_poll_utc=utc(int(started)).isoformat(),
                           last_success_utc=utc(int(last_ok)).isoformat() if last_ok else None,
                           consecutive_errors=errors, last_summary=summary)
        # sleep until the next aligned poll time (e.g. :01:30, :16:30, ...)
        nxt = (int(time.time()) // interval + 1) * interval + cfg.poll_offset_seconds
        stop.wait(max(5, nxt - time.time()))
    log.info("collector stopped")
