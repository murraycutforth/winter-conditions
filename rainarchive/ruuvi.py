"""RuuviTag logger: hourly Bluetooth check, broadcast sample and full history download -> ruuvi.sqlite.

    python3 -m rainarchive.ruuvi -c config.toml sync               # what the hourly timer runs
    python3 -m rainarchive.ruuvi -c config.toml status
    python3 -m rainarchive.ruuvi -c config.toml show [--tag 9261] [--hours 24]

The tag is sometimes carried up hills, so it is often out of range. Each run:
  1. scans for SCAN_S seconds and stores the tag's latest broadcast (data format 5: T, RH, p,
     acceleration, battery, movement counter) if it is heard;
  2. connects and downloads the tag's internal log (one T/RH/p record every 5 minutes, ~10 days
     capacity) from the last complete record already stored, so a trip away is back-filled when
     the tag comes home. The tag aborts a download after 5 minutes (~6.5 min would be needed for a
     full 10 days at the ~7 records/s seen on a Pi 4), so a partial download is kept and resumed,
     up to MAX_PASSES times per run and again on the next run;
  3. logs the attempt (seen / connected / records / error) in ruuvi_sync.

Log protocol (Ruuvi firmware 3.x, Nordic UART service; checked on tag 9261, FW v3.31.1):
    write  3A 3A 11 <now: u32 BE> <start: u32 BE>        to NUS RX
    notify 3A <src> 10 <time: u32 BE> <value: i32 BE>   src 30 = T (0.01 C), 31 = RH (0.01 %),
                                                         32 = p (Pa); oldest first
    end    3A 3A 10 FF FF FF FF FF FF FF FF;  error: type F0
<start> is exclusive in practice (a record stamped exactly <start> is not re-sent), which is why
downloads resume from the newest record that has all three values.
The tag converts its internal uptime clock to unix time using <now>, so the Pi clock must be
right (it is NTP-synced). Other NUS notifications (the live data-format-5 packet sent on connect)
are ignored.

Storage (data_dir/ruuvi.sqlite, WAL, all times unix UTC):
    ruuvi_history  (tag, time) -> temperature C, humidity %, pressure hPa   from the tag's log
    ruuvi_adv      (tag, time) -> broadcast sample at each successful scan
    ruuvi_sync     one row per attempt;  ruuvi_meta  firmware/model per tag
Bluetooth must be unblocked (`sudo rfkill unblock bluetooth`; install-ruuvi.sh does it). Uses the
apt package python3-bleak, imported only when talking to real hardware.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import struct
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("rainarchive.ruuvi")

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"     # write commands
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"     # notifications
DIS = {"firmware": "00002a26-0000-1000-8000-00805f9b34fb", "model": "00002a24-0000-1000-8000-00805f9b34fb"}
RUUVI_MANUFACTURER_ID = 0x0499

SCAN_S = 30.0               # listen this long for the tag's broadcasts
CONNECT_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 320.0  # the tag itself gives up after 300 s
IDLE_TIMEOUT_S = 30.0       # no notification for this long -> treat the pass as stalled
MAX_PASSES = 4              # resume a partial download this many times per connection
CONNECT_ATTEMPTS = 3        # BlueZ connections to the tag fail transiently now and then
RETRY_PAUSE_S = 5.0

KINDS = {0x30: "temperature", 0x31: "humidity", 0x32: "pressure"}
SCALE = {"temperature": 0.01, "humidity": 0.01, "pressure": 0.01}   # -> C, %, hPa (log pressure is Pa)


@dataclass(frozen=True)
class Tag:
    name: str
    mac: str


DEFAULT_TAGS = [Tag("9261", "F6:88:CF:86:92:61")]   # the user's RuuviTag (bought Oct 2026)


def load_tags(path: str | None) -> list[Tag]:
    raw = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    tags = [Tag(str(t["name"]), str(t["mac"]).upper()) for t in raw.get("ruuvi_tags", [])]
    return tags or list(DEFAULT_TAGS)


# --- decoding --------------------------------------------------------------------------------------
def decode_df5(data: bytes) -> dict | None:
    """Ruuvi data format 5 (RAWv2) broadcast payload, i.e. the manufacturer data after the 0x0499 id."""
    if len(data) < 24 or data[0] != 5:
        return None
    t, h, p, ax, ay, az, pw, mv, seq = struct.unpack(">hHHhhhHBH", data[1:18])
    return {
        "temperature": None if t == -32768 else t * 0.005,
        "humidity": None if h == 65535 else h * 0.0025,
        "pressure": None if p == 65535 else (p + 50000) / 100.0,
        "accel_x": None if ax == -32768 else ax / 1000.0, "accel_y": None if ay == -32768 else ay / 1000.0,
        "accel_z": None if az == -32768 else az / 1000.0,
        "battery_v": None if (pw >> 5) == 2047 else ((pw >> 5) + 1600) / 1000.0,
        "tx_dbm": None if (pw & 31) == 31 else (pw & 31) * 2 - 40,
        "movement": None if mv == 255 else mv, "seq": None if seq == 65535 else seq,
        "mac": ":".join(f"{b:02X}" for b in data[18:24]),
    }


def log_command(now: int, start: int) -> bytes:
    return b"\x3a\x3a\x11" + struct.pack(">II", now, max(0, start))


def parse_log_record(data: bytes):
    """-> ('end',) | ('error',) | (kind, time, value) | None for anything that isn't a log record."""
    if len(data) != 11 or data[0] != 0x3A:
        return None
    if data[2] == 0xF0:
        return ("error",)
    if data[2] != 0x10:
        return None
    if data[3:] == b"\xff" * 8:
        return ("end",)
    kind = KINDS.get(data[1])
    if kind is None:
        return None
    ts, raw = struct.unpack(">Ii", data[3:11])
    if kind != "temperature":
        raw &= 0xFFFFFFFF          # humidity and pressure are unsigned
    return (kind, ts, raw * SCALE[kind])


# --- storage ---------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS ruuvi_history (
    tag TEXT NOT NULL, time INTEGER NOT NULL,
    temperature REAL, humidity REAL, pressure REAL, downloaded_at INTEGER NOT NULL,
    PRIMARY KEY (tag, time)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ruuvi_adv (
    tag TEXT NOT NULL, time INTEGER NOT NULL, temperature REAL, humidity REAL, pressure REAL,
    accel_x REAL, accel_y REAL, accel_z REAL, battery_v REAL, tx_dbm INTEGER, movement INTEGER,
    seq INTEGER, rssi INTEGER,
    PRIMARY KEY (tag, time)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ruuvi_sync (
    id INTEGER PRIMARY KEY, tag TEXT NOT NULL, started_at INTEGER NOT NULL, duration_s REAL,
    seen INTEGER NOT NULL, connected INTEGER NOT NULL, passes INTEGER NOT NULL DEFAULT 0,
    n_records INTEGER NOT NULL DEFAULT 0, first_time INTEGER, last_time INTEGER,
    complete INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS ruuvi_meta (tag TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (tag, key));
"""


class RuuviDB:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "ruuvi.sqlite", timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def resume_from(self, tag: str) -> int:
        """Start of the next download: the newest record that has all three values (0 = everything)."""
        r = self.db.execute("SELECT MAX(time) FROM ruuvi_history WHERE tag=? AND temperature IS NOT NULL "
                            "AND humidity IS NOT NULL AND pressure IS NOT NULL", (tag,)).fetchone()[0]
        return r or 0

    def add_records(self, tag: str, recs: list[tuple]) -> int:
        """recs: (kind, time, value). Merges the three per-quantity records of each timestamp."""
        now = int(time.time())
        with self.db:
            for kind, ts, v in recs:
                self.db.execute(
                    f"INSERT INTO ruuvi_history (tag, time, {kind}, downloaded_at) VALUES (?,?,?,?) "
                    f"ON CONFLICT(tag, time) DO UPDATE SET {kind}=excluded.{kind}, downloaded_at=excluded.downloaded_at",
                    (tag, ts, v, now))
        return len(recs)

    def add_adv(self, tag: str, t: int, d: dict, rssi) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ruuvi_adv VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (tag, t, d["temperature"], d["humidity"], d["pressure"], d["accel_x"], d["accel_y"],
                             d["accel_z"], d["battery_v"], d["tx_dbm"], d["movement"], d["seq"], rssi))

    def set_meta(self, tag: str, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO ruuvi_meta VALUES (?,?,?)", (tag, key, value))


# --- Bluetooth -------------------------------------------------------------------------------------
class BleakBackend:
    """Real hardware via bleak/BlueZ. Tests substitute a fake with the same two methods."""

    def __init__(self):
        self.devices = {}

    async def scan(self, macs: set[str], seconds: float) -> dict[str, tuple[bytes, int]]:
        from bleak import BleakScanner
        seen = {}

        def cb(dev, adv):
            mac = dev.address.upper()
            if mac in macs and RUUVI_MANUFACTURER_ID in adv.manufacturer_data:
                seen[mac] = (bytes(adv.manufacturer_data[RUUVI_MANUFACTURER_ID]), adv.rssi)
                self.devices[mac] = dev

        async with BleakScanner(detection_callback=cb):
            t_end = time.monotonic() + seconds
            while time.monotonic() < t_end and set(seen) != macs:
                await asyncio.sleep(0.5)
            await asyncio.sleep(min(2.0, max(0.0, t_end - time.monotonic())))   # a fresher packet
        return seen

    def connect(self, mac: str):
        from bleak import BleakClient
        return BleakClient(self.devices.get(mac, mac), timeout=CONNECT_TIMEOUT_S)


async def download_pass(client, start: int, now: int, idle_s: float = IDLE_TIMEOUT_S,
                        total_s: float = DOWNLOAD_TIMEOUT_S) -> tuple[list, str]:
    """One log read. Returns (records, 'complete' | 'error' | 'stalled' | 'timeout')."""
    recs, status = [], {"v": None}
    last = {"t": time.monotonic()}
    done = asyncio.Event()

    def cb(_, data):
        last["t"] = time.monotonic()
        r = parse_log_record(bytes(data))
        if r is None:
            return
        if r[0] in ("end", "error"):
            status["v"] = "complete" if r[0] == "end" else "error"
            done.set()
        else:
            recs.append(r)

    await client.start_notify(NUS_TX, cb)
    try:
        await client.write_gatt_char(NUS_RX, log_command(now, start), response=False)
        t0 = time.monotonic()
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last["t"] > idle_s:
                status["v"] = "stalled"
                break
            if time.monotonic() - t0 > total_s:
                status["v"] = "timeout"
                break
    finally:
        try:
            await client.stop_notify(NUS_TX)
        except Exception:  # noqa: BLE001 - the link may already be gone
            pass
    return recs, status["v"]


async def sync_tag(rdb: RuuviDB, backend, tag: Tag, adv: tuple | None) -> dict:
    t0 = time.time()
    row = {"tag": tag.name, "started_at": int(t0), "seen": int(adv is not None), "connected": 0, "passes": 0,
           "n_records": 0, "first_time": None, "last_time": None, "complete": 0, "error": None}
    if adv is not None:
        d = decode_df5(adv[0])
        if d:
            rdb.add_adv(tag.name, int(t0), d, adv[1])
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                async with backend.connect(tag.mac) as client:
                    row["connected"] = 1
                    for key, uuid in DIS.items():
                        try:
                            rdb.set_meta(tag.name, key, (await client.read_gatt_char(uuid)).decode(errors="replace"))
                        except Exception:  # noqa: BLE001 - optional information
                            pass
                    for _ in range(MAX_PASSES):
                        start = rdb.resume_from(tag.name)
                        recs, status = await download_pass(client, start, int(time.time()))
                        row["passes"] += 1
                        rdb.add_records(tag.name, recs)
                        row["n_records"] += len(recs)
                        if recs:
                            ts = [r[1] for r in recs]
                            row["first_time"] = min(ts) if row["first_time"] is None else min(row["first_time"], min(ts))
                            row["last_time"] = max(ts) if row["last_time"] is None else max(row["last_time"], max(ts))
                        if status == "complete":
                            row["complete"] = 1
                            break
                        row["error"] = f"pass {row['passes']}: {status}"
                        if not recs:          # no progress: don't hammer the tag
                            break
            except Exception as e:  # noqa: BLE001
                row["error"] = f"attempt {attempt}: {type(e).__name__}: {e}"
                log.debug("tag %s attempt %d failed: %s", tag.name, attempt, row["error"])
            if row["complete"] or (row["connected"] and row["error"] and "pass" in row["error"]
                                   and row["n_records"] == 0):
                break                         # done, or the tag answers but sends nothing
            if attempt < CONNECT_ATTEMPTS:
                await asyncio.sleep(RETRY_PAUSE_S)
        if row["complete"]:
            row["error"] = None
    row["duration_s"] = round(time.time() - t0, 1)
    with rdb.db:
        rdb.db.execute("INSERT INTO ruuvi_sync (tag, started_at, duration_s, seen, connected, passes, n_records, "
                       "first_time, last_time, complete, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (row["tag"], row["started_at"], row["duration_s"], row["seen"], row["connected"],
                        row["passes"], row["n_records"], row["first_time"], row["last_time"], row["complete"],
                        row["error"]))
    return row


async def sync_all(rdb: RuuviDB, tags: list[Tag], backend=None, scan_s: float = SCAN_S) -> list[dict]:
    backend = backend or BleakBackend()
    try:
        seen = await backend.scan({t.mac for t in tags}, scan_s)
    except Exception as e:  # noqa: BLE001 - e.g. adapter blocked/powered off
        log.error("Bluetooth scan failed: %s", e)
        seen = {}
    out = []
    for tag in tags:
        r = await sync_tag(rdb, backend, tag, seen.get(tag.mac))
        out.append(r)
        _fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else "-"
        if not r["seen"]:
            log.info("tag %s not in range", tag.name)
        else:
            log.info("tag %s: %d records %s .. %s in %d pass(es), %.0f s%s", tag.name, r["n_records"],
                     _fmt(r["first_time"]), _fmt(r["last_time"]), r["passes"], r["duration_s"],
                     "" if r["complete"] else f" - {r['error']}")
    return out


# --- CLI -------------------------------------------------------------------------------------------
def _data_dir(path: str | None) -> Path:
    raw = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    return Path(raw.get("data_dir", "~/rain-archive-data")).expanduser()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rainarchive.ruuvi")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("status")
    sh = sub.add_parser("show")
    sh.add_argument("--tag", default=None)
    sh.add_argument("--hours", type=float, default=24)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    tags = load_tags(a.config)
    rdb = RuuviDB(_data_dir(a.config))
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "never"
    if a.cmd == "sync":
        rows = asyncio.run(sync_all(rdb, tags))
        return 0 if all(r["complete"] or not r["seen"] for r in rows) else 1
    if a.cmd == "status":
        for t in tags:
            n, first, last = rdb.db.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM ruuvi_history WHERE tag=?",
                                            (t.name,)).fetchone()
            meta = dict(rdb.db.execute("SELECT key, value FROM ruuvi_meta WHERE tag=?", (t.name,)).fetchall())
            print(f"tag {t.name} ({t.mac}) {meta.get('model', '')} {meta.get('firmware', '')}")
            print(f"  history: {n} records, {fmt(first)} .. {fmt(last)}")
            seen = rdb.db.execute("SELECT MAX(started_at) FROM ruuvi_sync WHERE tag=? AND seen=1", (t.name,)).fetchone()[0]
            ok = rdb.db.execute("SELECT MAX(started_at) FROM ruuvi_sync WHERE tag=? AND complete=1", (t.name,)).fetchone()[0]
            print(f"  last seen: {fmt(seen)}; last complete download: {fmt(ok)}")
            adv = rdb.db.execute("SELECT time, temperature, humidity, pressure, battery_v, rssi FROM ruuvi_adv "
                                 "WHERE tag=? ORDER BY time DESC LIMIT 1", (t.name,)).fetchone()
            if adv:
                print(f"  last broadcast {fmt(adv[0])}: {adv[1]:.2f} C, {adv[2]:.1f} %, {adv[3]:.1f} hPa, "
                      f"battery {adv[4]:.3f} V, RSSI {adv[5]} dBm")
            print("  recent attempts:")
            for r in rdb.db.execute("SELECT started_at, seen, connected, passes, n_records, complete, error, duration_s "
                                    "FROM ruuvi_sync WHERE tag=? ORDER BY id DESC LIMIT 6", (t.name,)):
                print(f"    {fmt(r[0])}  {'seen' if r[1] else 'not seen'}"
                      + (f", {r[4]} records in {r[3]} pass(es), {r[7]:.0f} s, {'complete' if r[5] else r[6]}"
                         if r[1] else ""))
    elif a.cmd == "show":
        name = a.tag or tags[0].name
        since = int(time.time() - a.hours * 3600)
        print(f"{'time (UTC)':17s} {'T C':>7s} {'RH %':>6s} {'p hPa':>8s}")
        for ts, t, h, pr in rdb.db.execute("SELECT time, temperature, humidity, pressure FROM ruuvi_history "
                                           "WHERE tag=? AND time>=? ORDER BY time", (name, since)):
            f = lambda v, n: "" if v is None else f"{v:.{n}f}"
            print(f"{fmt(ts)[:16]:17s} {f(t, 2):>7s} {f(h, 1):>6s} {f(pr, 1):>8s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
