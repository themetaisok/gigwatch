"""Bandsintown source (optional; needs BANDSINTOWN_APP_ID)."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Mapping
from urllib.parse import quote

import requests
from dateutil import parser as dtparser

from gigwatch.config import Config
from gigwatch.models import MADRID, Event, make_event
from gigwatch.sources.base import SourceError, request_with_retry

log = logging.getLogger("gigwatch")
LABEL = "Bandsintown"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def map_event(e: dict, now: datetime) -> Event | None:
    try:
        start = dtparser.isoparse(e["datetime"])
    except (KeyError, ValueError, TypeError):
        return None
    start = start.replace(tzinfo=MADRID) if start.tzinfo is None else start.astimezone(MADRID)
    v = e.get("venue") or {}
    offers = e.get("offers") or []
    offer = offers[0] if offers else {}
    url = offer.get("url") or e.get("url")
    return make_event(
        source="bandsintown", title=e.get("title") or ", ".join(e.get("lineup") or []),
        fetched_at=now, source_ids={f"bit:{e.get('id') or e.get('datetime')}:{v.get('name')}"},
        artists=list(e.get("lineup") or []), venue=v.get("name"), city=v.get("city"),
        lat=_f(v.get("latitude")), lon=_f(v.get("longitude")), start=start, url=url,
        status="onsale" if str(offer.get("status", "")).lower() == "available" else "unknown",
        sources_urls={LABEL: url or ""})


class BandsintownSource:
    name = "bandsintown"
    priority = 2

    def __init__(self, env: Mapping[str, str], session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.app_id = env.get("BANDSINTOWN_APP_ID") or None
        self.session = session or requests.Session()
        self.sleep = sleep

    def enabled(self, env: Mapping[str, str]) -> bool:
        return bool(env.get("BANDSINTOWN_APP_ID"))

    def fetch(self, cfg: Config, now: datetime) -> list[Event]:
        out: list[Event] = []
        ok = 0
        for a in cfg.artists:
            url = f"https://rest.bandsintown.com/artists/{quote(a.name, safe='')}/events"
            try:
                r = request_with_retry(self.session, url, sleep=self.sleep,
                                       params={"app_id": self.app_id, "date": "upcoming"})
                data = r.json()
            except (SourceError, ValueError) as e:
                log.warning("bandsintown %s failed: %s", a.name, e)
                continue
            ok += 1
            if isinstance(data, list):
                out.extend(ev for e in data if (ev := map_event(e, now)))
        if cfg.artists and ok == 0:
            raise SourceError("all bandsintown requests failed")
        return out
