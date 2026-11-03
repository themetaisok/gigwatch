"""RSS/Atom feeds (heads-up items) and ICS/webcal calendars (structured events)."""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import feedparser
import requests

from gigwatch.config import Config
from gigwatch.geo import find_non_region_city, find_region_keyword
from gigwatch.match import has_match, hit_tier, match_artist
from gigwatch.models import MADRID, Event, make_event
from gigwatch.normalize import contains_words, norm
from gigwatch.sources.base import SourceError, request_with_retry
from gigwatch.state import State

log = logging.getLogger("gigwatch")
_TAGS = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", s or "")).split())


def unwrap_google(url: str) -> str:
    """Google Alerts wraps links as google.com/url?...&url=<real>."""
    sp = urlsplit(url)
    if sp.netloc.endswith("google.com") and sp.path == "/url":
        real = parse_qs(sp.query).get("url") or parse_qs(sp.query).get("q")
        if real:
            return real[0]
    return url


def is_ics(url: str, body: str) -> bool:
    path = urlsplit(url).path.lower()
    return (body.lstrip("﻿ \r\n\t").startswith("BEGIN:VCALENDAR") or path.endswith(".ics")
            or url.lower().startswith("webcal://"))


# ---------- ICS ----------

def unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def ics_unescape(s: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _ics_dt(value: str, params: dict[str, str]) -> tuple[datetime, bool] | None:
    v = value.strip()
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
            d = datetime.strptime(v[:8], "%Y%m%d")
            return d.replace(tzinfo=MADRID), True
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(MADRID), False
        naive = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tz = MADRID
        if params.get("TZID"):
            try:
                tz = ZoneInfo(params["TZID"])
            except Exception:  # noqa: BLE001
                tz = MADRID
        return naive.replace(tzinfo=tz).astimezone(MADRID), False
    except ValueError:
        return None


def parse_ics(text: str, label: str, cfg: Config, now: datetime) -> list[Event]:
    events: list[Event] = []
    cur: dict[str, tuple[dict[str, str], str]] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                ev = _ics_event(cur, label, cfg, now)
                if ev:
                    events.append(ev)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *plist = head.split(";")
        params = {}
        for p in plist:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        cur.setdefault(name.upper(), (params, value))
    return events


def _ics_event(props: dict[str, tuple[dict[str, str], str]], label: str, cfg: Config,
               now: datetime) -> Event | None:
    def val(k: str) -> str:
        return ics_unescape(props[k][1]).strip() if k in props else ""

    if "DTSTART" not in props or not val("SUMMARY"):
        return None
    s = _ics_dt(props["DTSTART"][1], props["DTSTART"][0])
    if s is None:
        return None
    start, unknown = s
    end = None
    if "DTEND" in props:
        e = _ics_dt(props["DTEND"][1], props["DTEND"][0])
        end = e[0] if e else None
        if end and unknown:
            end = end - timedelta(days=1) if end > start else None  # DATE DTEND is exclusive
    summary = val("SUMMARY")
    artist = re.split(r"\s+(?:at|@|-)\s+", summary, maxsplit=1)[0].strip()
    location = val("LOCATION")
    venue = location.split(",")[0].strip() or None
    city = None
    nloc = norm(location)
    for kw in cfg.region_keywords:
        if contains_words(nloc, norm(kw)):
            city = kw
            break
    lat = lon = None
    if "GEO" in props:
        try:
            a, b = props["GEO"][1].split(";")
            lat, lon = float(a), float(b)
        except ValueError:
            pass
    url = val("URL") or None
    if not url:
        m = re.search(r"https?://[^\s<>\"]+", val("DESCRIPTION"))
        url = m.group(0) if m else None
    status = "canceled" if val("STATUS").upper() == "CANCELLED" else "unknown"
    uid = val("UID") or f"{summary}|{start.isoformat()}"
    # UID + start date: recurring events reuse one UID for every instance
    h = hashlib.sha1(f"{uid}|{start.astimezone(MADRID).date()}".encode("utf-8")).hexdigest()[:12]
    return make_event(
        source="feeds", title=summary, fetched_at=now, source_ids={f"feed:ics:{h}"},
        artists=[artist] if artist else [], venue=venue, city=city, lat=lat, lon=lon,
        start=start, end=end, url=url, status=status, time_unknown=unknown,
        description=val("DESCRIPTION"), sources_urls={label: url or ""})


# ---------- source ----------

class FeedsSource:
    name = "feeds"
    priority = 4

    def __init__(self, cfg: Config, state: State, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.feeds = cfg.feeds
        self.state = state
        self.session = session or requests.Session()
        self.sleep = sleep

    def enabled(self, env: Mapping[str, str]) -> bool:
        return bool(self.feeds)

    def fetch(self, cfg: Config, now: datetime) -> list[Event]:
        out: list[Event] = []
        failures = 0
        for feed in self.feeds:
            url = feed["url"]
            if url.lower().startswith("webcal://"):
                url = "https://" + url[len("webcal://"):]
            try:
                r = request_with_retry(self.session, url, sleep=self.sleep)
                body = r.content.decode("utf-8-sig", errors="replace")
            except SourceError as e:
                failures += 1
                log.warning("feed %s failed: %s", feed["name"], e)
                continue
            if is_ics(feed["url"], body):
                out.extend(parse_ics(body, feed["name"], cfg, now))
            else:
                out.extend(self._rss(body, feed, cfg, now))
        if self.feeds and failures == len(self.feeds):
            raise SourceError("all feeds failed")
        return out

    def _rss(self, body: str, feed: dict, cfg: Config, now: datetime) -> list[Event]:
        parsed = feedparser.parse(body)
        out: list[Event] = []
        for entry in parsed.entries:
            link = entry.get("link") or ""
            ident = entry.get("id") or link
            if not ident:
                continue
            h = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]
            if h in self.state.feed_seen:
                continue
            self.state.mark_feed_seen(h)
            title = strip_html(entry.get("title", ""))
            summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
            probe = make_event(source="feeds", title=f"{title} {summary}", fetched_at=now,
                               description=summary)
            if not has_match(probe, cfg):
                continue
            url = unwrap_google(link) or None
            hits = match_artist(probe, cfg.artists)
            names = [h.artist.name for h in hits if hit_tier(h) == "must-see"]
            # Feed items are digest material unless the feed opts in with `immediate: true`.
            # Even then: a title-only hit that is downgraded, or an item about another city,
            # stays a heads-up.
            text = f"{title} {summary}"
            elsewhere = (find_region_keyword(text, cfg) is None
                         and find_non_region_city(text, cfg) is not None)
            strong = any(hit_tier(h) == "must-see" for h in hits)
            hint = "must-see" if feed.get("immediate") and strong and not elsewhere else "heads-up"
            out.append(make_event(
                source="feeds", title=title, fetched_at=now, source_ids={f"feed:{h}"},
                artists=names, url=url, status="unknown", raw_tier_hint=hint,
                description=summary, sources_urls={feed["name"]: url or ""}))
        return out
