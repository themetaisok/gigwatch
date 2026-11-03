"""Barcelona Open Data (Agenda Cultural) source."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping

import requests
from dateutil import parser as dtparser

from gigwatch.config import Config
from gigwatch.match import has_match
from gigwatch.models import MADRID, Event, make_event, utcnow
from gigwatch.sources.base import SourceError, request_with_retry

log = logging.getLogger("gigwatch")

CKAN_URL = ("https://opendata-ajuntament.barcelona.cat/data/api/3/action/package_show"
            "?id=agenda-cultural")
FALLBACK_URL = ("https://opendata-ajuntament.barcelona.cat/data/dataset/"
                "2767159c-1c98-46b8-a686-2b25b40cb053/resource/"
                "3abb2414-1ee0-446e-9c25-380e938adb73/download")
LABEL = "BCN Open Data"

# Dedicated map for the real 40-column agenda export (verified live).
DEDICATED_REQUIRED = {"register_id", "name", "start_date", "end_date"}
DEDICATED = {
    "title": "name", "start": "start_date", "end": "end_date", "timetable": "timetable",
    "road": "addresses_road_name", "number": "addresses_start_street_number",
    "town": "addresses_town", "lat": "geo_epgs_4326_lat", "lon": "geo_epgs_4326_lon",
    "attr_name": "values_attribute_name", "attr_value": "values_value",
    "description": "values_description", "id": "register_id",
}
URL_ATTRS = ("venda d'entrades", "informació", "informacio", "inscripcions")
MUSIC_NAME_RE = re.compile(
    r"concert|concierto|dj|festival|m[uú]sica|jazz|electr|live|club|sessi[oó]|session|gira|tour",
    re.I)
MAX_SINGLE_SPAN_DAYS = 60  # longer spans are permanent/recurring entries, not shows

# Heuristic fallback (used when the header is not the known one).
FIELD_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("title", ("nom", "titol", "title", "name")),
    ("start", ("data_inici", "inici", "start", "data", "date")),
    ("end", ("data_fi", "fi", "final", "end")),
    ("venue", ("equipament", "lloc", "venue", "adre", "address")),
    ("lat", ("latitud", "lat")),
    ("lon", ("longitud", "lon", "lng")),
    ("url", ("enlla", "url", "link", "entrades", "web")),
    ("category", ("categori", "tipus", "classificaci", "category")),
    ("description", ("descripci", "description")),
]
CATEGORY_RE = re.compile(r"m[uú]sica|music|concert|concierto|dj|club|festival|electr[oò]nica", re.I)
_END_LIKE = re.compile(r"(^|[_\s])(fi|final|end|fin)([_\s]|$)")


@dataclass
class CsvResult:
    events: list[Event] = field(default_factory=list)
    skipped: int = 0   # rows that could not be parsed
    filtered: int = 0  # rows dropped on purpose (not music, past, ...)
    mapping: dict[str, str] = field(default_factory=dict)
    mode: str = "heuristic"


def decode_bytes(b: bytes) -> str:
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return b.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def map_columns(headers: list[str]) -> dict[str, str]:
    """field -> header. Each header is claimed by one field; patterns are tried in priority order."""
    claimed: set[str] = set()
    out: dict[str, str] = {}
    for fld, pats in FIELD_PATTERNS:
        for pat in pats:
            found = None
            for h in headers:
                hl = h.strip().casefold()
                if h in claimed or pat not in hl:
                    continue
                if fld == "start" and pat in ("data", "date") and _END_LIKE.search(hl):
                    continue
                found = h
                break
            if found:
                out[fld] = found
                claimed.add(found)
                break
    unmapped = [h for h in headers if h not in claimed]
    if unmapped:
        log.info("bcn csv: unmapped columns: %s", ", ".join(unmapped))
    return out


_ISO_FIRST = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
# only ":" or "h" separators: "12.11.2026" must never read as 12:11
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])(?:[:h]([0-5]\d)|\s?h\b)")


def parse_dt(value: str) -> tuple[datetime, bool] | None:
    """Return (aware Madrid datetime, time_unknown). ISO strings must not use dayfirst."""
    v = (value or "").strip()
    if not v:
        return None
    try:
        dt = dtparser.parse(v, dayfirst=not _ISO_FIRST.match(v))
    except (ValueError, OverflowError):
        return None
    has_time = bool(re.search(r"\d{1,2}:\d{2}", v)) and not (dt.hour == 0 and dt.minute == 0)
    dt = dt.replace(tzinfo=MADRID) if dt.tzinfo is None else dt.astimezone(MADRID)
    return dt, not has_time


def _hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _float(v: Any) -> float | None:
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _get(row: dict, col: str | None) -> str:
    return (row.get(col) or "").strip() if col else ""


def _read_rows(text: str) -> tuple[list[str], csv.DictReader]:
    first = text.lstrip("\ufeff").split("\n", 1)[0]
    counts = {d: first.count(d) for d in ",;\t|"}
    delim = max(counts, key=counts.get) if any(counts.values()) else ","
    if not any(counts.values()):
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    csv.field_size_limit(1 << 24)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    return list(reader.fieldnames or []), reader


def _dedicated_event(row: dict, m: dict[str, str], today: date, now: datetime,
                     cfg: Config | None = None) -> Event | None:
    """Returns None for rows to filter out; raises on malformed rows.

    With a config, a row is kept when its name looks musical OR it matches a followed artist,
    venue or promoter (so "Fontaines D.C." survives). Without one, every future row is kept."""
    name = _get(row, m["title"])
    if not name:
        return None
    s = parse_dt(_get(row, m["start"]))
    e = parse_dt(_get(row, m["end"]))
    if s is None and e is None:
        raise ValueError("no parseable date")
    s_dt, s_unknown = s if s else (e[0], True)  # type: ignore[index]
    e_dt = e[0] if e else None
    if max(s_dt.date(), e_dt.date() if e_dt else s_dt.date()) < today:
        return None
    end = None
    time_unknown = s_unknown
    if e_dt and e_dt.date() > s_dt.date():
        if (e_dt.date() - s_dt.date()).days > MAX_SINGLE_SPAN_DAYS:
            return None  # permanent / recurring entry
        start = s_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        time_unknown = True
        end = e_dt
    else:
        start = s_dt
        tm = _TIME_RE.search(_get(row, m.get("timetable")))
        if tm:
            start = s_dt.replace(hour=int(tm.group(1)), minute=int(tm.group(2) or 0), second=0,
                                 microsecond=0)
            time_unknown = False
    url = None
    if _get(row, m.get("attr_name")).casefold() in URL_ATTRS:
        val = _get(row, m.get("attr_value"))
        if val.lower().startswith(("http://", "https://")):
            url = val
    road = _get(row, m.get("road"))
    num = _get(row, m.get("number"))
    venue = (f"{road} {num}".strip() if road else None)
    ev = make_event(
        source="bcn_opendata", title=name, fetched_at=now,
        source_ids={f"bcn:{_hash(name, start.isoformat(), venue)}"},
        venue=venue, city=_get(row, m.get("town")) or "Barcelona",
        lat=_float(_get(row, m.get("lat"))), lon=_float(_get(row, m.get("lon"))),
        start=start, end=end, url=url, status="unknown", time_unknown=time_unknown,
        description=_get(row, m.get("description")), sources_urls={LABEL: url or ""})
    if cfg is not None and not (MUSIC_NAME_RE.search(name) or has_match(ev, cfg)):
        return None
    return ev


def _heuristic_event(row: dict, m: dict[str, str], now: datetime,
                     cfg: Config | None = None) -> Event | None:
    cat_col = m.get("category")
    category_ok = not cat_col or bool(CATEGORY_RE.search(_get(row, cat_col).casefold()))
    if not category_ok and cfg is None:
        return None
    title = _get(row, m.get("title"))
    s = parse_dt(_get(row, m.get("start")))
    if not title or s is None:
        raise ValueError("missing title or date")
    start, unknown = s
    e = parse_dt(_get(row, m.get("end")))
    venue = _get(row, m.get("venue")) or None
    url = _get(row, m.get("url")) or None
    if url and not url.lower().startswith(("http://", "https://")):
        url = None
    ev = make_event(
        source="bcn_opendata", title=title, fetched_at=now,
        source_ids={f"bcn:{_hash(title, start.isoformat(), venue)}"},
        venue=venue, city="Barcelona", lat=_float(_get(row, m.get("lat"))),
        lon=_float(_get(row, m.get("lon"))), start=start, end=e[0] if e else None, url=url,
        time_unknown=unknown, description=_get(row, m.get("description")),
        sources_urls={LABEL: url or ""})
    if not category_ok and not has_match(ev, cfg):
        return None
    return ev


def parse_csv_ex(text: str, now: datetime | None = None, cfg: Config | None = None) -> CsvResult:
    now = now or utcnow()
    today = now.astimezone(MADRID).date()
    res = CsvResult()
    try:
        headers, reader = _read_rows(text)
    except Exception as e:  # noqa: BLE001 - weird file must not crash
        log.warning("bcn csv: cannot read header: %s", e)
        return res
    if DEDICATED_REQUIRED <= set(headers):
        res.mode = "dedicated"
        res.mapping = {k: v for k, v in DEDICATED.items() if v in headers}
    else:
        res.mode = "heuristic"
        res.mapping = map_columns(headers)
        if "title" not in res.mapping or "start" not in res.mapping:
            log.warning("bcn csv: could not find title/start columns in %s", headers[:10])
            return res
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except Exception:  # noqa: BLE001 - csv.Error on a bad line
            res.skipped += 1
            continue
        try:
            ev = (_dedicated_event(row, res.mapping, today, now, cfg) if res.mode == "dedicated"
                  else _heuristic_event(row, res.mapping, now, cfg))
        except Exception:  # noqa: BLE001
            res.skipped += 1
            continue
        if ev is None:
            res.filtered += 1
        else:
            res.events.append(ev)
    log.info("bcn csv (%s): %d events, %d skipped, %d filtered", res.mode, len(res.events),
             res.skipped, res.filtered)
    return res


def parse_csv(text: str, now: datetime | None = None, cfg: Config | None = None) -> list[Event]:
    return parse_csv_ex(text, now, cfg).events


# The city portal sits behind a WAF that answers 403 to non-browser user agents
# from datacenter IPs (seen from GitHub Actions). A browser-like UA is accepted.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/json,text/plain,*/*;q=0.8",
    "Accept-Language": "ca,es;q=0.9,en;q=0.8",
}


def resolve_csv_url(session: requests.Session | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> str:
    session = session or requests.Session()
    try:
        r = request_with_retry(session, CKAN_URL, retries=1, sleep=sleep, headers=BROWSER_HEADERS)
        for res in r.json()["result"]["resources"]:
            if str(res.get("format", "")).upper() == "CSV" and res.get("url"):
                return res["url"]
        log.warning("bcn: no CSV resource in CKAN response; using fallback URL")
    except Exception as e:  # noqa: BLE001
        log.warning("bcn: CKAN lookup failed (%s); using fallback URL", e)
    return FALLBACK_URL


class BcnOpenDataSource:
    name = "bcn_opendata"
    priority = 1

    def __init__(self, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep

    def enabled(self, env: Mapping[str, str]) -> bool:
        return True

    def fetch(self, cfg: Config, now: datetime) -> list[Event]:
        url = resolve_csv_url(self.session, self.sleep)
        try:
            r = request_with_retry(self.session, url, timeout=120, sleep=self.sleep,
                                   headers=BROWSER_HEADERS)
        except SourceError as e:
            if "403" in str(e):
                raise SourceError("HTTP 403: the city portal blocks this network "
                                  "(GitHub Actions IPs); works from a home network") from e
            raise
        res = parse_csv_ex(decode_bytes(r.content), now, cfg)
        if not res.mapping or (res.mode == "heuristic"
                               and not {"title", "start"} <= set(res.mapping)):
            raise SourceError("unrecognised BCN CSV format")
        return res.events
