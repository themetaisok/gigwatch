"""Persistent state, diffing and digest timing."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gigwatch.dedupe import key as dedupe_key
from gigwatch.match import match_artist
from gigwatch.normalize import norm
from gigwatch.models import (CHANGE, MADRID, NEW, ON_SALE, REMINDER_1D, REMINDER_7D, TIER_MUST,
                             Action, Event, make_event)

if TYPE_CHECKING:
    from gigwatch.config import Config

STRUCTURED = {"ticketmaster", "bcn_opendata", "bandsintown"}
PREFIX_SOURCE = {"tm": "ticketmaster", "bcn": "bcn_opendata", "bit": "bandsintown",
                 "inbox": "inbox", "feed": "feeds"}
SEEN_CAP = 2000
CHANGE_STATUSES = ("canceled", "postponed", "rescheduled")


class StateError(Exception):
    pass


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sources_of(ev: Event) -> list[str]:
    return sorted({PREFIX_SOURCE.get(i.split(":", 1)[0], i.split(":", 1)[0]) for i in ev.source_ids})


def event_hash(ev: Event) -> str:
    raw = f"{ev.start.isoformat() if ev.start else ''}|{ev.status}|{bool(ev.url)}|{ev.venue or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def snapshot(ev: Event) -> dict[str, Any]:
    return {
        "title": ev.title,
        "start": ev.start.astimezone(MADRID).isoformat() if ev.start else "",
        "venue": ev.venue or "",
        "city": ev.city or "",
        "url": ev.url or "",
        "status": ev.status,
        "artists": list(ev.artists),
        "tier": ev.tier or "",
        "sources": sources_of(ev),
        "time_unknown": ev.time_unknown,
        "onsale_at": iso_utc(ev.onsale_at) if ev.onsale_at else "",
    }


def new_record(now: datetime) -> dict[str, Any]:
    return {
        "first_seen": iso_utc(now), "last_seen": iso_utc(now), "notified_tier": None,
        "last_hash": "", "reminders_sent": [], "missed_runs": 0, "possibly_removed": False,
        "removed_notified": False, "digested": False, "overflow": False, "snapshot": {},
        "source_ids": [], "keys": [],
    }


def rec_to_event(uid: str, rec: dict[str, Any]) -> Event:
    s = rec.get("snapshot") or {}
    first_seen = parse_iso(rec.get("first_seen")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return make_event(
        source=(s.get("sources") or ["state"])[0], title=s.get("title", ""), fetched_at=first_seen,
        uid=uid, artists=list(s.get("artists") or []), venue=s.get("venue") or None,
        city=s.get("city") or None, url=s.get("url") or None, status=s.get("status") or "unknown",
        start=parse_iso(s.get("start")), tier=s.get("tier") or None,
        time_unknown=bool(s.get("time_unknown")), onsale_at=parse_iso(s.get("onsale_at")),
        sources_urls={},
    )


@dataclass
class State:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_digest_at: str | None = None
    last_source_warning_at: str | None = None
    inbox_seen: list[str] = field(default_factory=list)
    feed_seen: list[str] = field(default_factory=list)
    version: int = 1
    # None = not recorded (older state files): then "first run" means no events and no digest yet.
    first_run_done: bool | None = None

    def is_first_run(self) -> bool:
        if self.first_run_done is not None:
            return not self.first_run_done
        return not (self.events or self.last_digest_at)

    @classmethod
    def load(cls, path: str | Path) -> "State":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise StateError(f"cannot read state file {p}: {e}") from e
        if not isinstance(d, dict) or d.get("version", 1) != 1:
            raise StateError(f"unsupported state file {p}")
        return cls(
            events=d.get("events") or {}, last_digest_at=d.get("last_digest_at"),
            last_source_warning_at=d.get("last_source_warning_at"),
            inbox_seen=list(d.get("inbox_seen") or []), feed_seen=list(d.get("feed_seen") or []),
            first_run_done=d.get("first_run_done"))

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "events": self.events, "last_digest_at": self.last_digest_at,
                "last_source_warning_at": self.last_source_warning_at,
                "inbox_seen": self.inbox_seen, "feed_seen": self.feed_seen,
                "first_run_done": self.first_run_done}

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
                       + "\n", encoding="utf-8")
        os.replace(tmp, p)

    def _mark(self, lst: list[str], h: str) -> None:
        if h not in lst:
            lst.append(h)
        del lst[:-SEEN_CAP]

    def mark_feed_seen(self, h: str) -> None:
        self._mark(self.feed_seen, h)

    def mark_inbox_seen(self, h: str) -> None:
        self._mark(self.inbox_seen, h)


@dataclass
class DiffResult:
    actions: list[Action] = field(default_factory=list)
    previous: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    def revert(self, state: State, uid: str) -> None:
        prev = self.previous.get(uid)
        if prev is None:
            state.events.pop(uid, None)
        else:
            state.events[uid] = prev


def detect_change(old: dict[str, Any], new: dict[str, Any]) -> str | None:
    if new["status"] in CHANGE_STATUSES and old.get("status") != new["status"]:
        return new["status"]
    if not old.get("start") and new["start"]:
        return "date"  # an undated alert now has a date
    if old.get("start") != new["start"]:
        return "start"
    if not old.get("url") and new["url"]:
        return "url"
    return None


def _is_past(ev: Event, now: datetime) -> bool:
    if ev.start is None:
        return False
    if ev.time_unknown:
        return ev.start.astimezone(MADRID).date() < now.astimezone(MADRID).date()
    return ev.start <= now


def _reminders(rec: dict, ev: Event, now: datetime, cfg: "Config", first_sight: bool,
               out: list[Action]) -> None:
    if ev.start is None or _is_past(ev, now) or ev.status in ("canceled", "postponed"):
        return
    sent: list[str] = rec["reminders_sent"]
    delta = ev.start - now
    due = [k for k, d in (("1d", timedelta(days=1)), ("7d", timedelta(days=7)))
           if delta <= d and k not in sent]
    if due:
        sent.extend(due)
        if not first_sight and not cfg.quiet:
            out.append(Action(REMINDER_1D if "1d" in due else REMINDER_7D, ev))
    if ev.onsale_at and ev.onsale_at <= now and "onsale" not in sent:
        if first_sight:
            sent.append("onsale")
        elif ev.url and ev.status == "onsale":
            sent.append("onsale")
            if not cfg.quiet:
                out.append(Action(ON_SALE, ev))


def followed_primary(ev: Event, cfg: "Config") -> str:
    """Normalised name of the first followed artist matched by the event, or ''."""
    hits = match_artist(ev, cfg.artists)
    return norm(hits[0].artist.name) if hits else ""


def _day(ev: Event) -> str:
    return ev.start.astimezone(MADRID).date().isoformat() if ev.start else ""


def reconcile_uids(state: State, events: list[Event], cfg: "Config", res: "DiffResult") -> None:
    """Keep uids stable when the set of sources behind an event changes between runs.

    An event reuses the uid of an existing record that
      1. is its own uid, or shares a source id with it (an ICS id only if the date is the same:
         recurring calendars reuse one UID for every instance), or
      2. has the same dedupe key (followed artist + local date + canonical venue), or
      3. has the same followed artist and date while the record or the event has no venue
         (an email alert later seen in a listing), if exactly one record matches; or, for a
         dated event, the one undated record of that artist (the date is now confirmed)."""
    by_sid: dict[str, str] = {}
    by_key: dict[str, str] = {}
    by_artist: dict[tuple[str, str], list[str]] = {}
    for uid, rec in state.events.items():
        for sid in rec.get("source_ids") or []:
            by_sid.setdefault(sid, uid)
        for k in rec.get("keys") or []:
            by_key.setdefault(k, uid)
        if rec.get("artist"):
            by_artist.setdefault((rec["artist"], rec.get("date") or ""), []).append(uid)
    own = {e.uid for e in events}
    for ev in events:
        day = _day(ev)
        cands: list[str] = []
        if ev.uid in state.events:
            cands.append(ev.uid)
        for sid in sorted(ev.source_ids):
            u = by_sid.get(sid)
            if u is None:
                continue
            rec_day = state.events[u].get("date") or ""
            if sid.startswith("feed:ics:") and day and rec_day and rec_day != day:
                continue
            cands.append(u)
        k = dedupe_key(ev, cfg.artists, cfg.venues)
        if k in by_key:
            cands.append(by_key[k])
        cands = [c for c in dict.fromkeys(cands) if c in state.events]
        if not cands and day:
            fa = followed_primary(ev, cfg)
            if fa:
                same = [u for u in by_artist.get((fa, day), [])
                        if u in state.events and u not in own
                        and (not ev.venue or not (state.events[u].get("snapshot") or {}).get("venue"))]
                if len(same) != 1:
                    same = [u for u in by_artist.get((fa, ""), [])
                            if u in state.events and u not in own]
                if len(same) == 1:
                    cands = same
        if not cands:
            continue
        target = cands[0]
        if target != ev.uid and target in own:
            continue  # that record belongs to another event of this run
        ev.uid = target
        own.add(target)
        for c in cands[1:]:  # records now merged into this event
            old_ids = set(state.events[c].get("source_ids") or [])
            if c not in own and old_ids and old_ids <= ev.source_ids:
                res.previous.setdefault(c, copy.deepcopy(state.events[c]))
                del state.events[c]


def _keep_precise_time(rec: dict, ev: Event) -> None:
    """A source that only knows the date must not overwrite a time we already know."""
    old = rec.get("snapshot") or {}
    old_start = parse_iso(old.get("start"))
    if (ev.time_unknown and ev.start and old_start and not old.get("time_unknown")
            and old_start.astimezone(MADRID).date() == ev.start.astimezone(MADRID).date()):
        ev.start = old_start
        ev.time_unknown = False


def diff(state: State, events: list[Event], now: datetime, cfg: "Config",
         ok_sources: set[str]) -> DiffResult:
    """Update state in place from this run's tiered events and return actions to send."""
    res = DiffResult()
    reconcile_uids(state, events, cfg, res)
    seen: set[str] = set()
    new_uids: set[str] = set()
    live: dict[str, Event] = {}
    for ev in events:
        uid = ev.uid
        seen.add(uid)
        live[uid] = ev
        rec = state.events.get(uid)
        res.previous.setdefault(uid, copy.deepcopy(rec))
        if rec is None:
            rec = new_record(now)
            state.events[uid] = rec
        _keep_precise_time(rec, ev)
        snap = snapshot(ev)
        h = event_hash(ev)
        old_snap = rec["snapshot"]

        if ev.tier == TIER_MUST:
            if rec["notified_tier"] != TIER_MUST:
                new_uids.add(uid)
                if ev.status != "canceled":
                    short = (ev.start is not None
                             and ev.start - now < timedelta(days=cfg.min_lead_days_warning))
                    res.actions.append(Action(NEW, ev, short_notice=short))
                rec["notified_tier"] = TIER_MUST
                rec["digested"] = True
            elif rec["last_hash"] != h:
                change = detect_change(old_snap, snap)
                if change:
                    res.actions.append(Action(CHANGE, ev, change=change))
        elif rec["notified_tier"] is None:
            rec["notified_tier"] = ev.tier
            rec["digested"] = False

        rec.update(snapshot=snap, last_hash=h, last_seen=iso_utc(now), missed_runs=0,
                   possibly_removed=False, removed_notified=False)
        rec["source_ids"] = sorted(set(rec.get("source_ids") or []) | ev.source_ids)
        rec["keys"] = sorted(set(rec.get("keys") or []) | {dedupe_key(ev, cfg.artists, cfg.venues)})
        rec["artist"] = followed_primary(ev, cfg)
        rec["date"] = _day(ev)

    # Reminders run over every must-see record, including ones no source returned this run.
    for uid, rec in list(state.events.items()):
        ev = live.get(uid)
        if ev is None:
            if rec.get("notified_tier") != TIER_MUST or rec.get("possibly_removed"):
                continue
            if (rec.get("snapshot") or {}).get("tier") != TIER_MUST:
                continue
            ev = rec_to_event(uid, rec)
        elif ev.tier != TIER_MUST:
            continue
        res.previous.setdefault(uid, copy.deepcopy(rec))
        _reminders(rec, ev, now, cfg, uid in new_uids, res.actions)

    for uid, rec in state.events.items():
        if uid in seen:
            continue
        snap = rec.get("snapshot") or {}
        srcs = set(snap.get("sources") or [])
        start = parse_iso(snap.get("start"))
        if not srcs or not srcs <= STRUCTURED or not srcs <= ok_sources:
            continue
        if start is None or start <= now:
            continue
        res.previous.setdefault(uid, copy.deepcopy(rec))
        rec["missed_runs"] += 1
        if rec["missed_runs"] >= 3 and not rec["possibly_removed"]:
            rec["possibly_removed"] = True
            rec["removed_notified"] = False

    prune(state, now)
    return res


def prune(state: State, now: datetime) -> None:
    for uid in list(state.events):
        rec = state.events[uid]
        snap = rec.get("snapshot") or {}
        start = parse_iso(snap.get("start"))
        if start is not None:
            if start < now - timedelta(days=7):
                del state.events[uid]
        else:
            last = parse_iso(rec.get("last_seen")) or now
            if last < now - timedelta(days=60):
                del state.events[uid]


# --- digest timing and content ---

def latest_digest_instant(now: datetime, cfg: "Config") -> datetime:
    """Most recent weekday/hour_local (Madrid wall clock) at or before now, as aware datetime."""
    local = now.astimezone(cfg.tz)
    back = (local.isoweekday() - cfg.digest_weekday) % 7
    d = (local - timedelta(days=back)).replace(
        hour=cfg.digest_hour, minute=0, second=0, microsecond=0)
    if d > local:
        d = (local - timedelta(days=back + 7)).replace(
            hour=cfg.digest_hour, minute=0, second=0, microsecond=0)
    return d


def digest_due(state: State, now: datetime, cfg: "Config") -> bool:
    last = parse_iso(state.last_digest_at)
    return last is None or last < latest_digest_instant(now, cfg)


@dataclass
class DigestContent:
    held: list[tuple[str, Event]] = field(default_factory=list)
    maybe: list[tuple[str, Event]] = field(default_factory=list)
    heads: list[tuple[str, Event]] = field(default_factory=list)
    removed: list[tuple[str, Event]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.held or self.maybe or self.heads or self.removed)


def collect_digest(state: State) -> DigestContent:
    out = DigestContent()
    for uid, rec in sorted(state.events.items()):
        ev = rec_to_event(uid, rec)
        if not rec["digested"]:
            if rec.get("overflow") or ev.tier == TIER_MUST:
                out.held.append((uid, ev))
            elif ev.tier == "heads-up":
                out.heads.append((uid, ev))
            else:
                out.maybe.append((uid, ev))
        if rec["possibly_removed"] and not rec.get("removed_notified"):
            out.removed.append((uid, ev))
    far = datetime.max.replace(tzinfo=MADRID)
    for lst in (out.held, out.maybe, out.heads, out.removed):
        lst.sort(key=lambda t: (t[1].start or far, t[0]))
    return out


def mark_digested(state: State, content: DigestContent) -> None:
    for uid, _ in [*content.held, *content.maybe, *content.heads]:
        rec = state.events.get(uid)
        if rec:
            rec["digested"] = True
            rec["overflow"] = False
    for uid, _ in content.removed:
        rec = state.events.get(uid)
        if rec:
            rec["removed_notified"] = True
