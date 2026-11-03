from datetime import datetime

from gigwatch.dedupe import dedupe, key
from gigwatch.models import MADRID
from tests.conftest import NOW, mk

PRIO = ["ticketmaster", "bcn_opendata", "bandsintown", "inbox", "feeds"]


def trio():
    tm = mk()
    bcn = mk(source="bcn_opendata", title="Concert: Peggy Gou", artists=[], venue="Sala Razzmatazz",
             url=None, source_ids={"bcn:a"}, sources_urls={"BCN Open Data": ""}, status="unknown",
             time_unknown=True, start=datetime(2026, 11, 21, 0, 0, tzinfo=MADRID),
             lat=41.39778, lon=2.19167)
    inbox = mk(source="inbox", title="Peggy Gou", venue=None, city="Barcelona", source_ids={"inbox:z"},
               url="https://ra.co/events/1", sources_urls={"Resident Advisor email": "https://ra.co/events/1"},
               status="unknown")
    return tm, bcn, inbox


def test_merge_tm_bcn_inbox(cfg):
    tm, bcn, inbox = trio()
    out = dedupe([inbox, bcn, tm], PRIO, cfg.artists, cfg.venues)
    assert len(out) == 1
    m = out[0]
    assert m.url == "https://t.example/1"  # highest-priority url
    assert m.source == "ticketmaster"
    assert m.source_ids == {"tm:1", "bcn:a", "inbox:z"}
    assert list(m.sources_urls) == ["Ticketmaster", "BCN Open Data", "Resident Advisor email"]
    assert m.sources_urls["Resident Advisor email"] == "https://ra.co/events/1"
    assert m.start.hour == 23 and not m.time_unknown  # precise time from TM
    assert m.lat == 41.39778 and m.status == "onsale"


def test_uid_is_stable_across_runs_and_input_order(cfg):
    a = dedupe(list(trio()), PRIO, cfg.artists, cfg.venues)[0].uid
    b = dedupe(list(reversed(trio())), PRIO, cfg.artists, cfg.venues)[0].uid
    assert a == b and len(a) == 16
    assert key(mk(), cfg.artists, cfg.venues) == a


def test_proximity_rule_merges_without_similar_venue_names(cfg):
    tm = mk(lat=41.39778, lon=2.19167)
    bcn = mk(source="bcn_opendata", title="Concert: Peggy Gou", artists=[], venue="Carrer de Pamplona 88",
             lat=41.3979, lon=2.1917, source_ids={"bcn:a"}, sources_urls={"BCN Open Data": ""})
    assert len(dedupe([tm, bcn], PRIO, cfg.artists)) == 1
    far = mk(source="bcn_opendata", title="Concert: Peggy Gou", artists=[], venue="Other",
             lat=41.42, lon=2.20, source_ids={"bcn:b"})
    assert len(dedupe([tm, far], PRIO, cfg.artists)) == 2


def test_venue_alias_canonicalisation_and_different_days(cfg):
    a = mk(venue="Razzmatazz")
    b = mk(venue="The Loft", source="bcn_opendata", source_ids={"bcn:q"})
    assert len(dedupe([a, b], PRIO, cfg.artists, cfg.venues)) == 1
    c = mk(start=datetime(2026, 11, 22, 23, 0, tzinfo=MADRID), source_ids={"tm:2"})
    assert len(dedupe([a, c], PRIO, cfg.artists, cfg.venues)) == 2


def test_worst_status_wins_and_undated_never_merge(cfg):
    a = mk()
    b = mk(source="bcn_opendata", status="canceled", source_ids={"bcn:c"}, sources_urls={"BCN Open Data": ""})
    assert dedupe([a, b], PRIO, cfg.artists)[0].status == "canceled"
    u1 = mk(start=None, source="feeds", source_ids={"feed:1"})
    u2 = mk(start=None, source="feeds", source_ids={"feed:2"})
    assert len(dedupe([u1, u2], PRIO, cfg.artists)) == 2
    assert key(u1) != key(u2) and key(u1) == key(u1)
