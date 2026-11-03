import logging
import shutil
from pathlib import Path

import pytest
import yaml

from gigwatch import config as cfgmod
from gigwatch.cli import main
from gigwatch.config import ConfigError, load, parse_config
from gigwatch.spotify_import import build_auth_url, make_pkce, merge_artists
from tests.conftest import FIX

REPO = Path(__file__).resolve().parent.parent


def test_load_fixture_config():
    c = load(FIX / "config")
    assert [a.name for a in c.artists] == ["Peggy Gou", "Dixon", "Floating Points"]
    assert c.artists[1].exclude == ["dixon brothers"]
    assert [v.name for v in c.venues] == ["Razzmatazz", "Nitsa"] and len(c.feeds) == 2
    assert c.genres == [] and c.digest_weekday == 1 and "Barcelona" in c.region_keywords


def test_load_seed_config():
    c = load(REPO / "config")
    assert len(c.artists) == 38 and c.feeds == []
    dixon = next(a for a in c.artists if a.name == "Dixon")
    assert dixon.exclude == ["dixon brothers"]
    nitsa = next(v for v in c.venues if v.name == "Nitsa")
    assert "La [2] de Apolo" in nitsa.aliases and "Nitsa Club" in nitsa.aliases
    razz = next(v for v in c.venues if v.name == "Razzmatazz")
    assert {"Razz", "The Loft", "Sala Razzmatazz"} <= set(razz.aliases)
    assert len(c.promoters) == 11


def test_missing_settings_is_error_other_files_optional(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path)
    (tmp_path / "settings.yaml").write_text("home: {lat: 1, lon: 2, radius_km: 10}\n")
    c = load(tmp_path)
    assert c.artists == [] and c.radius_km == 10


def test_unknown_keys_warn_and_extra_keywords_merge(caplog):
    with caplog.at_level(logging.WARNING, logger="gigwatch"):
        c = parse_config({"home": {"lat": 1, "lon": 2, "radius_km": 5}, "bogus": 1,
                          "extra_region_keywords": ["Mollet"]},
                         [{"name": "X", "typo": 1}])
    assert "bogus" in caplog.text and "typo" in caplog.text
    assert "Mollet" in c.region_keywords and "Barcelona" in c.region_keywords


def test_validation_errors():
    base = {"home": {"lat": 1, "lon": 2, "radius_km": 5}}
    with pytest.raises(ConfigError):
        parse_config({**base, "maybe_digest": {"weekday": 9, "hour_local": 9}})
    with pytest.raises(ConfigError):
        parse_config(base, [{"name": "X", "tier": "bogus"}])
    with pytest.raises(ConfigError):
        parse_config({"home": {"lat": "x", "lon": 2, "radius_km": 5}})


def test_add_artist_roundtrip_keeps_order_and_entries(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "c")
    before = yaml.safe_load((tmp_path / "c/artists.yaml").read_text())
    assert cfgmod.add_artist(tmp_path / "c", "Test Artist", "maybe")
    assert not cfgmod.add_artist(tmp_path / "c", "test  artist")  # duplicate by normalised name
    after = yaml.safe_load((tmp_path / "c/artists.yaml").read_text())
    assert after[:-1] == before and after[-1] == {"name": "Test Artist", "tier": "maybe"}
    assert list(after[1].keys()) == list(before[1].keys())  # key order preserved
    assert load(tmp_path / "c").artists[-1].tier == "maybe"


def test_cli_list_and_add_artist(tmp_path, capsys):
    shutil.copytree(REPO / "config", tmp_path / "c")
    assert main(["list", "--config", str(tmp_path / "c")]) == 0
    assert "Peggy Gou" in capsys.readouterr().out
    assert main(["add-artist", "Test Artist", "--tier", "maybe", "--config", str(tmp_path / "c")]) == 0
    assert "Added Test Artist" in capsys.readouterr().out
    assert main(["list", "--config", str(tmp_path / "nope")]) == 2


def test_spotify_merge_and_pkce():
    existing = [{"name": "Peggy Gou", "tier": "must-see", "aliases": ["PG"]}, {"name": "Âme"}]
    incoming = [{"name": "peggy gou", "spotify_id": "1"}, {"name": "AME", "spotify_id": "2"},
                {"name": "pg", "spotify_id": "3"}, {"name": "New Band", "spotify_id": "4"}]
    merged, added = merge_artists(existing, incoming)
    assert added == 1 and merged[:2] == existing
    assert merged[2] == {"name": "New Band", "tier": "maybe", "spotify_id": "4"}
    v, ch = make_pkce()
    assert len(v) == 64 and len(ch) == 43 and "=" not in ch
    url = build_auth_url("CID", ch, "st")
    assert "code_challenge_method=S256" in url and "user-follow-read" in url
