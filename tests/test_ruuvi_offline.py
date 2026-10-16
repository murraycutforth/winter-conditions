"""Offline test of the RuuviTag logger against a fake Bluetooth backend (no bleak needed).

Run from the project folder:  python3 -m tests.test_ruuvi_offline
"""
from __future__ import annotations

import asyncio
import struct
import tempfile
from pathlib import Path

from rainarchive import ruuvi as R

MAC = "F6:88:CF:86:92:61"
# real packets captured from tag 9261 on 2026-10-16
ADV = bytes.fromhex("050bd964dbcce2fffcffec0410c416117db4f688cf869261")
LIVE_ON_CONNECT = bytes.fromhex("050bca653ccceffffcfff00410c396117e36")


def test_decode():
    d = R.decode_df5(ADV)
    assert abs(d["temperature"] - 15.165) < 1e-9 and abs(d["humidity"] - 64.5475) < 1e-9, d
    assert abs(d["pressure"] - 1024.50) < 1e-9 and d["battery_v"] == 3.168 and d["tx_dbm"] == 4, d
    assert (d["accel_x"], d["accel_y"], d["accel_z"]) == (-0.004, -0.02, 1.04) and d["mac"] == MAC
    assert d["movement"] == 17 and d["seq"] == 32180
    assert R.decode_df5(b"\x03" + ADV[1:]) is None
    for hexrec, kind, value in (("3a30106ad27470000005e7", "temperature", 15.11),
                                ("3a31106ad2747000001930", "humidity", 64.48),
                                ("3a32106ad2747000018f5b", "pressure", 1022.35)):
        k, ts, v = R.parse_log_record(bytes.fromhex(hexrec))
        assert k == kind and ts == 0x6AD27470 and abs(v - value) < 1e-9, (k, ts, v)
    neg = bytes.fromhex("3a30106ad27470") + struct.pack(">i", -1234)
    assert R.parse_log_record(neg)[2] == -12.34                             # sub-zero temperatures
    assert R.parse_log_record(bytes.fromhex("3a3a10ffffffffffffffff")) == ("end",)
    assert R.parse_log_record(bytes.fromhex("3a3af0ffffffffffffffff")) == ("error",)
    assert R.parse_log_record(LIVE_ON_CONNECT) is None
    assert R.log_command(0x6AD28E48, 0x6AD27470) == bytes.fromhex("3a3a116ad28e486ad27470")


class FakeClient:
    """Speaks the NUS log protocol. Aborts each pass after `abort_after` records, like the tag's
    5-minute limit; sends the live data packet first, like the real tag."""

    def __init__(self, log, abort_after=None, fail_connect=0):
        self.log, self.abort_after, self.fail_connect = log, abort_after, fail_connect   # fail the first N connects
        self.cb, self.commands = None, []

    async def __aenter__(self):
        if self.fail_connect:
            self.fail_connect -= 1
            raise TimeoutError("connection timed out")
        return self

    async def __aexit__(self, *exc):
        return False

    async def read_gatt_char(self, uuid):
        return {R.DIS["firmware"]: b"Ruuvi FW v3.31.1+default", R.DIS["model"]: b"RuuviTag B"}[uuid]

    async def start_notify(self, uuid, cb):
        assert uuid == R.NUS_TX
        self.cb = cb

    async def stop_notify(self, uuid):
        self.cb = None

    async def write_gatt_char(self, uuid, data, response=False):
        assert uuid == R.NUS_RX and data[:3] == b"\x3a\x3a\x11" and len(data) == 11
        now, start = struct.unpack(">II", data[3:])
        self.commands.append(start)
        asyncio.get_running_loop().create_task(self._send(start))

    async def _send(self, start):
        cb = self.cb
        cb(None, bytearray(LIVE_ON_CONNECT))
        n = 0
        for ts, t, h, p in self.log:
            if ts < start:
                continue
            for src, raw in ((0x31, round(h * 100)), (0x32, round(p * 100)), (0x30, round(t * 100))):
                if self.abort_after is not None and n >= self.abort_after:
                    cb(None, bytearray(b"\x3a\x3a\xf0" + b"\xff" * 8))
                    return
                cb(None, bytearray(bytes([0x3A, src, 0x10]) + struct.pack(">Ii", ts, raw)))
                n += 1
            await asyncio.sleep(0)
        cb(None, bytearray(b"\x3a\x3a\x10" + b"\xff" * 8))


class FakeBackend:
    def __init__(self, client, present=True):
        self.client, self.present = client, present

    async def scan(self, macs, seconds):
        return {MAC: (ADV, -67)} if self.present and MAC in macs else {}

    def connect(self, mac):
        return self.client


R.RETRY_PAUSE_S = 0.0


def run(rdb, backend):
    return asyncio.run(R.sync_all(rdb, [R.Tag("9261", MAC)], backend, scan_s=0))[0]


def test_sync():
    t0 = 1791500000
    log = [(t0 + 300 * i, -2.5 + 0.01 * i, 90.0 - 0.01 * i, 1000.0 + 0.1 * i) for i in range(300)]
    with tempfile.TemporaryDirectory() as tmp:
        rdb = R.RuuviDB(Path(tmp))
        # 1. out of range
        r = run(rdb, FakeBackend(FakeClient(log), present=False))
        assert r["seen"] == 0 and r["connected"] == 0 and r["n_records"] == 0
        # 2. in range; each pass aborted after 400 records -> resumes until complete (3 passes)
        client = FakeClient(log, abort_after=400)
        r = run(rdb, FakeBackend(client))
        assert r["seen"] and r["connected"] and r["complete"] and r["error"] is None, r
        assert r["passes"] == 3 and client.commands[0] == 0 and client.commands[1] > 0, (r, client.commands)
        rows = rdb.db.execute("SELECT time, temperature, humidity, pressure FROM ruuvi_history ORDER BY time").fetchall()
        assert len(rows) == 300 and rows[0] == (t0, -2.5, 90.0, 1000.0), rows[0]
        assert all(None not in x for x in rows) and abs(rows[-1][1] - 0.49) < 1e-9
        assert dict(rdb.db.execute("SELECT key, value FROM ruuvi_meta").fetchall())["model"] == "RuuviTag B"
        assert rdb.db.execute("SELECT COUNT(*), MAX(battery_v), MAX(rssi) FROM ruuvi_adv").fetchone() == (1, 3.168, -67)
        # 3. next hour: only from the last complete record, plus the new ones
        log += [(t0 + 300 * i, 1.0, 80.0, 990.0) for i in range(300, 312)]
        client = FakeClient(log)
        r = run(rdb, FakeBackend(client))
        assert r["complete"] and client.commands == [t0 + 300 * 299] and r["n_records"] == 3 * 13, (r, client.commands)
        assert rdb.db.execute("SELECT COUNT(*) FROM ruuvi_history").fetchone()[0] == 312
        # 4. a tag that stops responding mid-way (every pass aborts after 0 records): no hammering
        r = run(rdb, FakeBackend(FakeClient(log, abort_after=0)))
        assert not r["complete"] and r["passes"] == 1 and "error" in r["error"], r
        # 5. a transient connection failure is retried within the run
        log.append((t0 + 300 * 312, 2.0, 70.0, 980.0))
        r = run(rdb, FakeBackend(FakeClient(log, fail_connect=1)))
        assert r["connected"] and r["complete"] and r["error"] is None and r["n_records"] == 6, r
        # 6. persistent connection failure is logged after CONNECT_ATTEMPTS; the broadcast is still stored
        r = run(rdb, FakeBackend(FakeClient(log, fail_connect=99)))
        assert r["seen"] and not r["connected"] and f"attempt {R.CONNECT_ATTEMPTS}: TimeoutError" in r["error"], r
        assert rdb.db.execute("SELECT COUNT(*) FROM ruuvi_sync").fetchone()[0] == 6
        # CLI read-only commands
        cfg = Path(tmp) / "c.toml"
        cfg.write_text(f'data_dir = "{tmp}"\n[[ruuvi_tags]]\nname = "9261"\nmac = "{MAC.lower()}"\n')
        assert R.load_tags(str(cfg)) == [R.Tag("9261", MAC)] and R.load_tags(None) == R.DEFAULT_TAGS
        assert R.main(["-c", str(cfg), "status"]) == 0
        assert R.main(["-c", str(cfg), "show", "--hours", "1e6"]) == 0
        print("OK - ruuvi: 313 history records, 6 sync attempts")


if __name__ == "__main__":
    test_decode()
    test_sync()
