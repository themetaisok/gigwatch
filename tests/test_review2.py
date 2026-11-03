"""Second adversarial pass (changed code). FAILING tests encode suspected bugs."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

import pytest

from gigwatch import cli
from gigwatch.config import Artist, Config
from gigwatch.dedupe import dedupe
from gigwatch.match import assign_tier
from gigwatch.models import MADRID, NEW, make_event
from gigwatch.notify_telegram import split_message
from gigwatch.sources.inbox import find_date, parse_message
from gigwatch.state import State, diff, iso_utc
from tests.conftest import NOW, mk

PRIO = ["ticketmaster", "bcn_opendata", "bandsintown", "inbox", "feeds"]


def eml(subject, body_html, sender="noreply@ra.co", mid="<r2-1@ra.co>"):
    return (f"From: X <{sender}>\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: {mid}\r\nDate: Tue, 03 Nov 2026 09:00:00 +0100\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n" + body_html).encode("utf-8")


def tier_all(evs, cfg):
    for e in evs:
        e.tier = assign_tier(e, cfg)
    return [e for e in evs if e.tier]


class ListSource:
    def __init__(self, name, events):
        self.name, self.events, self.priority = name, events, 0

    def enabled(self, env):
        return True

    def fetch(self, cfg, now):
        return [copy.deepcopy(e) for e in self.events]


class Collect:
    def __init__(self, ok=True):
        self.messages, self.ok = [], ok

    def send(self, text, preview=True):
        self.messages.append(text)
        return self.ok


def cfg_dir(tmp_path, artists):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / "settings.yaml").write_text("home: {lat: 41.3874, lon: 2.1686, radius_km: 150}\n")
    (d / "artists.yaml").write_text(artists)
    (d / "venues.yaml").write_text("- name: Razzmatazz\n  lat: 41.3979\n  lon: 2.1917\n  city: Barcelona\n")
    return d


# ---------- find_date ----------

def test_send_date_equal_to_today_is_not_the_show_date():
    d = find_date("Sent 3 nov 2026. Peggy Gou plays 15 nov 23:00", NOW)
    assert d[0].day == 15 and d[0].hour == 23


def test_onsale_date_is_not_the_show_date():
    d = find_date("Tickets on sale Fri 6 Nov at 10:00. Peggy Gou · Sat 21 Nov 2026 23:00", NOW)
    assert d[0].day == 21


def test_pm_times():
    d = find_date("Sat 15 Nov 2026, 10:30 PM", NOW)
    assert d[0].hour == 22


def test_ordinal_set_is_not_september():
    d = find_date("Peggy Gou 1st set at 23:00 on 15 nov", NOW)
    assert (d[0].month, d[0].day) == (11, 15)


@pytest.mark.parametrize("text,want", [
    ("jue 5 feb 2027", (2027, 2, 5)), ("5 de febrer", (2027, 2, 5)),
    ("dissabte, 7 de març de 2027", (2027, 3, 7)), ("Sat, Nov 15", (2026, 11, 15)),
    ("15/11/2026", (2026, 11, 15)), ("2026-11-15", (2026, 11, 15)), ("10 ene", (2027, 1, 10)),
])
def test_guard_dates(text, want):
    d = find_date(text, NOW)[0]
    assert (d.year, d.month, d.day) == want


# ---------- inbox region / tier ----------

def test_footer_company_city_does_not_demote_local_show(cfg):
    raw = eml("Peggy Gou at Razzmatazz", "<p>Peggy Gou · Sat 21 Nov 2026 23:00 · Razzmatazz</p>"
              "<p>Resident Advisor Ltd, Hoxton Square, London</p>")
    ev = parse_message(raw, cfg, NOW)
    assert assign_tier(ev, cfg) == "must-see"


def test_title_match_maybe_not_bypassed_by_inbox():
    cfg = Config(artists=[Artist("Jungle", title_match="maybe")])
    raw = eml("El Libro de la Jungle Book: concierto familiar",
              "<p>Sat 21 Nov 2026 18:00 · Barcelona</p>", sender="hello@feverup.com")
    ev = parse_message(raw, cfg, NOW)
    assert assign_tier(ev, cfg) != "must-see"


# ---------- uid reuse ----------

def test_inbox_first_then_structured_listing_is_not_a_second_new(cfg):
    """RA mail (no venue) arrives first; next run the same show appears in an ICS/TM listing
    with a venue, with the mail already read: no shared source id, different key -> 2nd NEW."""
    st = State(last_digest_at=iso_utc(NOW))
    inbox = mk(source="inbox", venue=None, lat=None, lon=None, city="Barcelona",
               source_ids={"inbox:1"}, raw_tier_hint="must-see")
    r1 = tier_all(dedupe([inbox], PRIO, cfg.artists, cfg.venues), cfg)
    assert [a.kind for a in diff(st, r1, NOW, cfg, {"inbox"}).actions] == [NEW]
    tm = mk(source="ticketmaster", source_ids={"tm:9"})
    r2 = tier_all(dedupe([tm], PRIO, cfg.artists, cfg.venues), cfg)
    acts = diff(st, r2, NOW + timedelta(hours=3), cfg, {"ticketmaster"}).actions
    assert not [a for a in acts if a.kind == NEW]


def test_ics_new_instance_with_reused_uid_is_new_not_change(cfg):
    """Recurring ICS: next week's instance carries the same UID as last week's (still in state
    for 7 days). It must alert as NEW, not hijack the old record as '🔄 CHANGED'."""
    st = State(last_digest_at=iso_utc(NOW))
    w1 = mk(source="feeds", source_ids={"feed:ics:abc"}, start=NOW.astimezone(MADRID) + timedelta(days=1))
    diff(st, tier_all(dedupe([w1], PRIO, cfg.artists, cfg.venues), cfg), NOW, cfg, {"feeds"})
    later = NOW + timedelta(days=2)
    w2 = mk(source="feeds", source_ids={"feed:ics:abc"}, start=NOW.astimezone(MADRID) + timedelta(days=8))
    acts = diff(st, tier_all(dedupe([w2], PRIO, cfg.artists, cfg.venues), cfg), later, cfg, {"feeds"}).actions
    assert [a.kind for a in acts if a.kind in (NEW, "IMMEDIATE_CHANGE")] == [NEW]


# ---------- run flow ----------

def test_first_run_summary_failure_retries_summary_not_one_by_one(tmp_path):
    names = ["Peggy Gou", "Dixon", "Solomun"]
    d = cfg_dir(tmp_path, "".join(f"- name: {n}\n" for n in names) + "- name: Khruangbin\n  tier: maybe\n")
    st = tmp_path / "s.json"
    evs = [mk(title=n, artists=[n], source_ids={f"tm:{i}"}, start=datetime(2026, 12, 1 + i, 22, tzinfo=MADRID))
           for i, n in enumerate(names + ["Khruangbin"])]
    src = [ListSource("ticketmaster", evs)]
    cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=Collect(ok=False), sources=src)
    n = Collect()
    cli.run(d, st, tmp_path / "o", now=NOW + timedelta(hours=3), env={}, notifier=n, sources=src)
    assert sum("First run" in m for m in n.messages) == 1 and len(n.messages) <= 2


def test_guard_old_v1_state_without_new_fields(tmp_path):
    d = cfg_dir(tmp_path, "- name: Peggy Gou\n")
    st = tmp_path / "s.json"
    old = {"version": 1, "last_digest_at": iso_utc(NOW), "last_source_warning_at": None,
           "inbox_seen": [], "feed_seen": [], "events": {"abc": {
               "first_seen": iso_utc(NOW), "last_seen": iso_utc(NOW), "notified_tier": "must-see",
               "last_hash": "x", "reminders_sent": [], "missed_runs": 0, "possibly_removed": False,
               "digested": True, "snapshot": {"title": "Dixon", "start": "2026-11-05T23:00:00+01:00",
                                              "venue": "Input", "city": "Barcelona", "url": "",
                                              "status": "onsale", "artists": ["Dixon"],
                                              "tier": "must-see", "sources": ["ticketmaster"]}}}}
    st.write_text(json.dumps(old))
    code, rep = cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=Collect(),
                        sources=[ListSource("ticketmaster", [mk()])])
    assert code == 0 and any("tomorrow" in m or "7 days" in m for m in rep.messages)


def test_guard_empty_config_dir_fails_cleanly(tmp_path):
    (tmp_path / "c").mkdir()
    code, _ = cli.run(tmp_path / "c", tmp_path / "s.json", tmp_path / "o", now=NOW, env={},
                      notifier=Collect(), sources=[])
    assert code == 2 and not (tmp_path / "s.json").exists()


# ---------- telegram chunking ----------

def test_long_line_split_keeps_html_balanced():
    line = "<b>" + " ".join(["word"] * 1500) + "</b>"
    for ch in split_message(line):
        assert ch.count("<b>") == ch.count("</b>")
