"""Command line: python3 -m rainarchive [-c config.toml] <command>

    run            run forever (what the systemd service does)
    once           poll once, build any finished daily totals, exit
    status         print collector health and archive size
    build-daily    (re)build daily totals: --date YYYY-MM-DD or --all
    render         write a colour PNG preview: --latest, --time ISO, or --daily YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import load_config
from .render import render_frame, render_mm
from .store import Store, utc


_render_frame, _render_mm = render_frame, render_mm


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rainarchive")
    p.add_argument("-c", "--config", default=None, help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("status")
    b = sub.add_parser("build-daily")
    b.add_argument("--date")
    b.add_argument("--all", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--latest", action="store_true")
    r.add_argument("--time")
    r.add_argument("--daily")
    r.add_argument("-o", "--out", default="preview.png")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    cfg = load_config(a.config)

    if a.cmd == "run":
        from .collector import run_forever
        run_forever(cfg)
        return 0

    store = Store(cfg)
    if a.cmd == "once":
        from .collector import poll_once
        from .fetcher import RainViewer
        print(poll_once(cfg, store, RainViewer(cfg)))
        for d in store.days_needing_build(int(time.time())):
            print(store.build_daily(d))
        store.write_status(last_poll_utc=utc(int(time.time())).isoformat(), consecutive_errors=0)
    elif a.cmd == "status":
        st = cfg.data_dir / "status.json"
        print(st.read_text() if st.exists() else "{}  (collector has not written a status yet)")
        size = sum(f.stat().st_size for f in Path(cfg.data_dir).rglob("*") if f.is_file())
        n_days = store.db.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        print(f"archive size: {size / 1e6:.1f} MB, daily totals: {n_days}")
    elif a.cmd == "build-daily":
        if a.all:
            dates = [r[0] for r in store.db.execute(
                "SELECT DISTINCT date(time,'unixepoch') FROM frames WHERE path IS NOT NULL ORDER BY 1")]
        elif a.date:
            dates = [a.date]
        else:
            dates = store.days_needing_build(int(time.time()))
        for d in dates:
            print(json.dumps(store.build_daily(d)))
    elif a.cmd == "render":
        if a.daily:
            from .query import Archive
            img = _render_mm(Archive(cfg.data_dir).daily(a.daily))
        else:
            if a.latest:
                t = store.db.execute("SELECT MAX(time) FROM frames WHERE path IS NOT NULL").fetchone()[0]
            else:
                from .query import _ts
                t = _ts(a.time)
            img = _render_frame(store.load_frame(t))
        img.save(a.out)
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
