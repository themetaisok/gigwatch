import imaplib
from datetime import datetime

from gigwatch.config import Config
from gigwatch.models import MADRID
from gigwatch.sources.inbox import (InboxSource, clean_url, choose_link, find_date, parse_message,
                                    source_name_from_sender)
from gigwatch.state import State
from tests.conftest import FIX, NOW

RA = (FIX / "ra_email.eml").read_bytes()
DICE = (FIX / "dice_email.eml").read_bytes()


def test_sender_names():
    assert source_name_from_sender("ra.co") == "Resident Advisor"
    assert source_name_from_sender("mail.dice.fm") == "DICE"
    assert source_name_from_sender("songkick.com") == "Songkick"
    assert source_name_from_sender("example.org") == "example.org"


def test_clean_url_keeps_only_scheme_host_path():
    assert clean_url("https://ra.co/e/1?utm_source=a&fbclid=b&mc_cid=c&ref=mail#frag") == "https://ra.co/e/1"


def test_ra_email_with_artist_and_date(cfg):
    ev = parse_message(RA, cfg, NOW)
    assert ev.source == "inbox" and ev.artists == ["Peggy Gou"] and ev.title == "Peggy Gou"
    assert ev.start == datetime(2026, 11, 21, 23, 0, tzinfo=MADRID) and not ev.time_unknown
    assert ev.url == "https://ra.co/events/1234567"  # ticket link beats unsubscribe/tracking; no query
    assert ev.city == "Barcelona"
    assert list(ev.sources_urls) == ["Resident Advisor email"]
    (sid,) = ev.source_ids
    assert sid.startswith("inbox:") and len(sid) == len("inbox:") + 40
    assert "Get tickets" not in repr(ev) and "Unsubscribe" not in repr(ev)  # no body text stored


def test_dice_email_without_date_is_immediate_candidate(cfg):
    from gigwatch.match import assign_tier
    ev = parse_message(DICE, cfg, NOW)
    assert ev.artists == ["Dixon"] and ev.start is None
    assert ev.raw_tier_hint == "must-see" and assign_tier(ev, cfg) == "must-see"
    assert ev.url == "https://dice.fm/artist/dixon"  # query stripped, preferences link ignored
    assert list(ev.sources_urls) == ["DICE email"]


def test_unknown_artist_email_gets_generic_title():
    ev = parse_message(DICE, Config(), NOW)
    assert ev.artists == [] and ev.title == "New DICE notification"


def test_choose_link_prefers_platform_host_then_keywords():
    links = [("https://other.example/buy-tickets", "Buy"), ("https://ra.co/about", "About"),
             ("https://ra.co/events/9", "Get tickets"), ("https://ra.co/unsubscribe", "Unsubscribe")]
    assert choose_link(links, "ra.co") == "https://ra.co/events/9"
    assert choose_link([("https://x.example/a", "a"), ("https://x.example/event/1", "b")], "nowhere.com") == "https://x.example/event/1"
    assert choose_link([("mailto:a@b.c", "x")], "ra.co") is None


def test_find_date_variants():
    assert find_date("on 2026-12-05 at the club", NOW)[0].day == 5
    assert find_date("Friday 5th December 2026 22:30", NOW)[0].hour == 22
    d = find_date("December 5", NOW)  # no year: next occurrence
    assert d[0].year == 2026 and d[1] is True
    assert find_date("Nov 1", NOW)[0].year == 2027  # already passed this year
    assert find_date("no date here", NOW) is None


class FakeIMAP:
    def __init__(self, messages, fail_on=()):
        self.messages, self.stored, self.fail_on, self.logged_out = messages, [], set(fail_on), False

    def __call__(self, host, **kw):
        assert kw.get('timeout') == 60
        assert host == "imap.gmail.com"
        return self

    def login(self, u, p):
        assert (u, p) == ("me@gmail.com", "app-pass")

    def select(self, box):
        assert box == "gigwatch"
        return "OK", [b"2"]

    def search(self, charset, crit):
        assert crit == "UNSEEN"
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, spec):
        assert spec == "(BODY.PEEK[])"
        if int(num) in self.fail_on:
            raise imaplib.IMAP4.error("boom")
        return "OK", [(b"1 (BODY[] {1}", self.messages[int(num) - 1]), b")"]

    def store(self, num, cmd, flags):
        self.stored.append((int(num), cmd, flags))

    def logout(self):
        self.logged_out = True


ENV = {"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "app-pass"}


def test_inbox_fetch_does_not_mark_seen_finalize_does(cfg):
    fake = FakeIMAP([RA, DICE], fail_on={2})
    st = State()
    src = InboxSource(ENV, st, imap_factory=fake)
    evs = src.fetch(cfg, NOW)
    assert len(evs) == 1 and fake.stored == [] and st.inbox_seen == []  # nothing flagged yet
    (sid,) = src.pending_ids()
    assert src.finalize({sid}) == 1
    assert fake.stored == [(1, "+FLAGS", "\\Seen")] and fake.logged_out
    assert st.inbox_seen == [sid]


def test_inbox_finalize_skips_mail_that_was_not_done(cfg):
    fake = FakeIMAP([RA])
    st = State()
    src = InboxSource(ENV, st, imap_factory=fake)
    src.fetch(cfg, NOW)
    assert src.finalize(set()) == 0 and fake.stored == [] and fake.logged_out


def test_inbox_dry_run_never_marks_seen(cfg):
    fake = FakeIMAP([RA])
    st = State()
    src = InboxSource(ENV, st, mark_seen=False, imap_factory=fake)
    (ev,) = src.fetch(cfg, NOW)
    assert src.finalize(src.pending_ids()) == 0 and fake.stored == [] and st.inbox_seen == []


def test_inbox_known_mail_is_flagged_but_not_reprocessed(cfg):
    fake = FakeIMAP([RA])
    st = State()
    src = InboxSource(ENV, st, imap_factory=fake)
    (ev,) = src.fetch(cfg, NOW)
    src.finalize(src.pending_ids())
    fake2 = FakeIMAP([RA])  # flag was lost: mail shows up unread again
    src2 = InboxSource(ENV, st, imap_factory=fake2)
    assert src2.fetch(cfg, NOW) == [] and src2.pending_ids()


def test_inbox_enabled_requires_both_env_vars():
    s = InboxSource(ENV, State())
    assert s.enabled(ENV) and not s.enabled({"GMAIL_USER": "x"}) and not s.enabled({})


def test_tracking_links_are_never_published(cfg):
    html = ('<p>Dixon Sat 21 Nov 2026 Barcelona</p>'
            '<a href="https://click.dice.fm/ls/click?upn=SECRET123">Get tickets</a>')
    raw = (b"From: D <hi@dice.fm>\r\nSubject: Dixon\r\nMessage-ID: <t@dice.fm>\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n" + html.encode())
    ev = parse_message(raw, cfg, NOW)
    assert ev.url == "https://dice.fm/" and "SECRET" not in repr(ev)
    assert parse_message(raw.replace(b"@dice.fm", b"@unknown.example"), cfg, NOW).url is None
