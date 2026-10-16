"""Offline test of the SAIS calendar scraper against a fake sais.gov.uk.

Run from the project folder:  python3 -m tests.test_sais_offline
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from rainarchive import sais as S

SLUGS = {2: "northern-cairngorms", 3: "lochaber", 4: "glencoe", 5: "southern-cairngorms",
         6: "creag-meagaidh", 7: "torridon"}


def day(n, hazard=None, slug="lochaber", rid=1, alt=False):
    cls = "sais_calendar__day" + (" sais_calendar__day--alternate" if alt else "")
    if hazard is None:
        return f'<div class="{cls}"> <span>{n}</span> </div>'
    img = hazard.lower().replace(" ", "")
    return (f'<div class="{cls}"> <span>{n}</span> <a title="{hazard} avalanche hazard - click to view report" '
            f'href="https://www.sais.gov.uk/{slug}/?report_id={rid}"><img alt="" '
            f'src="https://www.sais.gov.uk/wp-content/themes/sais/images/{img}.png" /></a> </div>')


def page(season, code):
    """Same layout as the real archive page: <h3>Month YYYY</h3> then a grid of day cells."""
    slug = SLUGS[code]
    head = '<div class="sais_calendar__day sais_calendar__day--heading">Sun</div>'
    dec = "".join([day(1, "Low", slug, 100 * code + 1), day(2), day(3, "Moderate", slug, 100 * code + 3, alt=True),
                   day(4, "Considerable", slug, 100 * code + 4), day(5, "High", slug, 100 * code + 5),
                   day(6, "Very High", slug, 100 * code + 6), day(7, "Na", slug, 100 * code + 7)])
    jan = day(1, "Moderate", slug, 100 * code + 8)
    return (f"<html><body><h2>{season}/{season + 1 - 2000} Forecast Archive</h2>"
            f"<h3>December {season}</h3><div class=\"sais_calendar\">{head}<div class=\"sais_calendar__day\"></div>{dec}</div>"
            f"<h3>January {season + 1}</h3><div class=\"sais_calendar\">{head}{jan}</div>"
            f"<footer><h3>Contact Us</h3></footer></body></html>")


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


class FakeSAIS:
    def __init__(self):
        self.headers, self.calls = {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        if params["y"] == 2008 and params["a"] == 7:
            return Resp("server error", 500)
        return Resp(page(params["y"], params["a"]))


def test_parse():
    days = S.parse_calendar(page(2023, 3))
    assert [(d["date"], d["level"]) for d in days] == [
        ("2023-12-01", 1), ("2023-12-03", 2), ("2023-12-04", 3), ("2023-12-05", 4), ("2023-12-06", 5),
        ("2023-12-07", None), ("2024-01-01", 2)], days
    assert all(d["area"] == "Lochaber" for d in days) and days[0]["report_id"] == 301
    assert days[5]["hazard"] == "Na" and days[4]["hazard"] == "Very High"
    try:
        S.parse_calendar(page(2023, 3).replace('title="Low avalanche', 'title="Extreme avalanche'))
        raise AssertionError("unknown hazard accepted")
    except ValueError:
        pass
    assert S.season_of(date(2024, 2, 1)) == 2023 and S.season_of(date(2023, 12, 1)) == 2023


def test_fetch():
    with tempfile.TemporaryDirectory() as tmp:
        sdb = S.SaisDB(Path(tmp))
        fake = FakeSAIS()
        f = S.Fetcher(session=fake, interval_s=0)
        today = date(2009, 1, 15)                           # seasons 2007, 2008 (current)
        s = S.fetch_calendars(sdb, f, today=today)
        assert s == {"pages": 11, "skipped": 0, "days": 77, "errors": 1}, s
        assert fake.headers["User-Agent"] == S.USER_AGENT
        n, areas = sdb.db.execute("SELECT COUNT(*), COUNT(DISTINCT area) FROM sais_calendar").fetchone()
        assert n == 77 and areas == 6
        assert (Path(tmp) / "sais_raw" / "calendar" / "2007_a3.html").exists()
        # second run: finished season skipped, current season re-fetched (and its failed page retried)
        fake.calls.clear()
        s2 = S.fetch_calendars(sdb, f, today=today)
        assert s2["skipped"] == 6 and {c["y"] for c in fake.calls} == {2008} and len(fake.calls) == 6, (s2, fake.calls)
        assert sdb.db.execute("SELECT COUNT(*) FROM sais_calendar").fetchone()[0] == 77
        # CLI status smoke test via a config pointing at the temp dir
        cfg = Path(tmp) / "c.toml"
        cfg.write_text(f'data_dir = "{tmp}"\n')
        assert S.main(["-c", str(cfg), "status"]) == 0
        print(f"OK - {n} SAIS calendar days from {len(fake.calls)} + 12 fake pages")


if __name__ == "__main__":
    test_parse()
    test_fetch()
