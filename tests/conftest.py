from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gigwatch.config import Artist, Config, Promoter, Venue
from gigwatch.models import MADRID, make_event

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)  # a Tuesday


def mk(**kw):
    """Event factory with a typical Ticketmaster-ish default."""
    base = dict(
        source="ticketmaster", title="Peggy Gou", fetched_at=NOW, artists=["Peggy Gou"],
        venue="Razzmatazz", city="Barcelona", start=datetime(2026, 11, 21, 23, 0, tzinfo=MADRID),
        url="https://t.example/1", status="onsale", source_ids={"tm:1"},
        sources_urls={"Ticketmaster": "https://t.example/1"})
    base.update(kw)
    return make_event(**base)


@pytest.fixture
def cfg() -> Config:
    return Config(
        artists=[
            Artist("Peggy Gou"),
            Artist("Dixon", exclude=["dixon brothers"]),
            Artist("Âme", aliases=["Ame"]),
            Artist("Amelie Lens"),
            Artist("Fontaines D.C.", aliases=["Fontaines DC"]),
            Artist("Marcel Dettmann"),
            Artist("Floating Points"),
            Artist("Khruangbin", tier="maybe"),
        ],
        venues=[
            Venue("Razzmatazz", aliases=["Razz", "The Loft", "Sala Razzmatazz"], lat=41.3979, lon=2.1917),
            Venue("Nitsa", aliases=["Sala Apolo", "Apolo", "La [2] de Apolo"], lat=41.3745, lon=2.1695),
        ],
        promoters=[Promoter("Primavera Sound"), Promoter("Sónar", aliases=["Sonar"])],
        genres=["Techno"],
    )
