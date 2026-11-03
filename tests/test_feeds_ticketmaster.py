import json
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from gigwatch.cli import filter_events
from gigwatch.config import Config
from gigwatch.match import assign_tier
from gigwatch.models import MADRID
from gigwatch.sources.base import SourceError
from gigwatch.sources.feeds import FeedsSource, is_ics, parse_ics, unfold, unwrap_google
from gigwatch.sources.ticketmaster import TM_URL, TicketmasterSource, map_event
from gigwatch.state import State
from tests.conftest import FIX, NOW

RSS_URL = "https://alerts.example.test/feed.xml"
ICS_URL = "https://calendar.example.test/venue.ics"


def feeds_cfg(cfg, *feeds):
    cfg.feeds = list(feeds)
    return cfg


@responses.activate
def test_rss_feed_matches_and_marks_seen(cfg):
    responses.add(responses.GET, RSS_URL, body=(FIX / "google_alert.xml").read_text())
    feeds_cfg(cfg, {"name": "Alerts", "url": RSS_URL})
    st = State()
    src = FeedsSource(cfg, st, sleep=lambda s: None)
    evs = src.fetch(cfg, NOW)
    assert len(evs) == 1
    e = evs[0]
    assert e.title == "Floating Points adds second Barcelona date" and e.start is None
    assert e.raw_tier_hint == "heads-up" and e.url == "https://example.com/floating-points-bcn"
    assert assign_tier(e, cfg) is None or assign_tier(e, cfg) == "heads-up"
    assert len(st.feed_seen) == 2  # unmatched entry is marked seen without alerting
    assert src.fetch(cfg, NOW) == []  # nothing new on the second poll


def test_feed_seen_is_capped_fifo():
    st = State()
    for i in range(2100):
        st.mark_feed_seen(str(i))
    assert len(st.feed_seen) == 2000 and st.feed_seen[0] == "100" and st.feed_seen[-1] == "2099"


@responses.activate
def test_all_feeds_failing_raises(cfg):
    responses.add(responses.GET, RSS_URL, status=404)
    feeds_cfg(cfg, {"name": "A", "url": RSS_URL})
    with pytest.raises(SourceError):
        FeedsSource(cfg, State(), sleep=lambda s: None).fetch(cfg, NOW)
    assert not FeedsSource(Config(), State()).enabled({})


def test_unwrap_google():
    assert unwrap_google("https://www.google.com/url?sa=t&url=https://a.example/x&ct=ga") == "https://a.example/x"
    assert unwrap_google("https://b.example/y") == "https://b.example/y"


def test_ics_detection():
    assert is_ics("https://x.example/a", "BEGIN:VCALENDAR\r\n")
    assert is_ics("https://x.example/cal.ics", "<html>")
    assert is_ics("webcal://x.example/cal", "")
    assert not is_ics("https://x.example/feed.xml", "<?xml")


def test_ics_unfold_and_parse(cfg):
    text = (FIX / "venue_calendar.ics").read_text()
    assert "folded across several lines" in " ".join(unfold(text))
    evs = parse_ics(text, "Club calendar", cfg, NOW)
    assert len(evs) == 3
    d = evs[0]
    assert d.artists == ["Dixon"] and d.venue == "Sala Apolo" and d.city == "Barcelona"
    assert d.start.isoformat() == "2026-11-28T23:30:00+01:00" and d.url == "https://club.example/dixon"
    assert "comma and a semicolon; ok" in d.description
    m = evs[1]  # UTC datetime converted to Madrid; url taken from description
    assert m.start.isoformat() == "2026-12-05T22:00:00+01:00" and m.url == "https://club.example/madrid-fp"
    assert m.artists == ["Floating Points"] and m.city is None
    allday = evs[2]
    assert allday.time_unknown and allday.start == allday.start.replace(hour=0)


@responses.activate
def test_ics_feed_goes_through_geo_filter_and_tiers(cfg):
    responses.add(responses.GET, ICS_URL, body=(FIX / "venue_calendar.ics").read_text())
    feeds_cfg(cfg, {"name": "Club calendar", "url": "webcal://calendar.example.test/venue.ics"})
    evs = FeedsSource(cfg, State(), sleep=lambda s: None).fetch(cfg, NOW)
    kept = filter_events(evs, cfg, NOW)
    assert sorted(e.title for e in kept) == ["Dixon @ Sala Apolo", "Launch party"]  # Madrid event dropped
    tiers = {e.title: assign_tier(e, cfg) for e in kept}
    assert tiers["Dixon @ Sala Apolo"] == "must-see"
    assert tiers["Launch party"] is None


# ---------------- Ticketmaster ----------------

def tm_json(n):
    return json.loads((FIX / f"tm_page{n}.json").read_text())


def test_tm_mapping_details():
    evs = {e["id"]: map_event(e, NOW) for p in (tm_json(0), tm_json(1)) for e in p["_embedded"]["events"]}
    peggy = evs["tm-001"]
    assert peggy.start.isoformat() == "2026-11-21T23:00:00+01:00" and peggy.artists == ["Peggy Gou"]
    assert (peggy.lat, peggy.lon) == (41.39778, 2.19167) and peggy.status == "onsale"
    assert peggy.genres == ["Music", "Dance/Electronic", "House"] and peggy.onsale_at.year == 2026
    assert peggy.source_ids == {"tm:tm-001"} and peggy.sources_urls == {"Ticketmaster": peggy.url}
    assert evs["tm-003"].status == "canceled"
    fp = evs["tm-004"]  # no localTime / dateTime
    assert fp.time_unknown and fp.start.hour == 0 and fp.start.day == 12
    nov = evs["tm-005"]  # venue without location
    assert nov.lat is None and nov.venue == "Razzmatazz" and nov.city == "Barcelona"


def test_tm_malformed_local_time_and_missing_date():
    base = {"id": "x", "name": "N", "dates": {"start": {"localDate": "2026-12-01", "localTime": "late"}}}
    ev = map_event(base, NOW)
    assert ev.time_unknown and ev.start.hour == 0
    assert map_event({"id": "y", "name": "N", "dates": {"start": {}}}, NOW) is None


def make_tm_callback(calls):
    def cb(request):
        q = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
        calls.append(q)
        if "geoPoint" in q:
            return 200, {}, json.dumps(tm_json(int(q["page"])))
        return 200, {}, json.dumps({"page": {"size": 50, "totalElements": 0, "totalPages": 0, "number": 0}})
    return cb


@responses.activate
def test_tm_fetch_paging_params_and_throttle(cfg):
    calls = []
    responses.add_callback(responses.GET, TM_URL, callback=make_tm_callback(calls))
    sleeps = []
    src = TicketmasterSource({"TICKETMASTER_API_KEY": "KEY"}, sleep=sleeps.append)
    evs = src.fetch(cfg, NOW)
    assert len(evs) == 6
    geo = [c for c in calls if "geoPoint" in c]
    assert [c["page"] for c in geo] == ["0", "1"]
    a = geo[0]
    assert a["locale"] == "*" and a["countryCode"] == "ES" and a["classificationName"] == "Music"
    assert a["radius"] == "150" and a["unit"] == "km" and a["size"] == "200" and a["sort"] == "date,asc"
    assert a["startDateTime"] == "2026-11-03T12:00:00Z" and a["endDateTime"] == "2027-05-02T12:00:00Z"
    assert len(a["geoPoint"]) == 5
    kw = [c for c in calls if "keyword" in c]
    assert [c["keyword"] for c in kw] == [x.name for x in cfg.artists] and kw[0]["size"] == "50"
    assert sleeps.count(0.25) == len(calls)


@responses.activate
def test_tm_retries_429_then_ok_and_all_failures_raise(cfg):
    responses.add(responses.GET, TM_URL, status=429)
    responses.add(responses.GET, TM_URL, json={"page": {"totalPages": 0}})
    cfg.artists = []
    sleeps = []
    assert TicketmasterSource({"TICKETMASTER_API_KEY": "SECRETKEY"}, sleep=sleeps.append).fetch(cfg, NOW) == []
    assert 1.0 in sleeps


@responses.activate
def test_tm_total_failure_raises_without_leaking_key(cfg):
    responses.add(responses.GET, TM_URL, status=500)
    cfg.artists = []
    with pytest.raises(SourceError) as ei:
        TicketmasterSource({"TICKETMASTER_API_KEY": "SECRETKEY"}, sleep=lambda s: None).fetch(cfg, NOW)
    assert "SECRETKEY" not in str(ei.value)


def test_tm_enabled_only_with_key():
    assert TicketmasterSource({"TICKETMASTER_API_KEY": "k"}).enabled({"TICKETMASTER_API_KEY": "k"})
    assert not TicketmasterSource({}).enabled({}) and not TicketmasterSource({}).enabled({"TICKETMASTER_API_KEY": ""})


@responses.activate
def test_tm_stops_at_1000_results(cfg):
    page = {"_embedded": {"events": [tm_json(0)["_embedded"]["events"][0]]}, "page": {"size": 200, "totalPages": 99, "number": 0}}
    n = []
    responses.add_callback(responses.GET, TM_URL, callback=lambda r: (n.append(1) or (200, {}, json.dumps(page))))
    cfg.artists = []
    TicketmasterSource({"TICKETMASTER_API_KEY": "k"}, sleep=lambda s: None).fetch(cfg, NOW)
    assert len(n) == 5  # (4+1)*200 >= 1000
