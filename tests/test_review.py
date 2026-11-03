"""Adversarial review tests. A FAILING test here encodes a suspected bug (see review notes).
Passing tests are regression guards for behaviour that is correct today."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gigwatch import cli
from gigwatch.config import Artist, Config, Venue
from gigwatch.dedupe import dedupe
from gigwatch.match import assign_tier, match_artist
from gigwatch.models import MADRID, NEW, REMINDER_7D, make_event
from gigwatch.notify_telegram import format_event
from gigwatch.sources.bcn_opendata import _TIME_RE, decode_bytes, parse_csv
from gigwatch.sources.feeds import FeedsSource, parse_ics
from gigwatch.sources.inbox import InboxSource, find_date, parse_message
from gigwatch.state import State, diff, iso_utc
from tests.conftest import FIX, NOW, mk
from tests.test_inbox import FakeIMAP

ENV = {"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "app-pass"}
PRIO = ["ticketmaster", "bcn_opendata", "bandsintown", "inbox", "feeds"]
ROOT = Path(__file__).resolve().parents[1]


def eml(subject, body_html, sender="noreply@dice.fm", mid="<x-1@dice.fm>"):
    return (f"From: X <{sender}>\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: {mid}\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n" + body_html).encode("utf-8")


class ListSource:
    def __init__(self, name, events, fail=False):
        self.name, self.events, self.fail, self.priority = name, events, fail, 0

    def enabled(self, env):
        return True

    def fetch(self, cfg, now):
        if self.fail:
            raise RuntimeError("down")
        return [copy.deepcopy(e) for e in self.events]


class Collect:
    def __init__(self, ok=True):
        self.messages, self.ok = [], ok

    def send(self, text, preview=True):
        self.messages.append(text)
        return self.ok


@pytest.fixture(autouse=True)
def _no_network_bcn(monkeypatch):
    monkeypatch.setattr(cli.BcnOpenDataSource, "fetch", lambda self, cfg, now: [])


def write_cfg(tmp_path, artists="- name: Peggy Gou\n"):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "settings.yaml").write_text("home: {lat: 41.3874, lon: 2.1686, radius_km: 150}\n")
    (d / "artists.yaml").write_text(artists)
    return d


# ---------------- A. missed shows ----------------

def test_E_inbox_must_see_without_date_is_immediate(cfg):
    ev = parse_message((FIX / "dice_email.eml").read_bytes(), cfg, NOW)
    assert ev.artists == ["Dixon"] and ev.start is None
    assert assign_tier(ev, cfg) == "must-see"


def test_E_feed_immediate_flag_promotes_must_see(cfg):
    cfg.feeds = [{"name": "GA", "url": "https://x.test/f", "immediate": True}]
    src = FeedsSource(cfg, State())
    body = ("<?xml version='1.0'?><rss version='2.0'><channel><item><guid>1</guid>"
            "<title>Peggy Gou announces Barcelona show</title><link>https://n.test/1</link>"
            "</item></channel></rss>")
    (ev,) = src._rss(body, cfg.feeds[0], cfg, NOW)
    assert assign_tier(ev, cfg) == "must-see"


def test_inbox_mail_not_lost_when_telegram_send_fails(tmp_path):
    """Mail is flagged \\Seen and added to inbox_seen during fetch; a failed send reverts the
    event but the mail is never fetched again."""
    raw = (FIX / "ra_email.eml").read_bytes()
    fake = FakeIMAP([raw])
    d = write_cfg(tmp_path)
    st = tmp_path / "state.json"
    State(last_digest_at=iso_utc(NOW)).save(st)  # not first run
    cli.run(d, st, tmp_path / "out", now=NOW, env=ENV, notifier=Collect(ok=False),
            imap_factory=fake, sources=None, sleep=lambda s: None)
    # second run: telegram is back
    fake2 = FakeIMAP([raw])
    fake2.search = lambda c, k: ("OK", [b"" if fake.stored else b"1"])
    n = Collect()
    cli.run(d, st, tmp_path / "out", now=NOW + timedelta(hours=3), env=ENV, notifier=n,
            imap_factory=fake2, sleep=lambda s: None)
    assert any("Peggy Gou" in m for m in n.messages)


def test_inbox_mail_not_lost_on_crash_after_fetch(tmp_path, monkeypatch):
    fake = FakeIMAP([(FIX / "ra_email.eml").read_bytes()])
    d = write_cfg(tmp_path)
    st = tmp_path / "state.json"
    monkeypatch.setattr(cli, "dedupe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        cli.run(d, st, tmp_path / "out", now=NOW, env=ENV, notifier=Collect(), imap_factory=fake)
    assert fake.stored == []  # still unseen, so the next run can retry


def test_inbox_spanish_weekday_abbrev_mar_is_not_march(cfg):
    d = find_date("Peggy Gou · mar 18 nov 2026 · 21:00 · Razzmatazz", NOW)
    assert d and d[0].month == 11 and d[0].day == 18


@pytest.mark.parametrize("text", ["sábado 15 de noviembre, 23:00", "dissabte 15 de novembre 23:00",
                                  "15 dic 2026 22:00", "15 de diciembre de 2026"])
def test_inbox_spanish_catalan_dates(text):
    d = find_date(text, NOW)
    assert d is not None and d[0].day == 15


def test_inbox_first_date_is_send_date_not_show_date(cfg):
    raw = eml("Peggy Gou just announced", "<p>Posted 30 Oct 2026</p><p>Peggy Gou · Sat 21 Nov 2026 23:00"
              " · Barcelona</p>")
    ev = parse_message(raw, cfg, NOW)
    assert ev.start is not None and ev.start.date().day == 21  # 30 Oct is in the past -> dropped


def test_inbox_short_name_ame_in_subject(cfg):
    raw = eml("Âme is playing Barcelona", "<p>Âme · Sat 21 Nov 2026 23:59 · Input, Barcelona</p>")
    ev = parse_message(raw, cfg, NOW)
    assert ev.artists == ["Âme"]


def test_bcn_row_without_music_keyword_is_kept():
    hdr = decode_bytes((FIX / "agenda_cultural_ca.csv").read_bytes()).split("\n", 1)[0].strip()
    cols = hdr.split(",")
    row = dict.fromkeys(cols, "")
    row.update(register_id="9", name="Fontaines D.C.", addresses_road_name="Passeig Olímpic",
               addresses_town="Barcelona", start_date="2026-11-20", end_date="2026-11-20",
               timetable="21:00")
    text = hdr + "\n" + ",".join(row[c] for c in cols) + "\n"
    evs = parse_csv(text, NOW)
    assert any("Fontaines" in e.title for e in evs)


def test_region_venue_only_known_venue_without_coords(cfg):
    cfg.venues.append(Venue("Laut", city="Barcelona"))
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Peggy Gou\nDTSTART:20261121T230000\n"
           "LOCATION:Laut\nEND:VEVENT\nEND:VCALENDAR\n")
    evs = parse_ics(ics, "club", cfg, NOW)
    cli.enrich_coords(evs, cfg)
    assert len(cli.filter_events(evs, cfg, NOW)) == 1


def test_region_uppercase_city_kept(cfg):
    ev = mk(city="BARCELONA", venue="Somewhere", lat=None, lon=None)
    assert cli.filter_events([ev], cfg, NOW)


def test_dedupe_festival_umbrella_does_not_swallow_show(cfg):
    day = datetime(2026, 11, 21, 0, 0, tzinfo=MADRID)
    umb = mk(source="bcn_opendata", title="Barcelona Jazz Festival", artists=[], venue="Carrer X 1",
             lat=None, lon=None, start=day, time_unknown=True, end=day + timedelta(days=5),
             source_ids={"bcn:a"}, url=None, sources_urls={})
    show = mk(source="bcn_opendata", title="Barcelona Jazz Festival: Peggy Gou", artists=[],
              venue="Carrer X 1", lat=None, lon=None, start=day.replace(hour=21),
              source_ids={"bcn:b"}, url=None, sources_urls={})
    out = dedupe([umb, show], PRIO, cfg.artists, cfg.venues)
    assert any(assign_tier(e, cfg) == "must-see" for e in out)


def test_uid_stable_when_lower_priority_partner_disappears(cfg):
    """Inbox (no venue) + ICS (venue) merge under the inbox key; next run the mail is gone
    (already read) and the ICS-only group gets a different uid -> second NEW alert."""
    st = State(last_digest_at=iso_utc(NOW))
    start = datetime(2026, 11, 21, 23, 0, tzinfo=MADRID)
    inbox = mk(source="inbox", venue=None, lat=None, lon=None, city="Barcelona", url="https://ra.co/e/1",
               source_ids={"inbox:1"}, sources_urls={"RA email": "https://ra.co/e/1"})
    ics = mk(source="feeds", venue="Razzmatazz", source_ids={"feed:ics:1"},
             url="https://club/1", sources_urls={"club": "https://club/1"})
    r1 = dedupe([inbox, ics], PRIO, cfg.artists, cfg.venues)
    for e in r1:
        e.tier = assign_tier(e, cfg)
    a1 = diff(st, r1, NOW, cfg, {"inbox", "feeds"}).actions
    r2 = dedupe([copy.deepcopy(ics)], PRIO, cfg.artists, cfg.venues)
    for e in r2:
        e.tier = assign_tier(e, cfg)
    a2 = diff(st, r2, NOW + timedelta(hours=3), cfg, {"feeds"}).actions
    assert [a.kind for a in a1] == [NEW] and not [a for a in a2 if a.kind == NEW]
    assert start  # silence


def test_no_change_alert_when_precise_source_fails_for_one_run(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    day = datetime(2026, 11, 21, 0, 0, tzinfo=MADRID)
    bcn = mk(source="bcn_opendata", title="Concert: Peggy Gou", artists=[], venue="Carrer de Pamplona 88",
             start=day, time_unknown=True, lat=41.39778, lon=2.19167, url=None, source_ids={"bcn:1"},
             sources_urls={"BCN Open Data": ""})
    ics = mk(source="feeds", title="Peggy Gou", start=day.replace(hour=23), lat=41.3979, lon=2.1917,
             source_ids={"feed:ics:1"}, url=None, sources_urls={"club": ""})
    for evs, t in (([bcn, ics], NOW), ([bcn], NOW + timedelta(hours=3))):
        out = dedupe(copy.deepcopy(evs), PRIO, cfg.artists, cfg.venues)
        for e in out:
            e.tier = assign_tier(e, cfg)
        acts = diff(st, out, t, cfg, {"bcn_opendata", "feeds"}).actions
    assert acts == []  # got "🔄 CHANGED" because start fell back to 00:00 / time_unknown


def test_overflow_must_see_not_parked_until_weekly_digest(tmp_path):
    names = [f"Artist {i:02d}" for i in range(17)]
    d = write_cfg(tmp_path, "".join(f"- name: {n}\n" for n in names))
    st = tmp_path / "state.json"
    State(events={"x": {"snapshot": {}, "notified_tier": None, "digested": True, "possibly_removed": False,
                         "last_seen": iso_utc(NOW), "reminders_sent": [], "missed_runs": 0}},
          last_digest_at=iso_utc(NOW)).save(st)
    evs = [mk(title=n, artists=[n], source_ids={f"tm:{i}"}, start=datetime(2026, 12, 1, 20, tzinfo=MADRID)
              + timedelta(days=i)) for i, n in enumerate(names)]
    n = Collect()
    cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=n, sources=[ListSource("ticketmaster", evs)])
    joined = "\n".join(n.messages)
    assert all(x in joined for x in names)  # #16/#17 wait up to 7 days for Monday's digest


def test_reminder_for_inbox_only_event(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    ev = mk(source="inbox", venue=None, start=NOW.astimezone(MADRID) + timedelta(days=10),
            source_ids={"inbox:1"}, tier="must-see")
    ev.uid = "u1"
    diff(st, [ev], NOW, cfg, {"inbox"})
    acts = diff(st, [], NOW + timedelta(days=4), cfg, {"inbox"}).actions
    assert any(a.kind == REMINDER_7D for a in acts)


def test_state_saved_even_if_render_fails(tmp_path, monkeypatch):
    d = write_cfg(tmp_path)
    st = tmp_path / "state.json"
    State(last_digest_at=iso_utc(NOW)).save(st)
    monkeypatch.setattr(cli, "write_outputs", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    try:
        cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=Collect(),
                sources=[ListSource("ticketmaster", [mk()])])
    except OSError:
        pass
    assert State.load(st).events  # else the alert is re-sent on every run


def test_exit_code_nonzero_when_every_send_fails(tmp_path):
    d = write_cfg(tmp_path)
    st = tmp_path / "state.json"
    State(last_digest_at=iso_utc(NOW)).save(st)
    code, _ = cli.run(d, st, tmp_path / "o", now=NOW, env={}, notifier=Collect(ok=False),
                      sources=[ListSource("ticketmaster", [mk()])])
    assert code != 0


def test_bcn_timetable_date_not_parsed_as_time():
    m = _TIME_RE.search("Del 12.11.2026, a les 21h")
    assert m is None or (m.group(1), m.group(2)) != ("12", "11")


# ---------------- B. noise ----------------

@pytest.mark.parametrize("artist,title", [
    ("Jungle", "Jungle & Drum and Bass Session"),
    ("Jungle", "El Llibre de la Jungla / The Jungle Book: concert familiar"),
    ("Arca", "Concert: Arca de Noé"),
    ("Black Coffee", "Black Coffee Jazz Brunch"),
])
def test_common_word_artist_not_must_see_from_title(artist, title):
    # decision 14: common-word names opt in via title_match (the seed config sets it)
    cfg = Config(artists=[Artist(artist, title_match="maybe")])
    ev = mk(source="bcn_opendata", title=title, artists=[], venue="Carrer X", lat=None, lon=None)
    assert assign_tier(ev, cfg) != "must-see"


def test_inbox_recommendation_footer_does_not_set_headliner(cfg):
    raw = eml("Dixon is playing Barcelona",
              "<p>Dixon · Sat 21 Nov 2026 23:00 · Barcelona</p><p>You might also like: Peggy Gou</p>")
    ev = parse_message(raw, cfg, NOW)
    assert ev.artists == ["Dixon"]


def test_inbox_url_tracking_token_not_published(cfg):
    raw = eml("Dixon is playing Barcelona",
              '<p>Dixon Sat 21 Nov 2026</p><a href="https://click.dice.fm/ls/click?upn=SECRET123">Get tickets</a>')
    ev = parse_message(raw, cfg, NOW)
    assert not ev.url or "SECRET123" not in ev.url


# ---------------- C. robustness ----------------

def test_pages_deploy_is_actually_triggered():
    """Pushes made with GITHUB_TOKEN do not trigger other workflows, so there is no pages
    workflow at all: Pages is served from the branch (Settings > Pages > main /docs)."""
    assert not (ROOT / ".github/workflows/pages.yml").exists()
    setup = (ROOT / "docs/SETUP.md").read_text()
    assert "Deploy from a branch" in setup and "/docs" in setup


def test_workflow_has_job_timeout():
    g = (ROOT / ".github/workflows/gigwatch.yml").read_text()
    assert "timeout-minutes" in g


def test_imap_connection_has_timeout(cfg):
    seen = {}

    def factory(host, *a, **kw):
        seen.update(kw)
        f = FakeIMAP([])
        return f
    InboxSource(ENV, State(), imap_factory=factory).fetch(cfg, NOW)
    assert "timeout" in seen  # imaplib.IMAP4_SSL(host) with no timeout can hang the job for 6 h


def test_inbox_out_of_region_tour_email_not_immediate(cfg):
    raw = eml("Peggy Gou announced a new show", "<p>Peggy Gou · Sat 21 Nov 2026 23:00 · La Riviera, Madrid</p>",
              sender="noreply@songkick.com")
    ev = parse_message(raw, cfg, NOW)
    kept = cli.filter_events([ev], cfg, NOW)
    assert not kept or assign_tier(kept[0], cfg) != "must-see"


# ---------------- regression guards (expected to pass) ----------------

@pytest.mark.parametrize("title,want", [
    ("Nitsa: Peggy Gou + John Talabot", {"Peggy Gou", "John Talabot"}),
    ("FRED AGAIN.. live", {"Fred again.."}),
    ("Fontaines DC en concert", {"Fontaines D.C."}),
    ("Biceps & triceps workshop", set()),
    ("Barcarola concert", set()),
    ("The Dixon Brothers", set()),
])
def test_guard_title_matching(title, want):
    cfg = Config(artists=[Artist("Peggy Gou"), Artist("John Talabot"), Artist("Fred again.."),
                          Artist("Fontaines D.C.", aliases=["Fontaines DC"]), Artist("Bicep"),
                          Artist("Arca"), Artist("Dixon", exclude=["dixon brothers"])])
    ev = mk(title=title, artists=[])
    assert {h.artist.name for h in match_artist(ev, cfg.artists)} == want


def test_guard_ics_tzid_and_utc(cfg):
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Peggy Gou\r\n"
           'DTSTART;TZID="America/New_York":20261121T170000\r\nEND:VEVENT\r\n'
           "BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:Dixon\r\nDTSTART:20260329T013000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    a, b = parse_ics(ics, "x", cfg, NOW)
    assert a.start.astimezone(MADRID).hour == 23
    assert b.start.astimezone(MADRID).hour == 3  # across the March DST jump


def test_guard_telegram_href_escaping():
    ev = mk(url='https://x.test/a?b=1&c="><script>', artists=["A<b>"])
    txt = format_event(ev)
    assert '&quot;&gt;&lt;script&gt;' in txt and "&amp;c=" in txt and "A&lt;b&gt;" in txt


def test_guard_same_artist_same_day_two_far_venues_not_merged(cfg):
    a = mk(venue="Razzmatazz", lat=41.3979, lon=2.1917, source_ids={"tm:1"})
    b = mk(venue="Input", lat=41.3687, lon=2.1469, source_ids={"tm:2"},
           start=datetime(2026, 11, 21, 18, 0, tzinfo=MADRID))
    assert len(dedupe([a, b], PRIO, cfg.artists, cfg.venues)) == 2


def test_guard_uid_independent_of_fetched_at_and_url(cfg):
    a = mk(fetched_at=NOW, url="https://t/1")
    b = mk(fetched_at=NOW + timedelta(hours=3), url="https://t/2")
    assert dedupe([a], PRIO, cfg.artists)[0].uid == dedupe([b], PRIO, cfg.artists)[0].uid


def test_guard_no_realert_on_repeat(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    ev = dedupe([mk()], PRIO, cfg.artists, cfg.venues)[0]
    ev.tier = "must-see"
    assert diff(st, [ev], NOW, cfg, {"ticketmaster"}).actions
    ev2 = copy.deepcopy(ev)
    assert diff(st, [ev2], NOW + timedelta(hours=3), cfg, {"ticketmaster"}).actions == []


def test_guard_tier_upgrade_alerts(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    ev = dedupe([mk()], PRIO, cfg.artists, cfg.venues)[0]
    ev.tier = "maybe"
    diff(st, [ev], NOW, cfg, {"ticketmaster"})
    ev.tier = "must-see"
    assert [a.kind for a in diff(st, [ev], NOW, cfg, {"ticketmaster"}).actions] == [NEW]


def test_guard_inbox_never_persists_body(tmp_path):
    fake = FakeIMAP([(FIX / "ra_email.eml").read_bytes()])
    d = write_cfg(tmp_path)
    st = tmp_path / "state.json"
    cli.run(d, st, tmp_path / "o", now=NOW, env=ENV, notifier=Collect(), imap_factory=fake)
    s = st.read_text()
    assert "is playing" not in s and "Unsubscribe" not in s and "ra-0001" not in s
