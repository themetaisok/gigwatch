"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")

TIER_MUST = "must-see"
TIER_MAYBE = "maybe"
TIER_HEADS = "heads-up"

# Action kinds
NEW = "IMMEDIATE_NEW"
CHANGE = "IMMEDIATE_CHANGE"
REMINDER_7D = "REMINDER_7D"
REMINDER_1D = "REMINDER_1D"
ON_SALE = "ON_SALE"

# Higher is worse.
STATUS_RANK = {"canceled": 3, "postponed": 2, "rescheduled": 1}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Event:
    uid: str
    source: str
    source_ids: set[str]
    title: str
    artists: list[str]
    venue: str | None
    city: str | None
    lat: float | None
    lon: float | None
    start: datetime | None  # tz-aware Europe/Madrid; None only for heads-up
    end: datetime | None
    url: str | None
    status: str  # onsale|offsale|canceled|postponed|rescheduled|unknown
    genres: list[str]
    raw_tier_hint: str | None  # must-see|maybe|heads-up
    fetched_at: datetime
    onsale_at: datetime | None = None
    tier: str | None = None
    sources_urls: dict[str, str] = field(default_factory=dict)
    time_unknown: bool = False
    description: str = ""

    def local_date(self) -> date | None:
        return self.start.astimezone(MADRID).date() if self.start else None


def make_event(*, source: str, title: str, fetched_at: datetime, **kw: Any) -> Event:
    """Build an Event with sensible defaults for the optional fields."""
    base: dict[str, Any] = dict(
        uid="",
        source_ids=set(),
        artists=[],
        venue=None,
        city=None,
        lat=None,
        lon=None,
        start=None,
        end=None,
        url=None,
        status="unknown",
        genres=[],
        raw_tier_hint=None,
    )
    base.update(kw)
    return Event(source=source, title=title, fetched_at=fetched_at, **base)


@dataclass
class Action:
    kind: str
    event: Event
    short_notice: bool = False
    change: str | None = None  # start|canceled|postponed|rescheduled|url
