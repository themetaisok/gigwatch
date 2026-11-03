"""Ticketmaster Discovery API source."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import requests
from dateutil import parser as dtparser

from gigwatch.config import Config
from gigwatch.geo import geohash
from gigwatch.models import MADRID, Event, make_event
from gigwatch.sources.base import SourceError, request_with_retry

log = logging.getLogger("gigwatch")
TM_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
LABEL = "Ticketmaster"
STATUS_MAP = {"onsale": "onsale", "offsale": "offsale", "canceled": "canceled",
              "cancelled": "canceled", "postponed": "postponed", "rescheduled": "rescheduled"}


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_event(e: dict[str, Any], now: datetime) -> Event | None:
    eid = e.get("id")
    start_d = (e.get("dates") or {}).get("start") or {}
    start: datetime | None = None
    time_unknown = False
    try:
        if start_d.get("dateTime"):
            start = dtparser.isoparse(start_d["dateTime"]).astimezone(MADRID)
            time_unknown = bool(start_d.get("timeTBA") or start_d.get("noSpecificTime"))
        elif start_d.get("localDate"):
            day = datetime.strptime(start_d["localDate"], "%Y-%m-%d")
            lt = start_d.get("localTime")
            hh, mm = 0, 0
            try:
                t = datetime.strptime(str(lt), "%H:%M:%S" if str(lt).count(":") == 2 else "%H:%M")
                hh, mm = t.hour, t.minute
            except ValueError:
                time_unknown = True
            start = day.replace(hour=hh, minute=mm, tzinfo=MADRID)
            if start_d.get("timeTBA") or start_d.get("noSpecificTime"):
                time_unknown = True
    except (ValueError, TypeError):
        start = None
    if not eid or start is None:
        return None
    if TEST_NAME_RE.search(e.get("name") or "") or e.get("test") is True:
        return None  # Ticketmaster test listings ("Test 11068686 case") leak through includeTest=no

    emb = e.get("_embedded") or {}
    venues = emb.get("venues") or []
    v = venues[0] if venues else {}
    loc = v.get("location") or {}
    genres: list[str] = []
    for c in e.get("classifications") or []:
        for k in ("segment", "genre", "subGenre"):
            n = (c.get(k) or {}).get("name")
            if n and n.lower() != "undefined" and n not in genres:
                genres.append(n)
    onsale = None
    pub = ((e.get("sales") or {}).get("public") or {}).get("startDateTime")
    if pub:
        try:
            onsale = dtparser.isoparse(pub).astimezone(MADRID)
        except ValueError:
            onsale = None
    code = str(((e.get("dates") or {}).get("status") or {}).get("code", "")).lower()
    url = e.get("url")
    return make_event(
        source="ticketmaster", title=e.get("name") or "", fetched_at=now,
        source_ids={f"tm:{eid}"},
        artists=[a["name"] for a in emb.get("attractions") or [] if a.get("name")],
        venue=v.get("name"), city=(v.get("city") or {}).get("name"),
        lat=_float(loc.get("latitude")), lon=_float(loc.get("longitude")),
        start=start, url=url, status=STATUS_MAP.get(code, "unknown"), genres=genres,
        onsale_at=onsale, time_unknown=time_unknown, description=e.get("info") or "",
        sources_urls={LABEL: url or ""},
    )


TEST_NAME_RE = re.compile(r"^\s*test\b|\btest event\b|\bdo not (buy|purchase)\b", re.I)


class TicketmasterSource:
    name = "ticketmaster"
    priority = 0

    def __init__(self, env: Mapping[str, str], session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.apikey = env.get("TICKETMASTER_API_KEY") or None
        self.session = session or requests.Session()
        self.sleep = sleep
        self._cache: dict[tuple, dict] = {}

    def enabled(self, env: Mapping[str, str]) -> bool:
        return bool(env.get("TICKETMASTER_API_KEY"))

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        k = tuple(sorted((a, str(b)) for a, b in params.items()))
        if k in self._cache:
            return self._cache[k]
        self.sleep(0.25)  # <= 4 req/s
        r = request_with_retry(self.session, TM_URL, params=params, sleep=self.sleep)
        try:
            data = r.json()
        except ValueError as e:
            raise SourceError("ticketmaster returned invalid JSON") from e
        self._cache[k] = data
        return data

    def _paged(self, params: dict[str, Any], size: int) -> list[dict]:
        out: list[dict] = []
        page = 0
        while True:
            data = self._get({**params, "page": page})
            evs = (data.get("_embedded") or {}).get("events")
            if not evs:
                break
            out.extend(evs)
            pg = data.get("page") or {}
            if pg.get("number", page) + 1 >= pg.get("totalPages", 1) or (page + 1) * size >= 1000:
                break
            page += 1
        return out

    def fetch(self, cfg: Config, now: datetime) -> list[Event]:
        common = {
            "apikey": self.apikey, "countryCode": "ES", "classificationName": "Music",
            "includeTest": "no",
            "locale": "*", "startDateTime": _fmt_utc(now),
            "endDateTime": _fmt_utc(now + timedelta(days=cfg.horizon_days)),
        }
        raw: dict[str, dict] = {}
        ok = 0
        errors: list[str] = []

        def run(params: dict, size: int, label: str) -> None:
            nonlocal ok
            try:
                for e in self._paged(params, size):
                    raw.setdefault(e.get("id") or str(len(raw)), e)
                ok += 1
            except SourceError as ex:
                errors.append(f"{label}: {ex}")
                log.warning("ticketmaster %s failed: %s", label, ex)

        run({**common, "geoPoint": geohash(cfg.home_lat, cfg.home_lon, 5),
             "radius": int(cfg.radius_km), "unit": "km", "size": 200, "sort": "date,asc"},
            200, "geo search")
        for a in cfg.artists:
            run({**common, "keyword": a.name, "size": 50}, 50, f"artist {a.name}")
        if ok == 0:
            raise SourceError("; ".join(errors[:3]) or "no requests succeeded")
        events = [ev for e in raw.values() if (ev := map_event(e, now))]
        log.info("ticketmaster: %d events (%d requests failed)", len(events), len(errors))
        return events
