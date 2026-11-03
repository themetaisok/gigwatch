import json
import logging

import pytest
import responses

from gigwatch.models import CHANGE, NEW, ON_SALE, REMINDER_1D, REMINDER_7D
from gigwatch.notify_telegram import (DryRunNotifier, TelegramClient, format_digest, format_event,
                                      format_first_run, split_message)
from gigwatch.state import DigestContent
from tests.conftest import mk

URL = "https://api.telegram.org/botTOKEN/sendMessage"


def client(sleeps):
    return TelegramClient("TOKEN", "42", sleep=sleeps.append, clock=lambda: 1000.0)


def test_format_event_full():
    ev = mk(sources_urls={"Ticketmaster": "https://t.example/1", "BCN Open Data": "", "DICE email": "https://d.example"})
    text = format_event(ev, NEW, 4.0)
    assert text.splitlines() == [
        "\U0001f3af MUST-SEE · <b>Peggy Gou</b>",
        "\U0001f4c5 Sat 21 Nov 2026 · 23:00",
        "\U0001f4cd Razzmatazz, Barcelona",
        '\U0001f39f <a href="https://t.example/1">Tickets</a> · via Ticketmaster',
        "(+ also: BCN Open Data, DICE email)",
    ]


def test_distance_shown_only_above_15km():
    assert "km" not in format_event(mk(), NEW, 15.0)
    assert "· 87 km" in format_event(mk(), NEW, 87.2)


def test_missing_time_and_url_omitted():
    ev = mk(time_unknown=True, url=None, sources_urls={})
    text = format_event(ev, NEW)
    assert "23:00" not in text and "Tickets" not in text and "Sat 21 Nov 2026" in text


def test_prefixes():
    assert format_event(mk(), NEW, short_notice=True).startswith("⚠️ ")
    assert "❌ CANCELED" in format_event(mk(), CHANGE, change="canceled")
    assert "\U0001f504 CHANGED" in format_event(mk(), CHANGE, change="start")
    assert "⏰ in 7 days" in format_event(mk(), REMINDER_7D)
    assert "⏰ tomorrow" in format_event(mk(), REMINDER_1D)
    assert "\U0001f39f ON SALE NOW" in format_event(mk(), ON_SALE)


def test_html_escaping_everywhere():
    ev = mk(artists=["A&B <script>"], venue='Club "X" <b>', city="O'Neil",
            url='https://x.example/?a=1&b="2"', sources_urls={"Via <i>": 'https://x.example/?a=1&b="2"'})
    t = format_event(ev, NEW)
    assert "<script>" not in t and "&lt;script&gt;" in t
    assert 'href="https://x.example/?a=1&amp;b=&quot;2&quot;"' in t
    assert "<i>" not in t
    assert "javascript:" not in format_event(mk(url="javascript:alert(1)"), NEW)
    assert "<a " not in format_event(mk(url="javascript:alert(1)"), NEW)


def test_split_message_at_4000_on_line_boundaries():
    lines = ["x" * 99] * 100  # 100 lines of 99 chars
    chunks = split_message("\n".join(lines))
    assert len(chunks) == 3 and all(len(c) <= 4000 for c in chunks)
    assert "\n".join(chunks) == "\n".join(lines)
    assert all(len(c) <= 4000 for c in split_message("y" * 9000))


@responses.activate
def test_send_posts_expected_payload_and_throttles():
    seen = []
    responses.add_callback(responses.POST, URL, callback=lambda r: (seen.append(json.loads(r.body)) or (200, {}, "{}")))
    sleeps = []
    c = client(sleeps)
    assert c.send("hello", preview=False)
    assert c.send("again", preview=True)
    assert seen[0] == {"chat_id": "42", "text": "hello", "parse_mode": "HTML", "disable_web_page_preview": True}
    assert seen[1]["disable_web_page_preview"] is False
    assert sleeps == [pytest.approx(1.1)]  # >= 1.1 s between messages


@responses.activate
def test_429_retry_after_then_success():
    responses.add(responses.POST, URL, status=429, json={"ok": False, "parameters": {"retry_after": 3}})
    responses.add(responses.POST, URL, status=200, json={"ok": True})
    sleeps = []
    assert client(sleeps).send("hi")
    assert sleeps == [3.0]


@responses.activate
def test_5xx_backoff_and_give_up(caplog):
    for _ in range(4):
        responses.add(responses.POST, URL, status=502)
    sleeps = []
    with caplog.at_level(logging.DEBUG):
        assert client(sleeps).send("hi") is False
    assert sleeps == [1, 2, 4] and len(responses.calls) == 4
    assert "TOKEN" not in caplog.text


@responses.activate
def test_client_error_is_not_retried():
    responses.add(responses.POST, URL, status=400, json={"ok": False, "description": "bad"})
    sleeps = []
    assert client(sleeps).send("hi") is False and len(responses.calls) == 1


def test_dry_run_notifier_collects_and_prints(capsys):
    n = DryRunNotifier()
    assert n.send("<b>x</b>", False)
    assert n.messages == [("<b>x</b>", False)]
    assert "<b>x</b>" in capsys.readouterr().out


def test_digest_and_first_run_formatting():
    a = mk(title="Khruangbin", artists=["Khruangbin"], url=None)
    c = DigestContent(maybe=[("a", a)], removed=[("b", mk())])
    t = format_digest(c)
    assert "Maybe" in t and "Khruangbin" in t and "Possibly removed" in t and "Heads-up" not in t
    assert format_first_run([mk(), mk()]).startswith("\U0001f44b First run: found 2 must-see shows")
