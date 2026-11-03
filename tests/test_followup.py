"""Tests for the review follow-up decisions (title_match, uid reconciliation, splitting, ...)."""
import copy
from datetime import datetime, timedelta

from gigwatch import cli
from gigwatch.config import Artist, Config, parse_config
from gigwatch.dedupe import dedupe, key
from gigwatch.match import assign_tier, match_artist
from gigwatch.models import MADRID, NEW
from gigwatch.notify_telegram import split_message
from gigwatch.sources.feeds import FeedsSource
from gigwatch.sources.inbox import find_date, parse_message
from gigwatch.state import State, diff, iso_utc
from tests.conftest import NOW, mk
from tests.test_review import ListSource, Collect, PRIO, eml, write_cfg


def test_title_match_modes():
    ev = mk(title="Jungle live", artists=[], source="bcn_opendata")
    assert assign_tier(ev, Config(artists=[Artist("Jungle")])) == "must-see"
    assert assign_tier(ev, Config(artists=[Artist("Jungle", title_match="maybe")])) == "maybe"
    assert assign_tier(ev, Config(artists=[Artist("Jungle", title_match="off")])) is None
    lineup = mk(title="Festival night", artists=["Jungle"])  # lineup hits are never downgraded
    assert assign_tier(lineup, Config(artists=[Artist("Jungle", title_match="maybe")])) == "must-see"
    assert match_artist(mk(title="Âme x", artists=[]), [Artist("Âme", title_match="off")]) == []


def test_title_match_config_validation():
    import pytest
    from gigwatch.config import ConfigError
    base = {"home": {"lat": 1, "lon": 2, "radius_km": 5}}
    assert parse_config(base, [{"name": "X", "title_match": "off"}]).artists[0].title_match == "off"
    with pytest.raises(ConfigError):
        parse_config(base, [{"name": "X", "title_match": "sometimes"}])


def test_inbox_title_only_hit_of_downgraded_artist_stays_maybe(cfg):
    cfg.artists = [Artist("Dixon", title_match="maybe")]
    ev = parse_message(eml("Dixon is playing", "<p>Dixon Sat 21 Nov 2026 23:00 Barcelona</p>"), cfg, NOW)
    assert ev.artists == [] and ev.title == "Dixon" and assign_tier(ev, cfg) == "maybe"


def test_short_accented_name_matches_raw_title_only():
    a = [Artist("Âme", aliases=["Ame"])]
    assert match_artist(mk(title="Nitsa: Âme", artists=[]), a)
    assert match_artist(mk(title="NITSA: ÂME + guests", artists=[]), a)
    assert not match_artist(mk(title="Nitsa: ame", artists=[]), a)
    assert not match_artist(mk(title="Amelie Lens", artists=[]), a)


def test_uid_reused_via_key_when_source_ids_differ(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    a = dedupe([mk(source_ids={"tm:1"})], PRIO, cfg.artists, cfg.venues)
    a[0].tier = "must-see"
    first = diff(st, a, NOW, cfg, {"ticketmaster"})
    uid = a[0].uid
    # the show comes back under a different source id (re-listed event)
    b = dedupe([mk(source_ids={"tm:999"}, venue="Sala Razzmatazz")], PRIO, cfg.artists, cfg.venues)
    b[0].tier = "must-see"
    assert key(b[0], cfg.artists, cfg.venues) == key(a[0], cfg.artists, cfg.venues)
    res = diff(st, b, NOW + timedelta(hours=3), cfg, {"ticketmaster"})
    assert [x.kind for x in first.actions] == [NEW] and res.actions == [] and b[0].uid == uid
    assert len(st.events) == 1


def test_merged_duplicate_records_are_collapsed(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    tm = mk(source_ids={"tm:1"})
    bcn = mk(source="bcn_opendata", source_ids={"bcn:1"}, venue="Elsewhere 1", artists=[], title="Peggy Gou",
             sources_urls={"BCN Open Data": ""})
    for e in (tm, bcn):
        e.tier = "must-see"
    tm.uid, bcn.uid = "A", "B"
    diff(st, [tm, bcn], NOW, cfg, {"ticketmaster", "bcn_opendata"})
    assert set(st.events) == {"A", "B"}
    merged = mk(source_ids={"tm:1", "bcn:1"})
    merged.tier, merged.uid = "must-see", "A"
    res = diff(st, [merged], NOW + timedelta(hours=3), cfg, {"ticketmaster", "bcn_opendata"})
    assert set(st.events) == {"A"} and res.actions == []


def test_find_date_skips_past_and_handles_spanish_and_catalan():
    assert find_date("Enviado 30 oct 2026. Concierto: sábado 21 de noviembre de 2026, 23:00h", NOW)[0].day == 21
    d = find_date("dissabte 5 de desembre 22:30", NOW)
    assert (d[0].month, d[0].day, d[0].hour, d[0].minute) == (12, 5, 22, 30)
    assert find_date("martes 24 de noviembre", NOW)[0].day == 24
    assert find_date("1 de gener de 2025", NOW) is None


def test_non_region_city_caps_email_and_feed(cfg):
    ev = parse_message(eml("Peggy Gou live", "<p>Peggy Gou Sat 21 Nov 2026 23:00 Valencia</p>"), cfg, NOW)
    assert ev.city == "Valencia" and ev.raw_tier_hint == "heads-up"
    assert assign_tier(ev, cfg) == "heads-up"
    both = parse_message(eml("Peggy Gou live", "<p>Peggy Gou Sat 21 Nov 2026 Madrid and Barcelona</p>"), cfg, NOW)
    assert both.city == "Barcelona" and assign_tier(both, cfg) == "must-see"
    cfg.feeds = [{"name": "F", "url": "https://x.test", "immediate": True}]
    body = ("<?xml version='1.0'?><rss version='2.0'><channel><item><guid>7</guid>"
            "<title>Peggy Gou plays Madrid</title><link>https://n.test/7</link></item></channel></rss>")
    (e,) = FeedsSource(cfg, State())._rss(body, cfg.feeds[0], cfg, NOW)
    assert assign_tier(e, cfg) == "heads-up"


def test_inbox_headliner_prefers_subject_then_body_start(cfg):
    ev = parse_message(eml("New show announced", "<p>Dixon is coming to Barcelona 21 Nov 2026</p>"), cfg, NOW)
    assert ev.artists == ["Dixon"]
    long_body = "<p>" + "filler " * 200 + "Peggy Gou</p>"  # beyond the first 600 characters
    assert parse_message(eml("Weekly picks", long_body), cfg, NOW).artists == []


def test_ticketmaster_merge_keeps_uncapped_tier_when_mail_is_attached(cfg):
    mail = parse_message(eml("Peggy Gou", "<p>Peggy Gou Sat 21 Nov 2026 23:00 Madrid</p>"), cfg, NOW)
    assert mail.raw_tier_hint == "heads-up"
    tm = mk()
    (m,) = dedupe([tm, mail], PRIO, cfg.artists, cfg.venues)
    assert m.raw_tier_hint is None and assign_tier(m, cfg) == "must-see"


def test_split_never_cuts_inside_a_tag():
    line = "x " * 1000 + '<a href="https://example.test/a/very/long/path?with=query&and=more">link</a> ' + "y " * 1500
    chunks = split_message(line, 4000)
    assert len(chunks) >= 2 and all(len(c) <= 4000 for c in chunks)
    for c in chunks:
        assert c.count("<") == c.count(">")
    assert " ".join(chunks).replace("  ", " ").split() == line.split()
    one_word = split_message("<b>" + "z" * 3990 + "</b>" + "q" * 100, 4000)
    assert all(len(c) <= 4000 for c in one_word) and "".join(one_word) == "<b>" + "z" * 3990 + "</b>" + "q" * 100


def test_client_stops_after_failed_chunk_and_tracks_progress():
    import responses
    from gigwatch.notify_telegram import TelegramClient
    url = "https://api.telegram.org/botT/sendMessage"
    with responses.RequestsMock() as r:
        r.add(responses.POST, url, status=200, json={})
        r.add(responses.POST, url, status=400, json={})
        c = TelegramClient("T", "1", sleep=lambda s: None, clock=lambda: 0.0)
        assert c.send("\n".join(["a" * 3000, "b" * 3000, "c" * 3000])) is False
        assert (c.chunks_sent, c.chunks_total) == (1, 3) and len(r.calls) == 2


def test_first_run_digest_not_sent_if_summary_failed(tmp_path):
    d = write_cfg(tmp_path, "- name: Peggy Gou\n- {name: Khruangbin, tier: maybe}\n")
    evs = [mk(), mk(title="Khruangbin", artists=["Khruangbin"], source_ids={"tm:2"})]
    n = Collect(ok=False)
    code, rep = cli.run(d, tmp_path / "s.json", tmp_path / "o", now=NOW, env={}, notifier=n,
                        sources=[ListSource("ticketmaster", evs)])
    assert code == 1 and rep.first_run
    assert len(n.messages) == 1 and "First run" in n.messages[0]  # no digest after a failed summary
    st = State.load(tmp_path / "s.json")
    assert st.last_digest_at is None and all(r["notified_tier"] != "must-see" for r in st.events.values())


def test_render_failure_exit_code_and_state_saved(tmp_path, monkeypatch):
    d = write_cfg(tmp_path)
    monkeypatch.setattr(cli, "write_outputs", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    code, _ = cli.run(d, tmp_path / "s.json", tmp_path / "o", now=NOW, env={}, notifier=Collect(),
                      sources=[ListSource("ticketmaster", [mk()])])
    assert code == 1 and State.load(tmp_path / "s.json").events


def test_reminder_fires_for_must_see_record_no_source_returned(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    ev = mk(tier="must-see")
    ev.uid = "u9"
    ev.start = NOW.astimezone(MADRID) + timedelta(days=10)
    diff(st, [ev], NOW, cfg, {"ticketmaster"})
    acts = diff(st, [], NOW + timedelta(days=4), cfg, set()).actions  # every source down
    assert [a.kind for a in acts] == ["REMINDER_7D"]


def test_undated_alert_then_dated_listing_is_a_date_change_not_a_second_new(cfg):
    from gigwatch.models import CHANGE
    st = State(last_digest_at=iso_utc(NOW))
    mail = mk(source="inbox", start=None, venue=None, city="Barcelona", source_ids={"inbox:1"},
              raw_tier_hint="must-see", url=None, sources_urls={})
    diff(st, [_t(mail, cfg)], NOW, cfg, {"inbox"})
    tm = mk(source_ids={"tm:5"})
    acts = diff(st, [_t(tm, cfg)], NOW + timedelta(hours=3), cfg, {"ticketmaster"}).actions
    assert [(a.kind, a.change) for a in acts] == [(CHANGE, "date")] and len(st.events) == 1


def test_ambiguous_dateless_match_is_not_guessed(cfg):
    st = State(last_digest_at=iso_utc(NOW))
    for i in (1, 2):
        diff(st, [_t(mk(source="inbox", start=None, venue=None, source_ids={f"inbox:{i}"},
                        raw_tier_hint="must-see", url=None, sources_urls={}), cfg)], NOW, cfg, {"inbox"})
    assert len(st.events) == 2
    acts = diff(st, [_t(mk(source_ids={"tm:5"}), cfg)], NOW, cfg, {"ticketmaster"}).actions
    assert [a.kind for a in acts] == [NEW]


def _t(ev, cfg):
    ev.tier = assign_tier(ev, cfg)
    if not ev.uid:
        ev.uid = key(ev, cfg.artists, cfg.venues)
    return ev


def test_first_run_flag_defaults_for_old_state_files(tmp_path):
    assert State().is_first_run()
    assert not State(last_digest_at="2026-11-01T00:00:00Z").is_first_run()
    p = tmp_path / "s.json"
    State(events={"a": {}}).save(p)
    assert not State.load(p).is_first_run()  # flag missing -> events exist -> not a first run
    explicit = State(events={"a": {}}, first_run_done=False)
    explicit.save(p)
    assert State.load(p).is_first_run()
