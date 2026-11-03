from datetime import datetime

import pytest

from gigwatch.config import Artist
from gigwatch.match import assign_tier, match_artist, venue_match
from gigwatch.models import MADRID
from tests.conftest import mk


def names(ev, cfg):
    return [h.artist.name for h in match_artist(ev, cfg.artists)]


def test_ame_does_not_match_amelie_lens(cfg):
    ev = mk(title="Amelie Lens", artists=["Amelie Lens"])
    assert names(ev, cfg) == ["Amelie Lens"]


def test_short_alias_only_exact(cfg):
    assert names(mk(title="Ame live in concert", artists=[]), cfg) == []
    assert names(mk(title="Night", artists=["Âme"]), cfg) == ["Âme"]
    assert names(mk(title="Night", artists=["AME"]), cfg) == ["Âme"]


def test_dixon_excludes_dixon_brothers(cfg):
    assert names(mk(title="Dixon Brothers Live", artists=["Dixon Brothers"]), cfg) == []
    assert names(mk(title="Dixon Brothers Live", artists=[]), cfg) == []
    assert names(mk(title="Dixon - Night Session", artists=["Dixon"]), cfg) == ["Dixon"]


def test_no_fuzzy_on_subset_without_exclude():
    ev = mk(title="Gig", artists=["Dixon Brothers"])
    assert match_artist(ev, [Artist("Dixon")]) == []  # token_set_ratio says 100; token counts differ


@pytest.mark.parametrize("variant", ["Fontaines D.C.", "Fontaines DC", "FONTAINES D.C.", "fontaines d.c"])
def test_fontaines_variants(cfg, variant):
    assert names(mk(title="x", artists=[variant]), cfg) == ["Fontaines D.C."]
    assert names(mk(title=f"{variant} + support", artists=[]), cfg) == ["Fontaines D.C."]


def test_fuzzy_typo_on_artist_candidates_only(cfg):
    assert names(mk(title="Gig", artists=["Marcel Dettman"]), cfg) == ["Marcel Dettmann"]
    assert names(mk(title="Marcel Dettman live", artists=[]), cfg) == []  # never fuzzy on the title


def test_title_whole_word(cfg):
    assert names(mk(title="Peggy Gou: Boiler Room", artists=[]), cfg) == ["Peggy Gou"]
    assert names(mk(title="Peggy Gouda cheese", artists=[]), cfg) == []


def test_venue_match_variants(cfg):
    assert venue_match("Sala Apolo", cfg.venues).name == "Nitsa"
    assert venue_match("La [2] de Apolo", cfg.venues).name == "Nitsa"
    assert venue_match("Razzmatazz 2 - Sala Razzmatazz", cfg.venues).name == "Razzmatazz"
    assert venue_match("Teatre Coliseum", cfg.venues) is None


def test_tiers(cfg):
    assert assign_tier(mk(), cfg) == "must-see"
    assert assign_tier(mk(title="Khruangbin", artists=["Khruangbin"], venue="Elsewhere"), cfg) == "maybe"
    assert assign_tier(mk(title="Some DJ", artists=["Some DJ"], venue="Sala Apolo"), cfg) == "maybe"
    assert assign_tier(mk(title="Primavera Sound 2027", artists=[], venue="Forum"), cfg) == "maybe"
    assert assign_tier(mk(title="Sonar Day", artists=[], venue="Fira"), cfg) == "maybe"
    assert assign_tier(mk(title="Nobody", artists=["Nobody"], venue="Elsewhere", genres=["Techno"]), cfg) == "maybe"
    assert assign_tier(mk(title="Nobody", artists=["Nobody"], venue="Elsewhere"), cfg) is None


def test_heads_up_hint_and_undated(cfg):
    assert assign_tier(mk(title="Nobody", artists=[], venue=None, raw_tier_hint="heads-up"), cfg) == "heads-up"
    # undated items can only ever be heads-up, even when an artist matches
    ev = mk(start=None, raw_tier_hint="heads-up")
    assert assign_tier(ev, cfg) == "heads-up"
    assert assign_tier(mk(start=None), cfg) is None
    assert assign_tier(mk(start=datetime(2026, 12, 1, tzinfo=MADRID), raw_tier_hint="heads-up", artists=[], title="x", venue=None), cfg) == "heads-up"
