import imaplib
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from gigwatch.cli import main, run
from gigwatch.sources.bcn_opendata import CKAN_URL
from gigwatch.sources.ticketmaster import TM_URL
from gigwatch.state import State, iso_utc
from tests.conftest import FIX, NOW
from tests.test_inbox import FakeIMAP

CFG = FIX / "config"
ENV = {"TICKETMASTER_API_KEY": "KEY", "GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "app-pass",
       "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}


class Collect:
    def __init__(self):
        self.messages = []

    def send(self, text, preview=True):
        self.messages.append((text, preview))
        return True


@pytest.fixture
def world(monkeypatch):
    fake = FakeIMAP([(FIX / "ra_email.eml").read_bytes(), (FIX / "dice_email.eml").read_bytes()])
    fake.search = lambda charset, crit: ("OK", [b" ".join(str(i + 1).encode() for i in range(2) if (i + 1) not in {s[0] for s in fake.stored})])
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        calls = []

        def tm(request):
            q = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
            calls.append(q)
            if "geoPoint" in q:
                return 200, {}, (FIX / f"tm_page{q['page']}.json").read_text()
            return 200, {}, json.dumps({"page": {"size": 50, "totalElements": 0, "totalPages": 0, "number": 0}})

        rsps.add_callback(responses.GET, TM_URL, callback=tm)
        ckan = json.loads((FIX / "ckan_package_show.json").read_text())
        rsps.add(responses.GET, CKAN_URL.split("?")[0], json=ckan)
        rsps.add(responses.GET, ckan["result"]["resources"][1]["url"], body=(FIX / "agenda_cultural_ca.csv").read_bytes())
        rsps.add(responses.GET, "https://alerts.example.test/feed.xml", body=(FIX / "google_alert.xml").read_text())
        rsps.add(responses.GET, "https://calendar.example.test/venue.ics", body=(FIX / "venue_calendar.ics").read_text())
        yield fake, calls


def test_dry_run_first_run_prints_and_writes_nothing(world, tmp_path, capsys):
    state, out = tmp_path / "state.json", tmp_path / "out"
    code, rep = run(CFG, state, out, dry_run=True, now=NOW, env=ENV, sleep=lambda s: None)
    assert code == 0 and not state.exists() and not out.exists()
    assert list(tmp_path.iterdir()) == []
    fake, calls = world
    assert fake.stored == []  # dry run never marks mail as seen
    printed = capsys.readouterr().out
    assert "First run: found 5 must-see shows" in printed
    for needle in ("Peggy Gou", "Floating Points", "Dixon"):
        assert needle in printed
    assert "gigwatch digest" in printed and "Primavera Sound 2027" in printed
    assert "Heads-up" in printed, printed
    assert "adds second Barcelona date" in printed
    assert "Dixon Brothers" not in printed and "WiZink" not in printed and "Launch party" not in printed
    assert rep.sent == 2 and rep.first_run
    assert rep.ok == ["ticketmaster", "bcn_opendata", "inbox", "feeds"] and rep.failed == []
    assert (rep.must, rep.maybe, rep.heads) == (6, 1, 1)  # incl. canceled Dixon (silent) and the undated DICE mail
    assert rep.summary.startswith("sources ok=[ticketmaster,bcn_opendata,inbox,feeds] failed=[] fetched=")
    assert all(c["locale"] == "*" for c in calls)


def test_real_run_alerts_then_repeat_run_is_silent(world, tmp_path):
    state, out = tmp_path / "state.json", tmp_path / "out"
    seed = State(last_digest_at=iso_utc(NOW))  # not a first run: individual alerts, digest not due
    seed.save(state)
    n1 = Collect()
    code, rep = run(CFG, state, out, now=NOW, env=ENV, notifier=n1, sleep=lambda s: None)
    assert code == 0
    texts = [t for t, _ in n1.messages]
    assert len(texts) == 5 and all("MUST-SEE" in t for t in texts)
    peggy = next(t for t in texts if "Peggy Gou" in t)
    assert "Sat 21 Nov 2026 · 23:00" in peggy and "Razzmatazz, Barcelona" in peggy
    assert "via Ticketmaster" in peggy and "(+ also: BCN Open Data, Resident Advisor email)" in peggy
    assert any("Dixon" in t and "Sala Apolo" in t for t in texts)  # from the ICS calendar feed
    assert any("Dixon" in t and "Date TBA" in t and "via DICE email" in t for t in texts)  # undated mail, still instant
    assert any("Sat 12 Dec 2026\n" in t + "\n" for t in texts if "Floating Points" in t)  # time unknown
    ics = (out / "gigwatch.ics").read_text()
    assert ics.count("BEGIN:VEVENT") == 6 and "STATUS:CANCELLED" in ics  # 5 must-see + Primavera
    assert "Peggy Gou" in (out / "index.html").read_text()
    st = State.load(state)
    assert len([r for r in st.events.values() if r["notified_tier"] == "must-see"]) == 6
    assert st.inbox_seen and "?" not in "".join(
        u for r in st.events.values() for u in [r["snapshot"]["url"]] if "dice.fm" in u or "ra.co" in u)

    n2 = Collect()
    code, rep2 = run(CFG, state, out, now=NOW + timedelta(hours=3), env=ENV, notifier=n2, sleep=lambda s: None)
    assert code == 0 and n2.messages == [] and rep2.sent == 0
    assert rep2.written == []  # unchanged run makes no output diff


def test_dry_run_without_network_fails_gracefully(tmp_path, capsys):
    """No mocks: every request fails. The run must still exit 0 and print the summary."""
    with responses.RequestsMock(assert_all_requests_are_fired=False):
        code, rep = run(CFG, tmp_path / "s.json", tmp_path / "o", dry_run=True, now=NOW, env={},
                        sleep=lambda s: None)
    assert code == 0 and rep.failed == ["bcn_opendata", "feeds"] and rep.ok == []
    out = capsys.readouterr().out
    assert "no structured source could be fetched (bcn_opendata)" in out  # health warning, enabled sources only
    assert "failed=[bcn_opendata,feeds]" in out and "sent=1" in out


def test_since_days_limits_lookahead(world, tmp_path):
    n = Collect()
    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    code, rep = run(CFG, tmp_path / "s.json", tmp_path / "o", since_days=25, now=NOW, env=ENV, notifier=n,
                    sleep=lambda s: None)
    texts = " ".join(t for t, _ in n.messages)
    assert "Peggy Gou" in texts and "Floating Points" not in texts and "Sat 19 Dec" not in texts


def test_overflow_cap_sends_rest_as_one_compact_list(world, tmp_path):
    State(last_digest_at=iso_utc(NOW - timedelta(days=8))).save(tmp_path / "s.json")  # digest due
    import shutil
    shutil.copytree(CFG, tmp_path / "cfg")
    p = tmp_path / "cfg" / "settings.yaml"
    p.write_text(p.read_text().replace("max_immediate: 15", "max_immediate: 2"))
    n = Collect()
    run(tmp_path / "cfg", tmp_path / "s.json", tmp_path / "o", now=NOW, env=ENV, notifier=n, sleep=lambda s: None)
    singles = [t for t, _ in n.messages if t.startswith("\U0001f3af MUST-SEE")]
    lists = [t for t, _ in n.messages if "more must-see shows" in t]
    assert len(singles) == 2 and len(lists) == 1  # the rest follow at once as one compact list
    assert lists[0].count("\u2022") == 3 and "alert cap of 2" in lists[0]


def test_main_run_requires_telegram_unless_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main(["run", "--config", str(CFG), "--state", str(tmp_path / "s.json"), "--out", str(tmp_path / "o")]) == 2


def test_failed_send_still_saves_state_and_exits_nonzero(world, tmp_path):
    class Down:
        def send(self, text, preview=True):
            return False

    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    code, rep = run(CFG, tmp_path / "s.json", tmp_path / "o", now=NOW, env=ENV, notifier=Down(),
                    sleep=lambda s: None)
    fake, _ = world
    assert code == 1 and rep.send_failures >= 5
    assert fake.stored == []  # mail stays unread so the alert is retried
    assert (tmp_path / "s.json").exists() and State.load(tmp_path / "s.json").events is not None


def test_inbox_urls_never_carry_query_strings_into_state_or_docs(world, tmp_path):
    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    run(CFG, tmp_path / "s.json", tmp_path / "o", now=NOW, env=ENV, notifier=Collect(), sleep=lambda s: None)
    blob = (tmp_path / "s.json").read_text() + "".join(p.read_text() for p in (tmp_path / "o").iterdir())
    assert "ra.co/events/1234567" in blob or "dice.fm" in blob
    for token in ("utm_", "fbclid", "mc_cid", "ra.co/events/1234567?", "dice.fm/artist/dixon?"):
        assert token not in blob


def test_only_bcn_active_logs_hint_and_no_telegram_hint(tmp_path, caplog, monkeypatch):
    import logging
    from gigwatch import cli
    monkeypatch.setattr(cli.BcnOpenDataSource, "fetch", lambda self, cfg, now: [])
    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "settings.yaml").write_text("home: {lat: 41.3874, lon: 2.1686, radius_km: 150}\n")
    n = Collect()
    with caplog.at_level(logging.INFO, logger="gigwatch"):
        code, rep = run(d, tmp_path / "s.json", tmp_path / "o", now=NOW, env={}, notifier=n)
    assert code == 0 and rep.ok == ["bcn_opendata"] and n.messages == []
    assert "only the BCN cultural agenda is active" in caplog.text


def test_warning_only_considers_enabled_structured_sources(tmp_path, monkeypatch):
    from gigwatch import cli
    monkeypatch.setattr(cli.BcnOpenDataSource, "fetch", lambda self, cfg, now: [])
    State(last_digest_at=iso_utc(NOW)).save(tmp_path / "s.json")
    n = Collect()
    with responses.RequestsMock(assert_all_requests_are_fired=False):  # feeds fail fast, offline
        run(CFG, tmp_path / "s.json", tmp_path / "o", now=NOW, env={}, notifier=n, sleep=lambda s: None)
    assert not any("no structured source" in t for t, _ in n.messages)  # BCN (enabled) succeeded; TM has no key
