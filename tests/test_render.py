import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from gigwatch.models import MADRID
from gigwatch.render import fold, ics_escape, render_atom, render_html, render_ics, write_outputs
from tests.conftest import NOW, mk


def evs():
    a = mk(uid="a1", tier="must-see")
    b = mk(uid="b2", tier="maybe", title="Khruangbin, live; <tour>", artists=["Khruangbin, live; <tour>"],
           start=datetime(2026, 11, 20, 21, 0, tzinfo=MADRID), venue='Sala "X"', url=None)
    c = mk(uid="c3", tier="must-see", status="canceled", time_unknown=True, title="Dixon", artists=["Dixon"],
           start=datetime(2026, 12, 5, tzinfo=MADRID))
    return [b, c, a]


def test_ics_escape_and_fold():
    assert ics_escape("a,b;c\\d\ne") == r"a\,b\;c\\d\ne"
    line = "SUMMARY:" + "ñ" * 60  # 2 octets each
    folded = fold(line)
    parts = folded.split("\r\n ")
    assert all(len(p.encode()) <= 75 for p in parts) and "".join(parts) == line
    assert all(len(p.encode()) <= 74 for p in parts[1:])
    assert fold("short") == "short"


def test_ics_structure_and_content():
    ics = render_ics(evs())
    assert ics.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//gigwatch//EN\r\n")
    assert ics.endswith("END:VCALENDAR\r\n") and "\n" not in ics.replace("\r\n", "")
    assert "UID:a1@gigwatch" in ics and "UID:c3@gigwatch" in ics
    assert "DTSTART:20261121T220000Z" in ics            # 23:00 Madrid -> UTC
    assert "DTSTART;VALUE=DATE:20261205" in ics         # unknown time -> all day
    assert "STATUS:CANCELLED" in ics and ics.count("STATUS:CANCELLED") == 1
    assert r"SUMMARY:Khruangbin\, live\; <tour>" in ics
    assert "DTSTAMP:20261103T120000Z" in ics
    for physical in ics.split("\r\n"):
        assert len(physical.encode()) <= 75


def test_ics_is_deterministic_and_order_independent():
    assert render_ics(evs()) == render_ics(list(reversed(evs())))
    assert render_atom(evs()) == render_atom(list(reversed(evs())))
    assert render_html(evs()) == render_html(list(reversed(evs())))


def test_atom_is_well_formed_and_escaped():
    xml = render_atom(evs())
    root = ET.fromstring(xml)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    assert len(entries) == 3
    ids = {e.find("a:id", ns).text for e in entries}
    assert "tag:gigwatch,a1" in ids
    titles = [e.find("a:title", ns).text for e in entries]
    assert any("Khruangbin, live; <tour>" in t for t in titles)


def test_html_well_formed_must_see_first_and_mobile():
    html = render_html(evs())
    root = ET.fromstring(html.split("\n", 1)[1])  # skip doctype; document is XML-clean
    assert "width=device-width" in html and "prefers-color-scheme:dark" in html
    assert html.index("Must-see") < html.index("Maybe") < html.index("Khruangbin")
    assert html.index("Peggy Gou") < html.index("Maybe")
    assert "&lt;tour&gt;" in html and "CANCELED" in html
    assert "Updated 2026-11-03" in html
    assert root.tag == "html"


def test_html_empty_and_skips_undated_and_untiered():
    h = render_html([mk(start=None, tier="heads-up"), mk(tier=None)])
    assert "No upcoming shows yet." in h
    ET.fromstring(h.split("\n", 1)[1])


def test_write_outputs_only_rewrites_changed_files(tmp_path):
    first = write_outputs(tmp_path, evs())
    assert {p.name for p in first} == {"index.html", "gigwatch.ics", "feed.xml"}
    assert write_outputs(tmp_path, evs()) == []
    assert (tmp_path / "gigwatch.ics").read_bytes().count(b"\r\n") > 5
