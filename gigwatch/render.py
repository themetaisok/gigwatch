"""Static outputs: HTML page, ICS calendar, Atom feed. Pure and deterministic."""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from gigwatch.models import MADRID, TIER_MAYBE, TIER_MUST, Event
from gigwatch.notify_telegram import display_name, fmt_date, safe_url

_FAR = datetime.max.replace(tzinfo=timezone.utc)


def _shown(events: Iterable[Event]) -> list[Event]:
    out = [e for e in events if e.start is not None and e.tier in (TIER_MUST, TIER_MAYBE)]
    out.sort(key=lambda e: (e.start, e.uid))
    return out


# ---------- HTML ----------

_CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1c1c1e;--mut:#6b6b70;--card:#fff;--bd:#e3e3e8;--ac:#0a58ca;--must:#c2410c}
@media(prefers-color-scheme:dark){:root{--bg:#111114;--fg:#ececf0;--mut:#9a9aa3;--card:#1b1b20;--bd:#2c2c33;--ac:#6ea8fe;--must:#fb923c}}
*{box-sizing:border-box}
body{margin:0;padding:1rem;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:42rem;margin:0 auto}
h1{font-size:1.4rem;margin:.2rem 0 1rem}
h2{font-size:1.1rem;margin:1.6rem 0 .4rem}
h2.must{color:var(--must)}
h3{font-size:.85rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:1rem 0 .3rem}
ul{list-style:none;margin:0;padding:0}
li{background:var(--card);border:1px solid var(--bd);border-radius:.6rem;padding:.6rem .8rem;margin:.4rem 0}
.n{font-weight:600}
.m{color:var(--mut);font-size:.9rem}
a{color:var(--ac)}
.x{display:inline-block;font-size:.75rem;font-weight:700;border:1px solid currentColor;border-radius:.3rem;padding:0 .3rem;margin-left:.4rem;color:#d12}
footer{margin-top:2rem;color:var(--mut);font-size:.8rem}
"""


def _li(ev: Event) -> str:
    esc = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    time = "" if ev.time_unknown else ev.start.astimezone(MADRID).strftime("%H:%M") + " \u00b7 "
    badge = ""
    if ev.status in ("canceled", "postponed", "rescheduled"):
        badge = f'<span class="x">{esc(ev.status.upper())}</span>'
    place = ", ".join(esc(x) for x in (ev.venue, ev.city) if x)
    url = safe_url(ev.url)
    link = f' \u00b7 <a href="{esc(url)}">Tickets</a>' if url else ""
    return (f'<li><span class="n">{esc(display_name(ev))}</span>{badge}<br />'
            f'<span class="m">{time}{place}{link}</span></li>')


def _section(title: str, cls: str, evs: list[Event]) -> str:
    if not evs:
        return ""
    parts = [f'<h2 class="{cls}">{html.escape(title)}</h2>']
    cur = None
    for ev in evs:
        d = ev.start.astimezone(MADRID).date()
        if d != cur:
            if cur is not None:
                parts.append("</ul>")
            parts.append(f"<h3>{html.escape(fmt_date(ev.start))}</h3><ul>")
            cur = d
        parts.append(_li(ev))
    parts.append("</ul>")
    return "\n".join(parts)


def render_html(events: Sequence[Event]) -> str:
    evs = _shown(events)
    must = [e for e in evs if e.tier == TIER_MUST]
    maybe = [e for e in evs if e.tier == TIER_MAYBE]
    body = _section("Must-see", "must", must) + "\n" + _section("Maybe", "maybe", maybe)
    if not evs:
        body = '<p class="m">No upcoming shows yet.</p>'
    updated = ""
    if evs:
        d = max(e.fetched_at for e in evs).astimezone(MADRID).date().isoformat()
        updated = f"Updated {d}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        '<meta name="color-scheme" content="light dark" />'
        "<title>gigwatch</title>"
        f"<style>{_CSS}</style></head><body><main>\n"
        "<h1>gigwatch</h1>\n"
        f"{body}\n"
        f'<footer>{html.escape(updated)} \u00b7 <a href="gigwatch.ics">Calendar (.ics)</a>'
        ' \u00b7 <a href="feed.xml">Feed</a></footer>\n'
        "</main></body></html>\n"
    )


# ---------- ICS ----------

def ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def fold(line: str) -> str:
    """Fold at 75 octets (never splitting a UTF-8 character); continuation lines start with a space."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    out: list[str] = []
    limit = 75
    i = 0
    while i < len(data):
        j = min(i + limit, len(data))
        while j < len(data) and (data[j] & 0xC0) == 0x80:
            j -= 1
        out.append(data[i:j].decode("utf-8"))
        i = j
        limit = 74
    return "\r\n ".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render_ics(events: Sequence[Event]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//gigwatch//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:gigwatch"]
    for ev in _shown(events):
        lines += ["BEGIN:VEVENT", f"UID:{ev.uid}@gigwatch", f"DTSTAMP:{_utc(ev.fetched_at)}"]
        if ev.time_unknown:
            day = ev.start.astimezone(MADRID).date()
            lines += [f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
        else:
            end = ev.end or ev.start + timedelta(hours=3)
            lines += [f"DTSTART:{_utc(ev.start)}", f"DTEND:{_utc(end)}"]
        lines.append("SUMMARY:" + ics_escape(display_name(ev)))
        place = ", ".join(x for x in (ev.venue, ev.city) if x)
        if place:
            lines.append("LOCATION:" + ics_escape(place))
        url = safe_url(ev.url)
        if url:
            lines.append("URL:" + url)
        if ev.status == "canceled":
            lines.append("STATUS:CANCELLED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)


# ---------- Atom ----------

ATOM_NS = "http://www.w3.org/2005/Atom"


def render_atom(events: Sequence[Event]) -> str:
    evs = sorted(_shown(events), key=lambda e: (e.fetched_at, e.uid), reverse=True)
    feed = ET.Element("feed", {"xmlns": ATOM_NS})
    ET.SubElement(feed, "title").text = "gigwatch"
    ET.SubElement(feed, "id").text = "tag:gigwatch,feed"
    updated = max((e.fetched_at for e in evs), default=datetime(1970, 1, 1, tzinfo=timezone.utc))
    ET.SubElement(feed, "updated").text = updated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for ev in evs:
        e = ET.SubElement(feed, "entry")
        ET.SubElement(e, "title").text = f"{display_name(ev)} - {fmt_date(ev.start)}"
        ET.SubElement(e, "id").text = f"tag:gigwatch,{ev.uid}"
        ET.SubElement(e, "updated").text = ev.fetched_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = safe_url(ev.url)
        if url:
            ET.SubElement(e, "link", {"href": url})
        place = ", ".join(x for x in (ev.venue, ev.city) if x)
        ET.SubElement(e, "summary").text = f"{ev.tier}: {place}" if place else str(ev.tier)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


# ---------- writing ----------

def write_outputs(out_dir: str | Path, events: Sequence[Event]) -> list[Path]:
    """Write index.html, gigwatch.ics, feed.xml; only touches files whose content changed."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    files = {"index.html": render_html(events), "gigwatch.ics": render_ics(events),
             "feed.xml": render_atom(events)}
    written: list[Path] = []
    for name, content in files.items():
        p = d / name
        data = content.encode("utf-8")
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)
            written.append(p)
    return written
