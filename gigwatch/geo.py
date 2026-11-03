"""Geography helpers: geohash, distance, region test."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from gigwatch.normalize import contains_words, norm

if TYPE_CHECKING:
    from gigwatch.config import Config
    from gigwatch.models import Event

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 5) -> str:
    lat_i = [-90.0, 90.0]
    lon_i = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    out: list[str] = []
    bit = 0
    ch = 0
    even = True
    while len(out) < precision:
        rng, val = (lon_i, lon) if even else (lat_i, lat)
        mid = (rng[0] + rng[1]) / 2
        if val > mid:
            ch |= bits[bit]
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_B32[ch])
            bit = 0
            ch = 0
    return "".join(out)


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def distance_km(ev: "Event", cfg: "Config") -> float | None:
    if ev.lat is None or ev.lon is None:
        return None
    return haversine_km(cfg.home_lat, cfg.home_lon, ev.lat, ev.lon)


def find_region_keyword(text: str, cfg: "Config") -> str | None:
    n = norm(text)
    return next((k for k in cfg.region_keywords if contains_words(n, norm(k))), None)


def find_non_region_city(text: str, cfg: "Config") -> str | None:
    n = norm(text)
    return next((c for c in cfg.non_region_cities if contains_words(n, norm(c))), None)


def in_region(ev: "Event", cfg: "Config") -> bool:
    """Coordinates win when present (never fall back to keywords). Without coordinates:
    a region keyword in city/venue, or a venue configured in venues.yaml, counts as local."""
    from gigwatch.match import venue_match

    d = distance_km(ev, cfg)
    if d is not None:
        return d <= cfg.radius_km
    text = norm(f"{ev.city or ''} {ev.venue or ''}")
    if text and any(contains_words(text, norm(k)) for k in cfg.region_keywords):
        return True
    return bool(ev.venue and venue_match(ev.venue, cfg.venues))
