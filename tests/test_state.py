from datetime import datetime, timedelta, timezone

from gigwatch.models import CHANGE, MADRID, NEW, ON_SALE, REMINDER_1D, REMINDER_7D
from gigwatch.state import (State, collect_digest, diff, digest_due, latest_digest_instant,
                            mark_digested, parse_iso)
from tests.conftest import NOW, mk

TM = {"ticketmaster"}


def must(**kw):
    kw.setdefault("uid", "u1")
    kw.setdefault("tier", "must-see")
    return mk(**kw)


def kinds(res):
    return [a.kind for a in res.actions]


def test_new_must_see_alerts_once():
    st = State()
    r1 = diff(st, [must()], NOW, cfg_(), TM)
    assert kinds(r1) == [NEW]
    assert st.events["u1"]["notified_tier"] == "must-see"
    r2 = diff(st, [must()], NOW + timedelta(hours=3), cfg_(), TM)
    assert kinds(r2) == []


def cfg_():
    from gigwatch.config import Config
    return Config()


def test_change_start_and_roundtrip_through_json(tmp_path):
    st = State()
    diff(st, [must()], NOW, cfg_(), TM)
    p = tmp_path / "s.json"
    st.save(p)
    st2 = State.load(p)
    new_start = datetime(2026, 11, 22, 23, 0, tzinfo=MADRID)
    r = diff(st2, [must(start=new_start)], NOW + timedelta(hours=3), cfg_(), TM)
    assert kinds(r) == [CHANGE] and r.actions[0].change == "start"


def test_canceled_postponed_rescheduled_and_url_appearing():
    for status in ("canceled", "postponed", "rescheduled"):
        st = State()
        diff(st, [must()], NOW, cfg_(), TM)
        r = diff(st, [must(status=status)], NOW, cfg_(), TM)
        assert [a.change for a in r.actions] == [status]
    st = State()
    diff(st, [must(url=None, sources_urls={})], NOW, cfg_(), TM)
    r = diff(st, [must()], NOW, cfg_(), TM)
    assert [a.change for a in r.actions] == ["url"]


def test_other_changes_update_snapshot_silently():
    st = State()
    diff(st, [must()], NOW, cfg_(), TM)
    r = diff(st, [must(venue="Elsewhere")], NOW, cfg_(), TM)
    assert r.actions == [] and st.events["u1"]["snapshot"]["venue"] == "Elsewhere"


def test_new_event_already_canceled_is_silent():
    st = State()
    r = diff(st, [must(status="canceled")], NOW, cfg_(), TM)
    assert r.actions == []
    assert diff(st, [must(status="canceled")], NOW, cfg_(), TM).actions == []


def test_tier_upgrade_counts_as_new():
    st = State()
    diff(st, [mk(uid="u1", tier="maybe")], NOW, cfg_(), TM)
    assert st.events["u1"]["digested"] is False
    r = diff(st, [must()], NOW, cfg_(), TM)
    assert kinds(r) == [NEW]


def test_maybe_goes_to_digest_and_flips_after_send():
    st = State()
    r = diff(st, [mk(uid="m1", tier="maybe", title="Khruangbin")], NOW, cfg_(), TM)
    assert r.actions == []
    c = collect_digest(st)
    assert [u for u, _ in c.maybe] == ["m1"]
    mark_digested(st, c)
    assert collect_digest(st).is_empty()


def test_short_notice_flag():
    st = State()
    soon = must(start=NOW.astimezone(MADRID) + timedelta(hours=30))
    assert diff(st, [soon], NOW, cfg_(), TM).actions[0].short_notice is True
    st = State()
    assert diff(st, [must()], NOW, cfg_(), TM).actions[0].short_notice is False


def test_reminders_7d_then_1d_each_once():
    st = State()
    start = NOW.astimezone(MADRID) + timedelta(days=10)
    diff(st, [must(start=start)], NOW, cfg_(), TM)
    assert kinds(diff(st, [must(start=start)], NOW + timedelta(days=2), cfg_(), TM)) == []
    t = NOW + timedelta(days=3, hours=1)  # 6.96 days left
    assert kinds(diff(st, [must(start=start)], t, cfg_(), TM)) == [REMINDER_7D]
    assert kinds(diff(st, [must(start=start)], t + timedelta(hours=3), cfg_(), TM)) == []
    t = NOW + timedelta(days=9, hours=2)
    assert kinds(diff(st, [must(start=start)], t, cfg_(), TM)) == [REMINDER_1D]
    assert kinds(diff(st, [must(start=start)], t + timedelta(hours=3), cfg_(), TM)) == []


def test_found_late_marks_reminders_silently():
    st = State()
    start = NOW.astimezone(MADRID) + timedelta(hours=20)
    r = diff(st, [must(start=start)], NOW, cfg_(), TM)
    assert kinds(r) == [NEW]
    assert sorted(st.events["u1"]["reminders_sent"]) == ["1d", "7d"]
    assert kinds(diff(st, [must(start=start)], NOW + timedelta(hours=3), cfg_(), TM)) == []


def test_quiet_suppresses_reminders_not_first_sight():
    from gigwatch.config import Config
    cfg = Config(quiet=True)
    st = State()
    start = NOW.astimezone(MADRID) + timedelta(days=10)
    assert kinds(diff(st, [must(start=start)], NOW, cfg, TM)) == [NEW]
    assert kinds(diff(st, [must(start=start)], NOW + timedelta(days=4), cfg, TM)) == []
    assert "7d" in st.events["u1"]["reminders_sent"]


def test_on_sale_ping():
    st = State()
    onsale = NOW + timedelta(days=1)
    ev = lambda: must(onsale_at=onsale, status="offsale")  # noqa: E731
    diff(st, [ev()], NOW, cfg_(), TM)
    later = NOW + timedelta(days=1, hours=1)
    assert kinds(diff(st, [must(onsale_at=onsale, status="onsale")], later, cfg_(), TM)) == [ON_SALE]
    assert kinds(diff(st, [must(onsale_at=onsale, status="onsale")], later, cfg_(), TM)) == []
    # already on sale at first sight: no ping
    st = State()
    diff(st, [must(onsale_at=NOW - timedelta(days=5))], NOW, cfg_(), TM)
    assert "onsale" in st.events["u1"]["reminders_sent"]


def test_missing_lifecycle_and_possibly_removed_once():
    st = State()
    diff(st, [mk(uid="m1", tier="maybe")], NOW, cfg_(), TM)
    c = collect_digest(st)
    mark_digested(st, c)
    for i in range(1, 4):
        diff(st, [], NOW + timedelta(hours=3 * i), cfg_(), TM)
    assert st.events["m1"]["missed_runs"] == 3 and st.events["m1"]["possibly_removed"]
    c = collect_digest(st)
    assert [u for u, _ in c.removed] == ["m1"]
    mark_digested(st, c)
    assert collect_digest(st).is_empty()
    diff(st, [mk(uid="m1", tier="maybe")], NOW + timedelta(days=1), cfg_(), TM)  # reappears
    assert st.events["m1"]["missed_runs"] == 0 and not st.events["m1"]["possibly_removed"]


def test_missing_skipped_when_source_failed_or_unstructured():
    st = State()
    diff(st, [mk(uid="m1", tier="maybe")], NOW, cfg_(), TM)
    diff(st, [], NOW, cfg_(), {"bcn_opendata"})  # TM failed this run
    assert st.events["m1"]["missed_runs"] == 0
    diff(st, [mk(uid="f1", tier="heads-up", start=None, source="feeds", source_ids={"feed:1"})], NOW, cfg_(), TM)
    diff(st, [], NOW, cfg_(), TM | {"feeds"})
    assert st.events["f1"]["missed_runs"] == 0


def test_prune_old_and_undated():
    st = State()
    old = mk(uid="old", tier="maybe", start=NOW.astimezone(MADRID) + timedelta(days=1))
    und = mk(uid="und", tier="heads-up", start=None, source="feeds", source_ids={"feed:1"})
    diff(st, [old, und], NOW, cfg_(), TM)
    diff(st, [], NOW + timedelta(days=9), cfg_(), set())
    assert "old" not in st.events and "und" in st.events
    diff(st, [], NOW + timedelta(days=61), cfg_(), set())
    assert "und" not in st.events


def test_revert_restores_previous_record():
    st = State()
    r = diff(st, [must()], NOW, cfg_(), TM)
    r.revert(st, "u1")
    assert "u1" not in st.events
    diff(st, [must()], NOW, cfg_(), TM)
    r2 = diff(st, [must(status="canceled")], NOW, cfg_(), TM)
    r2.revert(st, "u1")
    assert st.events["u1"]["snapshot"]["status"] == "onsale"


def test_digest_timing_across_dst_end():
    cfg = cfg_()
    # DST ended Sun 2026-10-25. Monday 09:00 Madrid is 07:00Z before and 08:00Z after.
    d = latest_digest_instant(datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc), cfg)
    assert d.astimezone(timezone.utc) == datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    st = State(last_digest_at="2026-10-19T07:05:00Z")
    assert digest_due(st, datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc), cfg)
    assert not digest_due(st, datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc), cfg)  # 08:30 CET
    # DST start 2027-03-28: Monday 03-29 09:00 CEST = 07:00Z
    d = latest_digest_instant(datetime(2027, 3, 29, 7, 30, tzinfo=timezone.utc), cfg)
    assert d.astimezone(timezone.utc) == datetime(2027, 3, 29, 7, 0, tzinfo=timezone.utc)


def test_digest_due_rules():
    cfg = cfg_()
    assert digest_due(State(), NOW, cfg)  # never sent
    assert not digest_due(State(last_digest_at="2026-11-02T09:00:00Z"), NOW, cfg)  # Mon 10:00 CET sent after 09:00
    assert digest_due(State(last_digest_at="2026-10-30T09:00:00Z"), NOW, cfg)
    assert parse_iso("2026-11-02T09:00:00Z").tzinfo is not None
