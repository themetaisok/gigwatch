from gigwatch.geo import distance_km, geohash, haversine_km, in_region
from gigwatch.normalize import norm
from tests.conftest import mk


def test_norm_examples():
    assert norm("Fred again..") == "fred again"
    assert norm("Âme") == "ame"
    assert norm("Fontaines D.C.") == "fontaines dc"
    assert norm("Hot  Chip &  Co") == "hot chip and co"
    assert norm("L'Hospitalet") == "lhospitalet"
    assert norm(None) == ""


def test_geohash_known_value():
    assert geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"
    h = geohash(41.3874, 2.1686, 5)
    assert len(h) == 5 and h.startswith("sp3")


def test_haversine():
    d = haversine_km(41.3874, 2.1686, 41.9794, 2.8214)  # Barcelona - Girona
    assert 75 < d < 95
    assert haversine_km(1, 1, 1, 1) == 0


def test_in_region_coordinates_win_over_keywords(cfg):
    far = mk(lat=40.4237, lon=-3.6717, city="Barcelona", venue="Somewhere")  # Madrid coords
    assert not in_region(far, cfg)
    near = mk(lat=41.39, lon=2.19, city="Madrid")
    assert in_region(near, cfg)
    assert distance_km(near, cfg) < 5


def test_in_region_keywords_whole_word(cfg):
    assert in_region(mk(lat=None, lon=None, city="Sitges", venue=None), cfg)
    assert in_region(mk(lat=None, lon=None, city=None, venue="Sala X, L'Hospitalet de Llobregat"), cfg)
    assert not in_region(mk(lat=None, lon=None, city="Madrid", venue="WiZink"), cfg)
    assert not in_region(mk(lat=None, lon=None, city="Preus", venue=None), cfg)  # not whole word "reus"
    assert not in_region(mk(lat=None, lon=None, city=None, venue=None), cfg)
    assert distance_km(mk(lat=None, lon=None), cfg) is None
