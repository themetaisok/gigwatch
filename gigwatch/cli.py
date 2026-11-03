"""Command line interface and the run pipeline."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

from gigwatch import config as config_mod
from gigwatch.config import Config, ConfigError
from gigwatch.dedupe import dedupe
from gigwatch.geo import distance_km, in_region
from gigwatch.match import assign_tier, venue_match
from gigwatch.models import (CHANGE, MADRID, NEW, TIER_HEADS, TIER_MAYBE, TIER_MUST, Action, Event,
                             utcnow)
from gigwatch.notify_telegram import (DryRunNotifier, Notifier, TelegramClient, format_digest,
                                      format_event, format_first_run, format_overflow)
from gigwatch.render import write_outputs
from gigwatch.sources.bandsintown import BandsintownSource
from gigwatch.sources.base import Source
from gigwatch.sources.bcn_opendata import BcnOpenDataSource
from gigwatch.sources.feeds import FeedsSource
from gigwatch.sources.inbox import InboxSource
from gigwatch.sources.ticketmaster import TicketmasterSource
from gigwatch.state import (State, StateError, collect_digest, diff, digest_due, iso_utc,
                            mark_digested, parse_iso, rec_to_event)

log = logging.getLogger("gigwatch")
STRUCTURED_SOURCES = {"ticketmaster", "bcn_opendata"}
WARNING_TEXT = ("⚠️ gigwatch: no structured source could be fetched ({names}). "
                "Alerts may be missing until this recovers. Check the Actions log.")


@dataclass
class RunReport:
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    fetched: int = 0
    must: int = 0
    maybe: int = 0
    heads: int = 0
    sent: int = 0
    first_run: bool = False
    dry_run: bool = False
    messages: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    send_failures: int = 0
    render_failed: bool = False
    summary: str = ""


def build_sources(cfg: Config, state: State, env: Mapping[str, str], *, dry_run: bool,
                  session: requests.Session | None = None,
                  sleep: Callable[[float], None] = time.sleep,
                  imap_factory: Callable[[str], Any] | None = None) -> list[Source]:
    return [
        TicketmasterSource(env, session=session, sleep=sleep),
        BcnOpenDataSource(session=session, sleep=sleep),
        BandsintownSource(env, session=session, sleep=sleep),
        InboxSource(env, state, mark_seen=not dry_run, imap_factory=imap_factory),
        FeedsSource(cfg, state, session=session, sleep=sleep),
    ]


def enrich_coords(events: Sequence[Event], cfg: Config) -> None:
    """Fill missing coordinates and city from a matching configured venue."""
    for ev in events:
        if ev.venue:
            v = venue_match(ev.venue, cfg.venues)
            if not v:
                continue
            if ev.lat is None and v.lat is not None and v.lon is not None:
                ev.lat, ev.lon = v.lat, v.lon
            if not ev.city and v.city:
                ev.city = v.city


def filter_events(events: Sequence[Event], cfg: Config, now: datetime) -> list[Event]:
    end = now + timedelta(days=cfg.horizon_days)
    today = now.astimezone(MADRID).date()
    out: list[Event] = []
    for ev in events:
        if ev.start is not None:
            past = (ev.start.astimezone(MADRID).date() < today if ev.time_unknown
                    else ev.start < now)
            if past or ev.start > end:
                continue
            no_loc = ev.lat is None and not ev.venue and not ev.city
            if ev.raw_tier_hint and ev.lat is None:
                pass  # emails / feed items: assign_tier caps ones that mention another city
            elif ev.source in ("inbox", "feeds") and no_loc:
                pass  # nothing to filter on; trust the user's own filters
            elif not in_region(ev, cfg):
                continue
        out.append(ev)
    return out


def _far() -> datetime:
    return datetime.max.replace(tzinfo=MADRID)


def run(config_dir: str | Path = "config", state_path: str | Path = "state/state.json",
        out_dir: str | Path = "docs", *, dry_run: bool = False, since_days: int | None = None,
        now: datetime | None = None, env: Mapping[str, str] | None = None,
        notifier: Notifier | None = None, sources: Sequence[Source] | None = None,
        session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
        imap_factory: Callable[[str], Any] | None = None) -> tuple[int, RunReport]:
    now = now or utcnow()
    env = {k: v for k, v in (os.environ if env is None else env).items() if v}
    report = RunReport(dry_run=dry_run)
    try:
        cfg = config_mod.load(config_dir)
        state = State.load(state_path)
    except (ConfigError, StateError) as e:
        log.error("%s", e)
        return 2, report
    if since_days:
        cfg.horizon_days = since_days
    report.first_run = state.is_first_run()

    if notifier is None:
        if dry_run:
            notifier = DryRunNotifier()
        elif env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
            notifier = TelegramClient(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"],
                                      session=session, sleep=sleep)
        else:
            log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required (or use --dry-run)")
            return 2, report

    def deliver(text: str, preview: bool = True) -> bool:
        ok = notifier.send(text, preview)
        if ok:
            report.sent += 1
            report.messages.append(text)
        else:
            report.send_failures += 1
        return ok

    # 2. fetch
    srcs = list(sources) if sources is not None else build_sources(
        cfg, state, env, dry_run=dry_run, session=session, sleep=sleep, imap_factory=imap_factory)
    raw: list[Event] = []
    enabled: list[str] = []
    for s in srcs:
        if not s.enabled(env):
            continue
        enabled.append(s.name)
        try:
            evs = s.fetch(cfg, now)
        except Exception as e:  # noqa: BLE001 - one source must never kill the run
            log.error("source %s failed: %s", s.name, e)
            report.failed.append(s.name)
            continue
        report.ok.append(s.name)
        raw.extend(evs)
    report.fetched = len(raw)
    if set(enabled) <= {"bcn_opendata"}:
        log.info("only the BCN cultural agenda is active (no Ticketmaster key, feeds or Gmail "
                 "configured); see docs/SETUP.md to add more sources")

    # 3. source health warning: only ENABLED structured sources count
    structured = [n for n in enabled if n in STRUCTURED_SOURCES]
    if structured and not set(structured) & set(report.ok):
        last = parse_iso(state.last_source_warning_at)
        if last is None or now - last > timedelta(hours=24):
            if deliver(WARNING_TEXT.format(names=", ".join(structured)), False):
                state.last_source_warning_at = iso_utc(now)

    # 4-6. enrich, filter, dedupe, tier
    enrich_coords(raw, cfg)
    events = dedupe(filter_events(raw, cfg, now), cfg.source_priority, cfg.artists, cfg.venues)
    tiered: list[Event] = []
    for ev in events:
        ev.tier = assign_tier(ev, cfg)
        if ev.tier:
            tiered.append(ev)
    report.must = sum(e.tier == TIER_MUST for e in tiered)
    report.maybe = sum(e.tier == TIER_MAYBE for e in tiered)
    report.heads = sum(e.tier == TIER_HEADS for e in tiered)

    # 7. diff
    res = diff(state, tiered, now, cfg, set(report.ok))
    report.actions = list(res.actions)
    failed_uids: set[str] = set()

    def fail(*uids: str) -> None:
        for u in uids:
            failed_uids.add(u)
            res.revert(state, u)

    uid_of = lambda a: a.event.uid  # noqa: E731
    by_start = lambda a: (a.event.start or _far(), a.event.uid)  # noqa: E731
    new_actions = [a for a in res.actions if a.kind == NEW]
    others = sorted((a for a in res.actions if a.kind != NEW), key=by_start)

    # 8. notify
    summary_failed = False
    if report.first_run and new_actions:
        evs = sorted((a.event for a in new_actions), key=lambda e: (e.start or _far(), e.uid))
        if not deliver(format_first_run(evs), False):
            summary_failed = True
            fail(*[uid_of(a) for a in new_actions])
        to_send = others
        overflow: list[Action] = []
    else:
        immediate = sorted([a for a in res.actions if a.kind in (NEW, CHANGE)], key=by_start)
        to_send = immediate[:cfg.max_immediate] + [a for a in others if a.kind not in (NEW, CHANGE)]
        overflow = immediate[cfg.max_immediate:]
    for a in to_send:
        text = format_event(a.event, a.kind, distance_km(a.event, cfg), change=a.change,
                            short_notice=a.short_notice)
        if not deliver(text, True):
            fail(uid_of(a))
    if overflow:  # beyond the cap: one compact list, still sent right away
        if not deliver(format_overflow([a.event for a in overflow], cfg.max_immediate), False):
            fail(*[uid_of(a) for a in overflow])

    if report.first_run:
        # stays a "first run" until the summary has been delivered and at least one source answered
        state.first_run_done = not summary_failed and bool(report.ok)
    if (report.first_run and not summary_failed) or (not report.first_run and digest_due(state, now, cfg)):
        content = collect_digest(state)
        if content.is_empty():
            state.last_digest_at = iso_utc(now)
        elif deliver(format_digest(content), False):
            mark_digested(state, content)
            state.last_digest_at = iso_utc(now)

    # 9-10. state first (so alerts are never re-sent), then mail flags, then outputs
    if not dry_run:
        state.save(state_path)
        sid_uid = {i.split(":", 1)[1]: e.uid for e in tiered for i in e.source_ids
                   if i.startswith("inbox:")}
        for src in srcs:
            if hasattr(src, "finalize") and hasattr(src, "pending_ids"):
                done = {sid for sid in src.pending_ids() if sid_uid.get(sid) not in failed_uids}
                if src.finalize(done):
                    state.save(state_path)  # inbox_seen changed
        try:
            today = now.astimezone(MADRID).date()
            shown = []
            for uid, rec in state.events.items():
                ev = rec_to_event(uid, rec)
                if (ev.start and ev.tier in (TIER_MUST, TIER_MAYBE) and not rec["possibly_removed"]
                        and ev.start.astimezone(MADRID).date() >= today):
                    shown.append(ev)
            report.written = write_outputs(out_dir, shown)
        except Exception as e:  # noqa: BLE001 - outputs are secondary to state and alerts
            log.error("rendering outputs failed: %s", e)
            report.render_failed = True
    else:
        for src in srcs:
            if hasattr(src, "close"):
                src.close()

    # 11. summary
    report.summary = (f"sources ok=[{','.join(report.ok)}] failed=[{','.join(report.failed)}] "
                      f"fetched={report.fetched} matched must={report.must} maybe={report.maybe} "
                      f"heads={report.heads} sent={report.sent}")
    print(report.summary)
    code = 1 if (report.send_failures or report.render_failed) else 0
    return code, report


# ---------- other commands ----------

def cmd_list(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    for tier in ("must-see", "maybe"):
        names = [a.name for a in cfg.artists if a.tier == tier]
        print(f"Artists ({tier}): {len(names)}")
        for n in names:
            print(f"  {n}")
    print(f"Venues: {len(cfg.venues)}")
    for v in cfg.venues:
        print(f"  {v.name}")
    print(f"Promoters: {len(cfg.promoters)}")
    for p in cfg.promoters:
        print(f"  {p.name}")
    print(f"Genres: {', '.join(cfg.genres) or '(none)'}")
    return 0


def cmd_add_artist(args: argparse.Namespace) -> int:
    if config_mod.add_artist(args.config, args.name, args.tier):
        print(f"Added {args.name} ({args.tier})")
    else:
        print(f"{args.name} is already in artists.yaml")
    return 0


def cmd_test_telegram(args: argparse.Namespace) -> int:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first", file=sys.stderr)
        return 2
    ok = TelegramClient(tok, chat).send("gigwatch OK", False)
    print("sent" if ok else "FAILED (see log)")
    return 0 if ok else 1


def cmd_inspect_csv(args: argparse.Namespace) -> int:
    from gigwatch.sources import bcn_opendata as bcn
    if args.file:
        raw = Path(args.file).read_bytes()
        print(f"file: {args.file}")
    else:
        url = bcn.resolve_csv_url()
        print(f"resolved url: {url}")
        raw = request_bytes(url)
    text = bcn.decode_bytes(raw)
    headers, reader = bcn._read_rows(text)
    res = bcn.parse_csv_ex(text)
    print(f"headers ({len(headers)}): {headers}")
    print(f"mode: {res.mode}")
    print(f"mapping: {res.mapping}")
    print(f"events={len(res.events)} skipped={res.skipped} filtered={res.filtered}")
    for i, row in enumerate(reader):
        if i >= 3:
            break
        print(f"sample {i + 1}: { {k: (v or '')[:60] for k, v in row.items() if v} }")
    return 0


def request_bytes(url: str) -> bytes:
    from gigwatch.sources.base import request_with_retry
    return request_with_retry(requests.Session(), url, timeout=120).content


def cmd_import_spotify(args: argparse.Namespace) -> int:
    from gigwatch.spotify_import import import_followed
    try:
        added = import_followed(args.config, args.tier)
    except Exception as e:  # noqa: BLE001
        print(f"Spotify import failed: {e}", file=sys.stderr)
        return 1
    print(f"Added {added} artists")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    code, _ = run(args.config, args.state, args.out, dry_run=args.dry_run,
                  since_days=args.since_days)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gigwatch", description="Personal gig alerts for Barcelona")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cfg_arg(sp):
        sp.add_argument("--config", default="config", help="config directory (default: config)")

    sp = sub.add_parser("run", help="fetch, diff and notify")
    cfg_arg(sp)
    sp.add_argument("--dry-run", action="store_true", help="print messages; write no state or files")
    sp.add_argument("--state", default="state/state.json")
    sp.add_argument("--out", default="docs")
    sp.add_argument("--since-days", type=int, default=None,
                    help="limit the look-ahead window to N days (overrides horizon_days)")
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("test-telegram", help="send 'gigwatch OK'")
    sp.set_defaults(func=cmd_test_telegram)
    sp = sub.add_parser("inspect-csv", help="show how the BCN CSV is parsed")
    sp.add_argument("--file", help="inspect a local file instead of downloading")
    sp.set_defaults(func=cmd_inspect_csv)
    sp = sub.add_parser("import-spotify", help="merge followed Spotify artists into artists.yaml")
    cfg_arg(sp)
    sp.add_argument("--tier", default="maybe", choices=["maybe", "must-see"])
    sp.set_defaults(func=cmd_import_spotify)
    sp = sub.add_parser("add-artist", help="add an artist to artists.yaml")
    cfg_arg(sp)
    sp.add_argument("name")
    sp.add_argument("--tier", default="must-see", choices=["maybe", "must-see"])
    sp.set_defaults(func=cmd_add_artist)
    sp = sub.add_parser("list", help="show configured artists, venues and promoters")
    cfg_arg(sp)
    sp.set_defaults(func=cmd_list)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
