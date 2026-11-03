"""Third adversarial pass. Failing tests = real bugs; the rest are regression guards."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from gigwatch import cli
from gigwatch.config import load
from gigwatch.dedupe import dedupe
from gigwatch.match import assign_tier, venue_match
from gigwatch.models import MADRID, NEW, make_event
from gigwatch.notify_telegram import split_message
from gigwatch.sources.inbox import find_date
from gigwatch.sources.ticketmaster import map_event
from gigwatch.state import State, diff, iso_utc
from tests.conftest import NOW, mk

PRIO = ["ticketmaster", "bcn_opendata", "bandsintown", "inbox", "feeds"]
SEED = load("config")


class Src:
    def __init__(self, events, fail=False):
        self.name, self.events, self.fail, self.priority = "ticketmaster", events, fail, 0

    def enabled(self, env):
        return True

    def fetch(self, cfg, now):
        if self.fail:
            raise RuntimeError("down")
        return [copy.deepcopy(e) for e in self.events]


class Collect:
    def __init__(self):
        self.messages = []

    def send(self, text, preview=True):
        self.messages.append(text)
        return True


def run_diff(st, evs, cfg, now, ok):
    out = dedupe(copy.deepcopy(evs), PRIO, cfg.artists, cfg.venues)
    for e in out:
        e.tier = assign_tier(e, cfg)
    return diff(st, [e for e in out if e.tier], now, cfg, ok).actions


# ---- (b) find_date: sent-day skip must not move a show happening tonight ----

def test_tonight_overnight_range_keeps_tonight():
    sent = datetime(2026, 11, 3, 9, 0, tzinfo=MADRID)
    d = find_date("Peggy Gou · Tue 3 Nov 2026 23:00 - Wed 4 Nov 2026 06:00 · Razzmatazz", NOW, sent)
    assert (d[0].day, d[0].hour) == (3, 23)  # got 4 Nov 06:00: user goes the wrong night


@pytest.mark.parametrize("text,want", [
    ("jue 5 feb 2027", (2027, 2, 5)), ("5 de febrer", (2027, 2, 5)),
    ("dissabte, 7 de març de 2027", (2027, 3, 7)), ("Sat, Nov 15", (2026, 11, 15)),
    ("15/11/2026", (2026, 11, 15)), ("2026-11-15", (2026, 11, 15)), ("10 ene", (2027, 1, 10)),
    ("mar 18 nov 2026 21:00", (2026, 11, 18)), ("15 de diciembre de 2026", (2026, 12, 15)),
    ("Sent 3 nov 2026. Peggy Gou plays 15 nov 23:00", (2026, 11, 15)),
    ("Peggy Gou tonight! 3 Nov 2026 23:00", (2026, 11, 3)),
])
def test_guard_dates(text, want):
    d = find_date(text, NOW, datetime(2026, 11, 3, 9, tzinfo=MADRID))[0]
    assert (d.year, d.month, d.day) == want


# ---- (e) first run ----

def test_first_run_with_all_sources_down_does_not_consume_the_summary(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "settings.yaml").write_text("home: {lat: 41.3874, lon: 2.1686, radius_km: 150}\n")
    (d / "artists.yaml").write_text("- name: Peggy Gou\n- name: Dixon\n- name: Solomun\n")
    st = tmp_path / "s.json"
    cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=Collect(), sources=[Src([], fail=True)])
    evs = [mk(title=n, artists=[n], source_ids={f"tm:{i}"}, start=datetime(2026, 12, 1 + i, 22, tzinfo=MADRID))
           for i, n in enumerate(["Peggy Gou", "Dixon", "Solomun"])]
    n = Collect()
    cli.run(d, st, tmp_path / "o", now=NOW + timedelta(hours=3), env={}, notifier=n, sources=[Src(evs)])
    assert sum("First run" in m for m in n.messages) == 1  # got 3 one-by-one alerts


# ---- guards ----

def test_guard_two_dated_shows_same_artist_one_email(cfg):
    """RA mail (no venue) parsed as Fri; Fri@Nitsa and Sat@Razzmatazz listed later: two NEWs total."""
    st = State(last_digest_at=iso_utc(NOW), first_run_done=True)
    fri = datetime(2026, 11, 20, 23, 0, tzinfo=MADRID)
    mail = mk(source="inbox", venue=None, lat=None, lon=None, city="Barcelona", start=fri,
              source_ids={"inbox:1"}, raw_tier_hint="must-see")
    a1 = run_diff(st, [mail], cfg, NOW, {"inbox"})
    tm_fri = mk(venue="Nitsa", lat=41.3745, lon=2.1695, start=fri, source_ids={"tm:1"})
    tm_sat = mk(venue="Razzmatazz", start=fri + timedelta(days=1), source_ids={"tm:2"})
    a2 = run_diff(st, [tm_fri, tm_sat], cfg, NOW + timedelta(hours=3), {"ticketmaster"})
    a3 = run_diff(st, [tm_fri, tm_sat], cfg, NOW + timedelta(hours=6), {"ticketmaster"})
    news = [a for a in a1 + a2 + a3 if a.kind == NEW]
    assert len(news) == 2 and not a3
    assert len(st.events) == 2


def test_guard_tm_flamenco_madrid_city_dropped():
    e = {"id": "fl1", "name": "Tablao Flamenco 1911", "url": "https://tm/x",
         "dates": {"start": {"localDate": "2026-11-20", "localTime": "19:00:00"}},
         "_embedded": {"venues": [{"name": "Tablao Flamenco 1911", "city": {"name": "Madrid"},
                                   "location": {"latitude": "41.3825", "longitude": "2.1769"}}]},
         "classifications": [{"segment": {"name": "Music"}, "genre": {"name": "World"}}]}
    evs = [map_event({**e, "id": f"fl{i}"}, NOW) for i in range(152)]
    assert all(assign_tier(ev, SEED) is None for ev in evs)
    st = State(last_digest_at=iso_utc(NOW), first_run_done=True)
    assert run_diff(st, evs, SEED, NOW, {"ticketmaster"}) == [] and st.events == {}


@pytest.mark.parametrize("name,want", [
    ("Sala Razzmatazz 1", "Razzmatazz"), ("Razzmatazz 3", "Razzmatazz"), ("La 2 de Apolo", "Nitsa"),
    ("Sala Laut", "Laut"), ("Sala Input", "Input"), ("Sala Upload - Poble Espanyol", "Sala Upload"),
    ("WOLF", "Sala Wolf"),
])
def test_tm_venue_names_match_seed(name, want):
    v = venue_match(name, SEED.venues)
    assert v is not None and v.name == want


def test_guard_chunks_balanced_with_link_and_entities():
    line = '<b>' + " ".join(["a&amp;b"] * 900) + '</b> <a href="https://x.test/?a=1&amp;b=2">' + \
           " ".join(["t"] * 2500) + "</a>"
    chunks = split_message(line)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 4000
        assert c.count("<b>") == c.count("</b>") and c.count("<a ") == c.count("</a>")
        assert not c.rstrip().endswith("&amp") and "&am " not in c
