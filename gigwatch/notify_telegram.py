"""Telegram client, dry-run notifier and message formatting."""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Callable, Protocol, Sequence

import requests

from gigwatch.models import (CHANGE, MADRID, NEW, ON_SALE, REMINDER_1D, REMINDER_7D, Event)

log = logging.getLogger("gigwatch")

MAX_LEN = 4000
MIN_INTERVAL = 1.1
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class Notifier(Protocol):
    def send(self, text: str, preview: bool = True) -> bool: ...


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


def _open_tags(text: str) -> list[tuple[str, str]]:
    """Stack of (name, full opening tag) still open at the end of text."""
    stack: list[tuple[str, str]] = []
    for m in _TAG_RE.finditer(text):
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == m.group(2):
                    del stack[i:]
                    break
        else:
            stack.append((m.group(2), m.group(0)))
    return stack


def _closers(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{n}>" for n, _ in reversed(stack))


def _cut_point(line: str, limit: int) -> int:
    """Index at which to cut an over-long line: the last space outside any <tag> (leaving room
    for the closing tags we must append), else the last position outside a tag or &entity;."""
    in_tag = in_ent = False
    best_space = best_safe = -1
    for i, ch in enumerate(line[: limit + 1]):
        if not in_tag and not in_ent and i > 0:
            room = limit - len(_closers(_open_tags(line[:i]))) if "<" in line[:i] else limit
            if i <= room:
                best_safe = i
                if ch == " ":
                    best_space = i
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif ch == "&" and not in_tag:
            in_ent = True
        elif ch in "; " and in_ent:
            in_ent = False
    if best_space > 0:
        return best_space
    return best_safe if best_safe > 0 else limit


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on line boundaries; split an over-long line at a space outside HTML tags, closing
    the tags still open at the cut and reopening them in the next chunk."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            cut = _cut_point(line, limit)
            stack = _open_tags(line[:cut])
            rest = line[cut:].lstrip(" ")
            nxt = "".join(t for _, t in stack) + rest
            if len(nxt) >= len(line):  # no progress (tags too long): plain hard cut
                chunks.append(line[:limit])
                line = line[limit:]
                continue
            chunks.append(line[:cut] + _closers(stack))
            line = nxt
        add = line if not cur else cur + "\n" + line
        if len(add) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = add
    if cur or not chunks:
        chunks.append(cur)
    return chunks


class TelegramClient:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, max_retries: int = 3) -> None:
        self._token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.max_retries = max_retries
        self._last: float | None = None
        self.chunks_sent = 0   # chunks delivered by the most recent send()
        self.chunks_total = 0

    @property
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self._token}/sendMessage"

    def send(self, text: str, preview: bool = True) -> bool:
        chunks = split_message(text)
        self.chunks_sent, self.chunks_total = 0, len(chunks)
        for chunk in chunks:
            if not self._send_one(chunk, preview):
                log.warning("telegram: stopped after %d/%d chunks", self.chunks_sent, len(chunks))
                return False  # never send later chunks out of order after a failure
            self.chunks_sent += 1
        return True

    def _throttle(self) -> None:
        if self._last is not None:
            wait = MIN_INTERVAL - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)

    def _send_one(self, text: str, preview: bool) -> bool:
        self._throttle()
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": not preview}
        for attempt in range(self.max_retries + 1):
            last = attempt >= self.max_retries
            try:
                r = self.session.post(self._url, json=payload, timeout=30)
            except requests.RequestException as e:
                log.warning("telegram: %s", type(e).__name__)  # never log the URL (token)
                if last:
                    break
                self.sleep(2 ** attempt)
                continue
            self._last = self.clock()
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2 ** attempt))
                except (ValueError, AttributeError):
                    wait = 2 ** attempt
                log.warning("telegram: rate limited, retry in %ss", wait)
                if last:
                    break
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning("telegram: HTTP %s", r.status_code)
                if last:
                    break
                self.sleep(2 ** attempt)
                continue
            log.error("telegram: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return False


class DryRunNotifier:
    def __init__(self, echo: bool = True) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.echo = echo

    def send(self, text: str, preview: bool = True) -> bool:
        self.messages.append((text, preview))
        if self.echo:
            print("---- [dry-run message] ----")
            print(text)
        return True


# --- formatting ---

def fmt_date(dt) -> str:
    d = dt.astimezone(MADRID)
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def fmt_when(ev: Event) -> str:
    if ev.start is None:
        return "Date TBA"
    s = fmt_date(ev.start)
    if not ev.time_unknown:
        s += " · " + ev.start.astimezone(MADRID).strftime("%H:%M")
    return s


def display_name(ev: Event) -> str:
    return ", ".join(ev.artists[:3]) if ev.artists else ev.title


def safe_url(url: str | None) -> str | None:
    return url if url and url.lower().startswith(("http://", "https://")) else None


def _labels(ev: Event) -> tuple[str | None, list[str]]:
    """(label of the source whose url we link, labels of the other sources)."""
    urls = ev.sources_urls or {}
    via = next((k for k, v in urls.items() if v and v == ev.url), None)
    if via is None:
        via = next((k for k, v in urls.items() if v), None)
    others = [k for k in urls if k != via]
    return via, others


_CHANGE_PREFIX = {
    "canceled": "❌ CANCELED", "postponed": "\U0001f504 POSTPONED",
    "rescheduled": "\U0001f504 RESCHEDULED", "start": "\U0001f504 CHANGED",
    "url": "\U0001f39f TICKETS LINK ADDED", "date": "\U0001f4c5 DATE CONFIRMED",
}
_KIND_PREFIX = {
    REMINDER_7D: "⏰ in 7 days", REMINDER_1D: "⏰ tomorrow",
    ON_SALE: "\U0001f39f ON SALE NOW",
}


def format_event(ev: Event, kind: str = NEW, distance: float | None = None, *,
                 change: str | None = None, short_notice: bool = False) -> str:
    header = f"\U0001f3af MUST-SEE · <b>{esc(display_name(ev))}</b>"
    if kind == CHANGE:
        header = f"{_CHANGE_PREFIX.get(change or 'start', _CHANGE_PREFIX['start'])} · {header}"
    elif kind in _KIND_PREFIX:
        header = f"{_KIND_PREFIX[kind]} · {header}"
    if short_notice:
        header = f"⚠️ {header}"
    lines = [header, f"\U0001f4c5 {esc(fmt_when(ev))}"]
    place = ", ".join(esc(x) for x in (ev.venue, ev.city) if x)
    if place:
        if distance is not None and distance > 15:
            place += f" · {round(distance)} km"
        lines.append(f"\U0001f4cd {place}")
    url = safe_url(ev.url)
    via, others = _labels(ev)
    if url:
        line = f'\U0001f39f <a href="{esc(url)}">Tickets</a>'
        if via:
            line += f" · via {esc(via)}"
        lines.append(line)
    if others:
        lines.append(f"(+ also: {esc(', '.join(others))})")
    return "\n".join(lines)


def format_line(ev: Event) -> str:
    """One compact line for summaries and digests."""
    parts = [f"<b>{esc(ev.title if ev.start is None else display_name(ev))}</b>"]
    if ev.start:
        parts.append(esc(fmt_when(ev)))
    place = ", ".join(esc(x) for x in (ev.venue, ev.city) if x)
    if place:
        parts.append(place)
    line = "• " + " · ".join(parts)
    url = safe_url(ev.url)
    if url:
        line += f' · <a href="{esc(url)}">link</a>'
    return line


def format_first_run(events: Sequence[Event]) -> str:
    lines = [f"\U0001f44b First run: found {len(events)} must-see shows"]
    lines += [format_line(e) for e in events]
    return "\n".join(lines)


def format_overflow(events: Sequence[Event], cap: int) -> str:
    """Compact list for must-see shows beyond the per-run alert cap (sent immediately)."""
    lines = [f"\U0001f3af {len(events)} more must-see shows (alert cap of {cap} per run reached)"]
    lines += [format_line(e) for e in events]
    return "\n".join(lines)


def format_digest(content) -> str:
    """content is a state.DigestContent."""
    lines = ["\U0001f4ec <b>gigwatch digest</b>"]

    def section(title: str, items) -> None:
        if items:
            lines.append("")
            lines.append(f"<b>{esc(title)}</b>")
            lines.extend(format_line(e) for _, e in items)

    section("Must-see held back (too many alerts at once)", content.held)
    section("Maybe", content.maybe)
    section("Heads-up (no date yet)", content.heads)
    section("Possibly removed (no longer listed)", content.removed)
    return "\n".join(lines)
