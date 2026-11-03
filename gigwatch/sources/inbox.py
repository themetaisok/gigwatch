"""Gmail inbox source: reads the user's own notification emails (IMAP, label 'gigwatch')."""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from gigwatch.config import Config, Venue
from gigwatch.geo import find_non_region_city, find_region_keyword
from gigwatch.match import hit_tier, match_artist
from gigwatch.normalize import contains_words, norm
from gigwatch.models import MADRID, Event, make_event
from gigwatch.sources.base import SourceError
from gigwatch.state import State

log = logging.getLogger("gigwatch")
IMAP_TIMEOUT = 60

PLATFORMS = {
    "ra.co": "Resident Advisor", "dice.fm": "DICE", "wegow.com": "Wegow",
    "songkick.com": "Songkick", "bandsintown.com": "Bandsintown", "feverup.com": "Fever",
    "ticketswap.com": "Ticketswap", "xceed.me": "Xceed",
}
TICKET_RE = re.compile(r"ticket|tickets|entradas|buy|event", re.I)
BAD_LINK_RE = re.compile(r"unsubscribe|preferences|privacy|mailto:", re.I)
# Mail-tracking redirect hosts: these links carry per-recipient tokens and are never published.
TRACKING_HOST_RE = re.compile(r"^(click|links|email|track)\.|list-manage|sendgrid|mailchimp", re.I)
BODY_HEADLINER_CHARS = 600  # artists are looked for in the subject first, then this much body

MONTH_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "gener": 1, "febrer": 2, "marc": 3, "maig": 5, "juny": 6, "juliol": 7, "agost": 8,
    "setembre": 9, "novembre": 11, "desembre": 12,
}
MONTH_EN_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                 "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
# Abbreviations only trusted when a day number precedes them ("15 dic").
MONTH_DAYFIRST_ABBR = {**MONTH_EN_ABBR, "ene": 1, "abr": 4, "ago": 8, "dic": 12, "gen": 1,
                       "set": 9, "des": 12}


def _alt(names) -> str:
    return "|".join(sorted(map(re.escape, names), key=len, reverse=True))


_MONTH_A = {**MONTH_FULL, **MONTH_DAYFIRST_ABBR}
_MONTH_B = {**MONTH_FULL, **MONTH_EN_ABBR}
_YEAR = r"(\d{4})(?!\d)"
RE_A = re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th|er)?\.?\s*(de\s+|d')?\s*({_alt(_MONTH_A)})(?![a-z])\.?"
                  rf"(?:\s*(?:de\s+|del\s+|,\s*)?{_YEAR})?")
# "set" and "des" are Catalan abbreviations but also plain words ("1st set", "des"): they only
# count as months with a year or a "de" connector.
AMBIGUOUS_ABBR = {"set", "des"}
# A date shortly after one of these words is a ticket-sale date, not the show date.
ONSALE_RE = re.compile(r"(?<![a-z])(on ?sale|pre-?sale|a la venta|preventa|venda|venta|sale)(?![a-z])")
ONSALE_WINDOW = 25
RE_B = re.compile(rf"(?<![a-z])({_alt(_MONTH_B)})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+{_YEAR})?")
RE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
RE_NUM = re.compile(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})(?!\d)")
# Times need ":" or "h" ("21:00", "21h"); a "." would turn dates like 12.11.2026 into 12:11.
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])(?:[:h]([0-5]\d)|\s?h\b)")
AMPM_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])(?::([0-5]\d))?\s?([ap])\.?m\b")


def fold(text: str) -> str:
    """Lower-case and strip accents while keeping string length (so offsets stay valid)."""
    out = []
    for c in text:
        d = unicodedata.normalize("NFKD", c)
        out.append(d[0] if d else c)
    return "".join(out).lower()


def source_name_from_sender(domain: str) -> str:
    d = domain.lower().strip()
    for root, name in PLATFORMS.items():
        if d == root or d.endswith("." + root):
            return name
    return d


def _root(domain: str) -> str:
    d = domain.lower().strip()
    for root in PLATFORMS:
        if d == root or d.endswith("." + root):
            return root
    return d


def clean_url(url: str) -> str:
    """Keep scheme://host/path only: query strings and fragments can carry tracking tokens."""
    sp = urlsplit(url)
    return urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))


class _Extract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._atext: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._atext = []
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._atext).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._atext.append(data)


def html_to_text_links(doc: str) -> tuple[str, list[tuple[str, str]]]:
    p = _Extract()
    p.feed(doc)
    return " ".join("".join(p.text).split()), p.links


def choose_link(links: list[tuple[str, str]], sender_domain: str) -> str | None:
    root = _root(sender_domain)
    best: tuple[int, int] | None = None
    best_url = None
    for i, (href, text) in enumerate(links):
        if not href.lower().startswith(("http://", "https://")) or BAD_LINK_RE.search(href + " " + text):
            continue
        host = urlsplit(href).netloc.lower()
        if TRACKING_HOST_RE.search(host):
            continue
        score = (2 if host == root or host.endswith("." + root) else 0) \
            + (1 if TICKET_RE.search(text) or TICKET_RE.search(href) else 0)
        if best is None or (score, -i) > best:
            best, best_url = (score, -i), href
    return clean_url(best_url) if best_url else None


def _time_after(t: str) -> tuple[int, int] | None:
    """First clock time in t: 24h ("21:00", "21h") or 12h ("10:30 PM")."""
    m24, m12 = TIME_RE.search(t), AMPM_RE.search(t)
    if m12 and (not m24 or m12.start() <= m24.start()):
        h = int(m12.group(1)) % 12 + (12 if m12.group(3) == "p" else 0)
        return h, int(m12.group(2) or 0)
    if m24:
        return int(m24.group(1)), int(m24.group(2) or 0)
    return None


def find_date(text: str, now: datetime, sent: datetime | None = None
              ) -> tuple[datetime, bool] | None:
    """First plausible show date in the text (English/Spanish/Catalan).

    Skipped: dates before today, ticket-sale dates ("on sale Fri 6 Nov"), and the day the mail
    was sent (the Date: header, else today) whenever another date is available.
    Returns (aware datetime, time_unknown)."""
    t = fold(text)
    today = now.astimezone(MADRID).date()
    sent_day = (sent or now).astimezone(MADRID).date()
    cands: list[tuple[int, int, int | None, int, int]] = []  # pos, end, year, month, day
    a_spans: list[tuple[int, int]] = []
    for m in RE_A.finditer(t):
        month_tok = m.group(3)
        if month_tok in AMBIGUOUS_ABBR and not (m.group(4) or m.group(2)):
            continue
        a_spans.append(m.span())
        cands.append((m.start(), m.end(), int(m.group(4)) if m.group(4) else None,
                      _MONTH_A[month_tok], int(m.group(1))))
    for m in RE_B.finditer(t):
        if any(m.start() < e and s < m.end() for s, e in a_spans):  # "mar 18 nov": mar = Tuesday
            continue
        cands.append((m.start(), m.end(), int(m.group(3)) if m.group(3) else None,
                      _MONTH_B[m.group(1)], int(m.group(2))))
    for m in RE_ISO.finditer(t):
        cands.append((m.start(), m.end(), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in RE_NUM.finditer(t):
        y = int(m.group(3))
        cands.append((m.start(), m.end(), y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1))))
    cands.sort()
    valid: list[tuple[date, tuple[int, int] | None]] = []
    for i, (pos, end, year, month, day) in enumerate(cands):
        if ONSALE_RE.search(t[max(0, pos - ONSALE_WINDOW):pos]):
            continue
        try:
            d = date(year or today.year, month, day)
        except ValueError:
            continue
        if year is None and d < today:
            try:
                d = date(today.year + 1, month, day)
            except ValueError:
                continue
        if d < today:
            continue
        stop = min(end + 30, cands[i + 1][0] if i + 1 < len(cands) else len(t))
        valid.append((d, _time_after(t[end:stop])))
    # A date on the send day is usually "sent <date>" noise, so drop it when another date is
    # available -- unless a time follows it (a show tonight), or the next date is the following
    # morning (an overnight range "23:00 - 04 Nov 06:00": keep the earlier start).
    kept = []
    for i, (d, tm) in enumerate(valid):
        if d == sent_day and tm is None and any(v[0] != sent_day for v in valid):
            nxt = valid[i + 1] if i + 1 < len(valid) else None
            overnight = (nxt and nxt[0] == d + timedelta(days=1) and nxt[1] is not None
                         and nxt[1][0] < 12)
            if not overnight:
                continue
        kept.append((d, tm))
    if not kept:
        return None
    d, tm = kept[0]
    if tm:
        return datetime(d.year, d.month, d.day, tm[0], tm[1], tzinfo=MADRID), False
    return datetime(d.year, d.month, d.day, tzinfo=MADRID), True


def _venue_in(text: str, cfg: Config) -> "Venue | None":
    n = norm(text)
    for v in cfg.venues:
        for a in (norm(x) for x in [v.name, *v.aliases]):
            if len(a) >= 4 and contains_words(n, a):
                return v
    return None


def parse_message(raw: bytes, cfg: Config, now: datetime) -> Event:
    """Turn one notification email into an Event. Subject/body text is never stored."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    _, addr = email.utils.parseaddr(str(msg.get("From", "")))
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    platform = source_name_from_sender(domain)
    subject = str(msg.get("Subject", ""))
    body_part = msg.get_body(preferencelist=("html", "plain"))
    content = body_part.get_content() if body_part else ""
    links: list[tuple[str, str]] = []
    if body_part is not None and body_part.get_content_type() == "text/html":
        text, links = html_to_text_links(content)
    else:
        text = " ".join(content.split())
        links = [(u, "") for u in re.findall(r"https?://[^\s<>\"]+", content)]
    full = f"{subject}\n{text}"
    mid = str(msg.get("Message-ID") or "") or hashlib.sha1(raw).hexdigest()
    sid = hashlib.sha1(mid.encode("utf-8")).hexdigest()

    # The headliner is whoever the subject names; only if it names nobody do we look at the
    # start of the body (never the "you might also like" footer).
    hits = match_artist(make_event(source="inbox", title=subject, fetched_at=now), cfg.artists)
    if not hits:
        hits = match_artist(make_event(source="inbox", title=text[:BODY_HEADLINER_CHARS],
                                       fetched_at=now), cfg.artists)
    # Only must-see-worthy hits become `artists`; a downgraded one (a common-word name found only
    # in the title, or a maybe-tier artist) stays title-only so it keeps its lower tier.
    names = [h.artist.name for h in hits if hit_tier(h) == "must-see"]
    sent = None
    try:
        sent = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=MADRID)
    except (TypeError, ValueError):
        sent = None
    dated = find_date(full, now, sent)
    # Location comes from the subject and the start of the body only: footers carry company
    # addresses ("Hoxton Square, London") that say nothing about the show.
    head = f"{subject}\n{text[:BODY_HEADLINER_CHARS]}"
    venue = _venue_in(head, cfg)
    city = find_region_keyword(head, cfg) or (venue.city if venue else None)
    hint = "must-see"
    if city is None and venue is None:
        city = find_non_region_city(head, cfg)
        if city:
            hint = "heads-up"  # a tour date somewhere else: digest only
    url = choose_link(links, domain)
    root = _root(domain)
    if url is None and root in PLATFORMS:
        url = f"https://{root}/"  # never fall back to a tracking link
    label = f"{platform} email"
    title = hits[0].artist.name if hits else f"New {platform} notification"
    return make_event(
        source="inbox", title=title, fetched_at=now, source_ids={f"inbox:{sid}"},
        artists=names, venue=venue.name if venue else None, city=city, start=dated[0] if dated else None,
        time_unknown=dated[1] if dated else False, url=url, status="unknown",
        raw_tier_hint=hint, sources_urls={label: url or ""})


class InboxSource:
    """Reads UNSEEN mail from the 'gigwatch' label.

    fetch() only reads. Mail is flagged \\Seen (and remembered in state.inbox_seen) by
    finalize(), which the runner calls after state has been saved and alerts have gone out, so a
    failed send or a crash leaves the mail unread for the next run."""
    name = "inbox"
    priority = 3

    def __init__(self, env: Mapping[str, str], state: State, mark_seen: bool = True,
                 imap_factory: Callable[..., Any] | None = None) -> None:
        self.user = env.get("GMAIL_USER") or None
        self.password = env.get("GMAIL_APP_PASSWORD") or None
        self.state = state
        self.mark_seen = mark_seen
        self.imap_factory = imap_factory
        self._conn: Any = None
        self._pending: dict[str, bytes] = {}  # sid -> IMAP message number

    def enabled(self, env: Mapping[str, str]) -> bool:
        return bool(env.get("GMAIL_USER") and env.get("GMAIL_APP_PASSWORD"))

    def pending_ids(self) -> set[str]:
        return set(self._pending)

    def fetch(self, cfg: Config, now: datetime) -> list[Event]:
        self.close()
        self._pending = {}
        try:
            conn = (self.imap_factory or imaplib.IMAP4_SSL)("imap.gmail.com", timeout=IMAP_TIMEOUT)
            self._conn = conn
            conn.login(self.user, self.password)
            typ, _ = conn.select("gigwatch")
            if typ != "OK":
                raise SourceError("Gmail label 'gigwatch' not found")
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                raise SourceError("IMAP search failed")
        except (imaplib.IMAP4.error, OSError) as e:
            self.close()
            raise SourceError(f"IMAP error: {type(e).__name__}") from e
        except SourceError:
            self.close()
            raise
        out: list[Event] = []
        for num in (data[0] or b"").split():
            try:
                typ, parts = conn.fetch(num, "(BODY.PEEK[])")
                raw = next(p[1] for p in parts if isinstance(p, tuple))
                ev = parse_message(raw, cfg, now)
            except Exception as e:  # noqa: BLE001 - leave unseen, try next run
                log.warning("inbox: could not process message: %s", type(e).__name__)
                continue
            sid = next(iter(ev.source_ids)).split(":", 1)[1]
            self._pending[sid] = num
            if sid not in self.state.inbox_seen:  # else: handled earlier, only the flag is missing
                out.append(ev)
        return out

    def finalize(self, done: set[str]) -> int:
        """Flag mails whose event is safely stored/sent; returns how many were marked."""
        n = 0
        if self._conn is not None and self.mark_seen:
            for sid, num in self._pending.items():
                if sid not in done:
                    continue
                try:
                    self._conn.store(num, "+FLAGS", "\\Seen")
                except Exception as e:  # noqa: BLE001
                    log.warning("inbox: could not flag message: %s", type(e).__name__)
                    continue
                self.state.mark_inbox_seen(sid)
                n += 1
        self.close()
        return n

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
