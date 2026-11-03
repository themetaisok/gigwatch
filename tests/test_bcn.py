import json
from datetime import datetime

import responses

from gigwatch.sources import bcn_opendata as bcn
from gigwatch.sources.bcn_opendata import (CKAN_URL, FALLBACK_URL, BcnOpenDataSource, decode_bytes,
                                           map_columns, parse_csv, parse_csv_ex, parse_dt, resolve_csv_url)
from gigwatch.models import MADRID
from tests.conftest import FIX, NOW


def fixture_text():
    return decode_bytes((FIX / "agenda_cultural_ca.csv").read_bytes())


def test_fixture_is_utf16_with_bom():
    raw = (FIX / "agenda_cultural_ca.csv").read_bytes()
    assert raw[:2] == b"\xff\xfe"
    assert "Música" in decode_bytes(raw)


def test_decode_fallbacks():
    assert decode_bytes("ñandú".encode("utf-8-sig")) == "ñandú"
    assert decode_bytes("ñandú".encode("latin-1")) == "ñandú"


def test_dedicated_format_rows(cfg):
    res = parse_csv_ex(fixture_text(), NOW, cfg)
    assert res.mode == "dedicated"
    titles = {e.title: e for e in res.events}
    assert set(titles) == {"Concert: Peggy Gou", "Festival de Música Primavera Sound 2027", "Live Session a la Sala"}
    assert res.skipped == 1  # "TBD" date
    assert res.filtered == 3  # past row, workshop, 1996 recurring row
    assert len(parse_csv_ex(fixture_text(), NOW).events) == 4  # without a config the workshop stays

    peggy = titles["Concert: Peggy Gou"]
    assert peggy.venue == "Carrer de Pamplona 88" and peggy.city == "Barcelona"
    assert (peggy.lat, peggy.lon) == (41.39778, 2.19167)
    assert peggy.start == datetime(2026, 11, 21, 23, 0, tzinfo=MADRID) and not peggy.time_unknown

    fest = titles["Festival de Música Primavera Sound 2027"]
    assert fest.time_unknown and fest.start == datetime(2027, 3, 5, tzinfo=MADRID)
    assert fest.end == datetime(2027, 3, 7, tzinfo=MADRID)
    assert fest.url == "https://www.primaverasound.com/tickets"

    live = titles["Live Session a la Sala"]
    assert live.start.hour == 21 and live.url == "https://example.org/info"
    assert all(e.source_ids and next(iter(e.source_ids)).startswith("bcn:") for e in res.events)


def test_iso_dates_ignore_dayfirst():
    assert parse_dt("2026-05-11")[0].month == 5  # dateutil dayfirst would give Nov 5
    assert parse_dt("11/05/2026")[0].month == 5 and parse_dt("11/05/2026")[0].day == 11
    assert parse_dt("2026-07-31T10:29:13+02:00")[0].day == 31
    assert parse_dt("garbage") is None and parse_dt("") is None


def test_stable_source_ids():
    a = [sorted(e.source_ids) for e in parse_csv(fixture_text(), NOW)]
    b = [sorted(e.source_ids) for e in parse_csv(fixture_text(), NOW)]
    assert a == b


def test_heuristic_english_headers():
    text = (FIX / "agenda_en.csv").read_text(encoding="utf-8")
    m = map_columns(text.splitlines()[0].split(","))
    assert m["title"] == "title" and m["start"] == "start_date" and m["end"] == "end_date"
    assert m["venue"] == "venue" and m["lat"] == "latitude" and m["lon"] == "longitude"
    assert m["category"] == "category" and m["description"] == "description" and "address" not in m.values()
    res = parse_csv_ex(text, NOW)
    assert res.mode == "heuristic"
    assert [e.title for e in res.events] == ["Peggy Gou", "Nitsa Night"]
    assert res.events[1].start == datetime(2026, 12, 15, 21, 0, tzinfo=MADRID)  # dd/mm/yyyy
    assert res.events[0].start == datetime(2026, 11, 21, 23, 0, tzinfo=MADRID)
    assert res.skipped == 1 and res.filtered == 1  # unparseable date / exhibition


def test_heuristic_catalan_legacy_headers_and_dayfirst():
    text = ("nom;descripcio;data_inici;data_fi;equipament;adreca;latitud;longitud;url;categoria\n"
            "Nitsa Night;Techno;15/12/2026 21:00;;Sala Apolo;Nou de la Rambla 113;41.37;2.17;https://e.org/n;Música i clubbing\n"
            "Exposició;Art;01/12/2026;;MACBA;Plaça dels Àngels;41.38;2.17;https://e.org/x;Exposicions\n"
            "Concert curt;x;;;;;;;;Música\n")
    res = parse_csv_ex(text, NOW)  # semicolon delimiter is sniffed
    m = res.mapping
    assert m["title"] == "nom" and m["start"] == "data_inici" and m["end"] == "data_fi"
    assert m["venue"] == "equipament" and m["category"] == "categoria" and m["description"] == "descripcio"
    assert [e.title for e in res.events] == ["Nitsa Night"]
    assert res.events[0].start == datetime(2026, 12, 15, 21, 0, tzinfo=MADRID)
    assert res.skipped == 1 and res.filtered == 1


def test_no_category_column_keeps_all_rows():
    res = parse_csv_ex("name,date\nA,2026-12-01\nB,2026-12-02\n", NOW)
    assert len(res.events) == 2


def test_weird_files_do_not_crash():
    for junk in ["", "\x00\x01\x02", "a,b,c\n1,2", "﻿", 'name,date\n"unterminated,2026-01-01\n',
                 "register_id,name,start_date,end_date\n1,Concert,,\n"]:
        res = parse_csv_ex(junk, NOW)
        assert isinstance(res.events, list)
    assert parse_csv_ex("register_id,name,start_date,end_date\n1,Concert,,\n", NOW).skipped == 1


@responses.activate
def test_resolve_csv_url_from_ckan():
    responses.add(responses.GET, CKAN_URL.split("?")[0], json=json.loads((FIX / "ckan_package_show.json").read_text()))
    url = resolve_csv_url(sleep=lambda s: None)
    assert url.endswith("3abb2414-1ee0-446e-9c25-380e938adb73/download") and "xml" not in url


@responses.activate
def test_resolve_csv_url_falls_back_on_failure():
    responses.add(responses.GET, CKAN_URL.split("?")[0], status=503)
    assert resolve_csv_url(sleep=lambda s: None) == FALLBACK_URL
    assert FALLBACK_URL.endswith("/download") and "2767159c" in FALLBACK_URL


@responses.activate
def test_source_fetch_end_to_end(cfg):
    ckan = json.loads((FIX / "ckan_package_show.json").read_text())
    responses.add(responses.GET, CKAN_URL.split("?")[0], json=ckan)
    responses.add(responses.GET, ckan["result"]["resources"][1]["url"], body=(FIX / "agenda_cultural_ca.csv").read_bytes())
    evs = BcnOpenDataSource(sleep=lambda s: None).fetch(cfg, NOW)
    assert len(evs) == 3 and all(e.source == "bcn_opendata" for e in evs)


def test_followed_artist_row_kept_even_without_music_word(cfg):
    hdr = fixture_text().split("\n", 1)[0].strip()
    cols = hdr.split(",")
    row = dict.fromkeys(cols, "")
    row.update(register_id="9", name="Marcel Dettmann", addresses_town="Barcelona",
               start_date="2026-12-01", end_date="2026-12-01")
    other = dict(row, register_id="10", name="Taller de ceràmica")
    text = hdr + "\n" + "\n".join(",".join(r[c] for c in cols) for r in (row, other)) + "\n"
    assert [e.title for e in parse_csv(text, NOW, cfg)] == ["Marcel Dettmann"]


def test_time_regex_needs_colon_or_h():
    from gigwatch.sources.bcn_opendata import _TIME_RE
    assert _TIME_RE.search("Del 12.11.2026") is None
    assert _TIME_RE.search("a les 21h").group(1) == "21"
    assert _TIME_RE.search("23:30h").group(2) == "30"
