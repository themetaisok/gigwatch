"""Artist / venue / promoter matching and tier assignment."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from rapidfuzz import fuzz

from gigwatch.models import TIER_HEADS, TIER_MAYBE, TIER_MUST
from gigwatch.normalize import contains_words, norm

if TYPE_CHECKING:
    from gigwatch.config import Artist, Config, Promoter, Venue
    from gigwatch.models import Event


@dataclass
class ArtistHit:
    artist: "Artist"
    how: str  # exact | fuzzy | title
    candidate: str


def _alias_set(artist: "Artist") -> list[str]:
    seen: list[str] = []
    for n in [artist.name, *artist.aliases]:
        k = norm(n)
        if k and k not in seen:
            seen.append(k)
    return seen


def _raw_aliases(artist: "Artist") -> list[str]:
    return [n for n in [artist.name, *artist.aliases] if n and n.strip()]


def match_artist(ev: "Event", artists: Sequence["Artist"]) -> list[ArtistHit]:
    cands = [c for c in (norm(a) for a in ev.artists) if c]
    title = norm(ev.title)
    raw_title = unicodedata.normalize("NFC", ev.title or "")
    hits: list[ArtistHit] = []
    for artist in artists:
        excluded = any(
            (x := norm(e)) and (contains_words(title, x) or any(contains_words(c, x) for c in cands))
            for e in artist.exclude
        )
        if excluded:
            continue
        hit: ArtistHit | None = None
        for alias in _alias_set(artist):
            for c in cands:
                if alias == c:
                    hit = ArtistHit(artist, "exact", c)
                    break
                if (len(alias) >= 5 and min(len(alias), len(c)) >= 5
                        and len(alias.split()) == len(c.split())
                        and fuzz.token_set_ratio(alias, c) >= 92):
                    hit = ArtistHit(artist, "fuzzy", c)
                    break
            if hit:
                break
            if (artist.title_match != "off" and len(alias) > 3
                    and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", title)):
                hit = ArtistHit(artist, "title", title)
                break
        if hit is None and artist.title_match != "off":
            # Short names (<=3 chars once normalised) are too ambiguous to match after
            # accent folding, so they match only on the raw title, accent-sensitively
            # ("Âme" matches "Nitsa: Âme" but not "ame" or "Amelie"). ASCII-only short
            # aliases never match a title.
            for raw in _raw_aliases(artist):
                if len(norm(raw)) <= 3 and not raw.isascii():
                    r = unicodedata.normalize("NFC", raw)
                    if re.search(rf"(?<!\w){re.escape(r)}(?!\w)", raw_title, re.IGNORECASE):
                        hit = ArtistHit(artist, "title", raw_title)
                        break
        if hit:
            hits.append(hit)
    return hits


def hit_tier(h: ArtistHit) -> str:
    """Tier a single hit is worth. Title-only hits can be downgraded per artist."""
    if h.how == "title" and h.artist.tier == TIER_MUST and h.artist.title_match == TIER_MAYBE:
        return TIER_MAYBE
    return h.artist.tier


def _strip_sala(n: str) -> str:
    for pre in ("la sala ", "sala "):
        if n.startswith(pre) and len(n) > len(pre):
            return n[len(pre):]
    return n


def venue_match(venue: str | None, venues: Sequence["Venue"]) -> "Venue | None":
    """Match an event venue name to a configured venue. A leading "Sala"/"La Sala" is ignored on
    both sides; an alias may be contained in the name, or (alias >= 4 chars) the name in the alias."""
    name = _strip_sala(norm(venue))
    if not name:
        return None
    for v in venues:
        for a in (_strip_sala(norm(x)) for x in [v.name, *v.aliases]):
            if not a:
                continue
            if name == a or fuzz.ratio(name, a) >= 90:
                return v
            if len(a) >= 5 and contains_words(name, a):
                return v
            if len(a) >= 4 and len(name) >= 4 and contains_words(a, name):
                return v
    return None


def promoter_match(ev: "Event", promoters: Sequence["Promoter"]) -> "Promoter | None":
    title = norm(ev.title)
    desc = norm(ev.description)
    for p in promoters:
        for a in (norm(x) for x in [p.name, *p.aliases]):
            if a and (contains_words(title, a) or contains_words(desc, a)):
                return p
    return None


def genre_match(ev: "Event", genres: Sequence[str]) -> bool:
    wanted = {norm(g) for g in genres if norm(g)}
    return any(norm(g) in wanted for g in ev.genres)


def has_match(ev: "Event", cfg: "Config") -> bool:
    """Any artist, venue or promoter match (used by feeds, where tier is always heads-up)."""
    return bool(match_artist(ev, cfg.artists)) or bool(venue_match(ev.venue, cfg.venues)) \
        or bool(promoter_match(ev, cfg.promoters))


def primary_artist_name(ev: "Event", artists: Sequence["Artist"] | None) -> str:
    if artists:
        hits = match_artist(ev, artists)
        if hits:
            return hits[0].artist.name
    return ev.artists[0] if ev.artists else ev.title


def assign_tier(ev: "Event", cfg: "Config") -> str | None:
    """must-see > maybe > heads-up > None.

    raw_tier_hint on notification-style items (emails, feed entries):
      "heads-up"  never above heads-up (e.g. feed news, or an out-of-region tour mail)
      "must-see"  eligible for an immediate alert on a must-see artist hit even without a date
    """
    hint = ev.raw_tier_hint
    if hint == TIER_HEADS and ev.source in ("inbox", "feeds"):
        return TIER_HEADS
    if ev.start is not None or hint == TIER_MUST:
        hits = match_artist(ev, cfg.artists)
        tiers = [hit_tier(h) for h in hits]
        if TIER_MUST in tiers:
            return TIER_MUST
        if ev.start is not None and (
                hits or venue_match(ev.venue, cfg.venues) or promoter_match(ev, cfg.promoters)
                or genre_match(ev, cfg.genres)):
            return TIER_MAYBE
    if hint in (TIER_HEADS, TIER_MUST):
        return TIER_HEADS
    return None
