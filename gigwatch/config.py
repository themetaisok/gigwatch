"""Config loading and validation."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from gigwatch.normalize import norm

log = logging.getLogger("gigwatch")

REGION_KEYWORDS = [
    "Barcelona", "Badalona", "L'Hospitalet", "Hospitalet", "Sitges", "Girona",
    "Tarragona", "Lleida", "Mataró", "Terrassa", "Sabadell", "Vilanova", "Reus",
    "Salou", "Montmeló", "Cornellà", "Sant Cugat", "Granollers", "Santa Coloma",
    "Castelldefels", "Viladecans", "Gavà", "El Prat", "Sant Adrià", "Catalunya",
    "Cataluña", "Catalonia",
]
NON_REGION_CITIES = [
    "Madrid", "Valencia", "Sevilla", "Bilbao", "Zaragoza", "Málaga", "Ibiza", "Lisboa", "Lisbon",
    "Paris", "London", "Berlin", "Amsterdam", "Milan", "Milano", "Roma", "Rome", "Porto",
    "Mallorca", "Palma",
]
SOURCE_PRIORITY = ["ticketmaster", "bcn_opendata", "bandsintown", "inbox", "feeds"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "home": {"lat": 41.3874, "lon": 2.1686, "radius_km": 150},
    "timezone": "Europe/Madrid",
    "horizon_days": 180,
    "region_keywords": REGION_KEYWORDS,
    "extra_region_keywords": [],
    "non_region_cities": NON_REGION_CITIES,
    "maybe_digest": {"weekday": 1, "hour_local": 9},
    "min_lead_days_warning": 2,
    "quiet": False,
    "max_immediate": 15,
    "feeds": [],
    "source_priority": SOURCE_PRIORITY,
}
ARTIST_KEYS = {"name", "aliases", "tier", "exclude", "spotify_id", "notes", "title_match"}
TITLE_MATCH_VALUES = ("must-see", "maybe", "off")
VENUE_KEYS = {"name", "aliases", "city", "lat", "lon", "tier"}
PROMOTER_KEYS = {"name", "aliases", "tier"}
ARTIST_TIERS = ("must-see", "maybe")


class ConfigError(Exception):
    pass


@dataclass
class Artist:
    name: str
    aliases: list[str] = field(default_factory=list)
    tier: str = "must-see"
    exclude: list[str] = field(default_factory=list)
    spotify_id: str | None = None
    notes: str | None = None
    # How much a hit that only comes from the event TITLE is worth: must-see | maybe | off.
    title_match: str = "must-see"


@dataclass
class Venue:
    name: str
    aliases: list[str] = field(default_factory=list)
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    tier: str = "maybe"


@dataclass
class Promoter:
    name: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class Config:
    home_lat: float = 41.3874
    home_lon: float = 2.1686
    radius_km: float = 150
    timezone: str = "Europe/Madrid"
    horizon_days: int = 180
    region_keywords: list[str] = field(default_factory=lambda: list(REGION_KEYWORDS))
    digest_weekday: int = 1
    digest_hour: int = 9
    min_lead_days_warning: int = 2
    quiet: bool = False
    max_immediate: int = 15
    feeds: list[dict[str, Any]] = field(default_factory=list)
    source_priority: list[str] = field(default_factory=lambda: list(SOURCE_PRIORITY))
    non_region_cities: list[str] = field(default_factory=lambda: list(NON_REGION_CITIES))
    artists: list[Artist] = field(default_factory=list)
    venues: list[Venue] = field(default_factory=list)
    promoters: list[Promoter] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _warn_unknown(where: str, d: dict, known: set[str]) -> None:
    for k in d:
        if k not in known:
            log.warning("config: unknown key %r in %s (ignored)", k, where)


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: invalid YAML: {e}") from e


def _str_list(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ConfigError(f"{where} must be a list")
    return [str(x) for x in v if str(x).strip()]


def _num(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{where} must be a number")
    return float(v)


def _items(data: Any, key: str, fname: str) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, dict):
        data = data.get(key, [])
    if data is None:
        return []
    if not isinstance(data, list):
        raise ConfigError(f"{fname}: expected a list")
    return data


def _parse_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("settings.yaml must be a mapping")
    _warn_unknown("settings.yaml", raw, set(DEFAULT_SETTINGS))
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for k, v in raw.items():
        if k in ("home", "maybe_digest") and isinstance(v, dict):
            s[k].update(v)
        elif k in DEFAULT_SETTINGS:
            s[k] = v
    return s


def parse_config(settings_raw: Any, artists_raw: Any = None, venues_raw: Any = None,
                 promoters_raw: Any = None, genres_raw: Any = None) -> Config:
    s = _parse_settings(settings_raw)
    cfg = Config()
    home = s["home"]
    cfg.home_lat = _num(home.get("lat"), "home.lat")
    cfg.home_lon = _num(home.get("lon"), "home.lon")
    cfg.radius_km = _num(home.get("radius_km"), "home.radius_km")
    if cfg.radius_km <= 0:
        raise ConfigError("home.radius_km must be > 0")
    cfg.timezone = str(s["timezone"])
    try:
        ZoneInfo(cfg.timezone)
    except Exception as e:
        raise ConfigError(f"unknown timezone {cfg.timezone!r}") from e
    cfg.horizon_days = int(_num(s["horizon_days"], "horizon_days"))
    kws = _str_list(s["region_keywords"], "region_keywords") + _str_list(
        s["extra_region_keywords"], "extra_region_keywords")
    cfg.region_keywords = list(dict.fromkeys(kws))
    cfg.non_region_cities = _str_list(s["non_region_cities"], "non_region_cities")
    md = s["maybe_digest"]
    cfg.digest_weekday = int(_num(md.get("weekday"), "maybe_digest.weekday"))
    cfg.digest_hour = int(_num(md.get("hour_local"), "maybe_digest.hour_local"))
    if not 1 <= cfg.digest_weekday <= 7:
        raise ConfigError("maybe_digest.weekday must be 1 (Monday) to 7 (Sunday)")
    if not 0 <= cfg.digest_hour <= 23:
        raise ConfigError("maybe_digest.hour_local must be 0-23")
    cfg.min_lead_days_warning = int(_num(s["min_lead_days_warning"], "min_lead_days_warning"))
    cfg.quiet = bool(s["quiet"])
    cfg.max_immediate = int(_num(s["max_immediate"], "max_immediate"))
    cfg.source_priority = _str_list(s["source_priority"], "source_priority")
    feeds = s["feeds"] or []
    if not isinstance(feeds, list):
        raise ConfigError("feeds must be a list")
    for f in feeds:
        if not isinstance(f, dict) or not f.get("url") or not f.get("name"):
            raise ConfigError("each feed needs name and url")
        f.setdefault("tier_default", "maybe")
    cfg.feeds = feeds

    for i, a in enumerate(_items(artists_raw, "artists", "artists.yaml")):
        if isinstance(a, str):
            a = {"name": a}
        if not isinstance(a, dict) or not str(a.get("name", "")).strip():
            raise ConfigError(f"artists.yaml item {i}: missing name")
        _warn_unknown(f"artists.yaml ({a['name']})", a, ARTIST_KEYS)
        tier = a.get("tier", "must-see")
        if tier not in ARTIST_TIERS:
            raise ConfigError(f"artist {a['name']!r}: tier must be one of {ARTIST_TIERS}")
        tm = a.get("title_match", "must-see")
        if tm not in TITLE_MATCH_VALUES:
            raise ConfigError(f"artist {a['name']!r}: title_match must be one of {TITLE_MATCH_VALUES}")
        cfg.artists.append(Artist(
            name=str(a["name"]).strip(), aliases=_str_list(a.get("aliases"), "aliases"),
            tier=tier, exclude=_str_list(a.get("exclude"), "exclude"),
            spotify_id=a.get("spotify_id"), notes=a.get("notes"), title_match=tm))

    for i, v in enumerate(_items(venues_raw, "venues", "venues.yaml")):
        if isinstance(v, str):
            v = {"name": v}
        if not isinstance(v, dict) or not str(v.get("name", "")).strip():
            raise ConfigError(f"venues.yaml item {i}: missing name")
        _warn_unknown(f"venues.yaml ({v['name']})", v, VENUE_KEYS)
        cfg.venues.append(Venue(
            name=str(v["name"]).strip(), aliases=_str_list(v.get("aliases"), "aliases"),
            city=v.get("city"),
            lat=float(v["lat"]) if v.get("lat") is not None else None,
            lon=float(v["lon"]) if v.get("lon") is not None else None))

    for i, p in enumerate(_items(promoters_raw, "promoters", "promoters.yaml")):
        if isinstance(p, str):
            p = {"name": p}
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            raise ConfigError(f"promoters.yaml item {i}: missing name")
        _warn_unknown(f"promoters.yaml ({p['name']})", p, PROMOTER_KEYS)
        cfg.promoters.append(Promoter(
            name=str(p["name"]).strip(), aliases=_str_list(p.get("aliases"), "aliases")))

    if isinstance(genres_raw, dict):
        _warn_unknown("genres.yaml", genres_raw, {"ticketmaster_genres"})
        cfg.genres = _str_list(genres_raw.get("ticketmaster_genres"), "ticketmaster_genres")
    elif isinstance(genres_raw, list):
        cfg.genres = _str_list(genres_raw, "genres")
    elif genres_raw is not None:
        raise ConfigError("genres.yaml: expected a mapping or list")
    return cfg


def load(directory: str | Path) -> Config:
    d = Path(directory)
    settings = _read_yaml(d / "settings.yaml")
    if settings is None:
        raise ConfigError(f"{d / 'settings.yaml'} is required but missing or empty")
    return parse_config(
        settings,
        _read_yaml(d / "artists.yaml"),
        _read_yaml(d / "venues.yaml"),
        _read_yaml(d / "promoters.yaml"),
        _read_yaml(d / "genres.yaml"),
    )


# --- raw artist list editing (preserves unknown keys and key order) ---

def load_artists_raw(directory: str | Path) -> list[dict[str, Any]]:
    data = _read_yaml(Path(directory) / "artists.yaml")
    items = _items(data, "artists", "artists.yaml")
    return [{"name": i} if isinstance(i, str) else i for i in items]


def save_artists_raw(directory: str | Path, items: list[dict[str, Any]]) -> None:
    path = Path(directory) / "artists.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(items, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8")


def add_artist(directory: str | Path, name: str, tier: str = "must-see") -> bool:
    """Append an artist; returns False if one with the same normalised name exists."""
    if tier not in ARTIST_TIERS:
        raise ConfigError(f"tier must be one of {ARTIST_TIERS}")
    items = load_artists_raw(directory)
    existing = {norm(i.get("name", "")) for i in items}
    if norm(name) in existing:
        return False
    items.append({"name": name, "tier": tier})
    save_artists_raw(directory, items)
    return True
