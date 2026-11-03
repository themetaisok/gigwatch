"""Cross-source deduplication with stable uids."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from rapidfuzz import fuzz

from gigwatch.geo import haversine_km
from gigwatch.match import match_artist, primary_artist_name, venue_match
from gigwatch.models import MADRID, STATUS_RANK, Event
from gigwatch.normalize import norm

if TYPE_CHECKING:
    from gigwatch.config import Artist, Venue


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _venue_key(ev: Event, venues: Sequence["Venue"] | None) -> str:
    if venues and ev.venue:
        v = venue_match(ev.venue, venues)
        if v:
            return norm(v.name)
    return norm(ev.venue or ev.city or "")


def key(ev: Event, artists: Sequence["Artist"] | None = None,
        venues: Sequence["Venue"] | None = None) -> str:
    if ev.start is None:
        return _sha("nodate|" + ",".join(sorted(ev.source_ids)))[:16]
    primary = norm(primary_artist_name(ev, artists))
    day = ev.start.astimezone(MADRID).date().isoformat()
    return _sha(f"{primary}|{day}|{_venue_key(ev, venues)}")[:16]


def _artist_set(ev: Event, artists: Sequence["Artist"] | None) -> set[str]:
    s = {norm(a) for a in ev.artists if norm(a)}
    s.add(norm(primary_artist_name(ev, artists)))
    s.discard("")
    return s


def _followed_set(ev: Event, artists: Sequence["Artist"] | None) -> set[str]:
    if not artists:
        return set()
    return {norm(h.artist.name) for h in match_artist(ev, artists)}


def _multiday(ev: Event) -> bool:
    """A festival-style entry: all-day, spanning more than one local date."""
    return bool(ev.start and ev.end and ev.time_unknown
                and ev.end.astimezone(MADRID).date() > ev.start.astimezone(MADRID).date())


def _merged_hint(members: list[Event]) -> str | None:
    """Notification-style hints only survive when every member is notification-style, so a
    Ticketmaster event merged with an email keeps its normal (uncapped) tiering."""
    hints = [m.raw_tier_hint for m in members]
    if any(h is None for h in hints):
        return None
    return "must-see" if "must-see" in hints else hints[0]


def merge_events(members: list[Event], priority: dict[str, int], uid: str) -> Event:
    """members must already be sorted by priority (best first)."""
    p = members[0]
    sources_urls: dict[str, str] = {}
    for m in members:
        urls = m.sources_urls or {m.source: m.url or ""}
        for label, url in urls.items():
            if url or label not in sources_urls:
                sources_urls[label] = url or sources_urls.get(label, "")
    url = next((m.url for m in members if m.url), None)

    artists: list[str] = []
    seen: set[str] = set()
    for m in members:
        for a in m.artists:
            if norm(a) not in seen:
                seen.add(norm(a))
                artists.append(a)

    status = p.status
    worst = max((m.status for m in members), key=lambda s: STATUS_RANK.get(s, 0))
    if STATUS_RANK.get(worst, 0) > 0:
        status = worst
    elif status == "unknown":
        status = next((m.status for m in members if m.status != "unknown"), "unknown")

    dated = [m for m in members if m.start is not None]
    precise = next((m for m in dated if not m.time_unknown), None)
    chosen = precise or (dated[0] if dated else None)

    def first(attr: str):
        return next((getattr(m, attr) for m in members if getattr(m, attr) not in (None, "")), None)

    genres = list(dict.fromkeys(g for m in members for g in m.genres))
    return Event(
        uid=uid,
        source=p.source,
        source_ids=set().union(*(m.source_ids for m in members)),
        title=p.title,
        artists=artists,
        venue=first("venue"),
        city=first("city"),
        lat=first("lat"),
        lon=first("lon"),
        start=chosen.start if chosen else None,
        end=chosen.end if chosen and chosen.end else first("end"),
        url=url,
        status=status,
        genres=genres,
        raw_tier_hint=_merged_hint(members),
        fetched_at=min(m.fetched_at for m in members),
        onsale_at=first("onsale_at"),
        tier=first("tier"),
        sources_urls=sources_urls,
        time_unknown=chosen.time_unknown if chosen else False,
        description=first("description") or "",
    )


def dedupe(events: Sequence[Event], priority: Sequence[str],
           artists: Sequence["Artist"] | None = None,
           venues: Sequence["Venue"] | None = None) -> list[Event]:
    rank = {name: i for i, name in enumerate(priority)}
    n = len(events)
    keys = [key(e, artists, venues) for e in events]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # pass 1: identical key
    by_key: dict[str, int] = {}
    for i, k in enumerate(keys):
        if k in by_key:
            union(i, by_key[k])
        else:
            by_key[k] = i

    day = [e.start.astimezone(MADRID).date() if e.start else None for e in events]
    vk = [_venue_key(e, venues) for e in events]
    aset = [_artist_set(e, artists) if e.start else set() for e in events]
    fset = [_followed_set(e, artists) if e.start else set() for e in events]
    multi = [_multiday(e) for e in events]

    # pass 2: same day, similar non-empty venues, shared artist or similar title
    for i in range(n):
        for j in range(i + 1, n):
            if day[i] is None or day[i] != day[j] or find(i) == find(j) or multi[i] != multi[j]:
                continue
            a, b = events[i], events[j]
            shared = bool(aset[i] & aset[j])
            # rule A: same day, shared artist, venues less than 0.5 km apart
            if (shared and None not in (a.lat, a.lon, b.lat, b.lon)
                    and haversine_km(a.lat, a.lon, b.lat, b.lon) < 0.5):
                union(i, j)
                continue
            # rule B: similar non-empty venue names, and shared artist or similar title
            if not a.venue or not b.venue or not vk[i] or not vk[j]:
                continue
            if fuzz.ratio(vk[i], vk[j]) < 90:
                continue
            if fset[i] & fset[j]:  # venue-name rule needs a shared FOLLOWED artist
                union(i, j)

    # pass 3: attach venue-less dated items (e.g. email alerts) to a venue-bearing event
    for i in range(n):
        if day[i] is None or events[i].venue:
            continue
        for j in range(n):
            if (j != i and day[j] == day[i] and events[j].venue and aset[i] & aset[j]
                    and multi[i] == multi[j]):
                union(i, j)
                break

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[Event] = []
    for idxs in groups.values():
        idxs.sort(key=lambda i: (rank.get(events[i].source, 99), events[i].fetched_at, i))
        members = [events[i] for i in idxs]
        merged = merge_events(members, rank, keys[idxs[0]])
        out.append(merged)
    far = datetime.max.replace(tzinfo=MADRID)
    out.sort(key=lambda e: (e.start is None, e.start or far, e.uid))
    return out
