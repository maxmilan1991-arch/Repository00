"""Tests for parser.py and the model helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from immobiliare_export.models import (
    is_auction,
    is_private_negotiation,
    listing_url,
    parse_surface,
)
from immobiliare_export.parser import ParserError, parse_results_page


def test_parser_extracts_25_listings_from_fixture(fixture_dir: Path):
    html = (fixture_dir / "sample_page.html").read_text(encoding="utf-8")
    page = parse_results_page(html, ricerca_nome="test")
    assert len(page.listings) == 25
    assert page.total_ads == 12345
    assert page.current_page == 1
    # First listing has the canonical Milan-shaped data.
    first = page.listings[0]
    assert first.id == 1
    assert first.titolo.startswith("Annuncio")
    assert first.comune == "Milano"
    assert first.provincia == "MI"
    assert first.regione == "Lombardia"
    assert first.lat == pytest.approx(45.461, rel=1e-3)


def test_parser_marks_private_negotiation(fixture_dir: Path):
    html = (fixture_dir / "sample_page.html").read_text(encoding="utf-8")
    page = parse_results_page(html)
    # Listing #7 in our fixture has price=1 (placeholder).
    listing = next(l for l in page.listings if l.id == 7)
    assert listing.e_trattativa is True
    assert listing.prezzo_corrente == 1


def test_parser_marks_auction_listings(fixture_dir: Path):
    html = (fixture_dir / "sample_page.html").read_text(encoding="utf-8")
    page = parse_results_page(html)
    # Listing #5 has "all'asta" in the title.
    listing = next(l for l in page.listings if l.id == 5)
    assert listing.e_asta is True


def test_parser_handles_missing_surface(fixture_dir: Path):
    html = (fixture_dir / "sample_page.html").read_text(encoding="utf-8")
    page = parse_results_page(html)
    listing = next(l for l in page.listings if l.id == 11)
    assert listing.superficie_raw == "n.d."
    assert listing.superficie_mq is None


def test_parser_uses_search_name(make_page_html):
    html = make_page_html([{"listing_id": 1}])
    page = parse_results_page(html, ricerca_nome="Milano centro")
    assert page.listings[0].ricerca_nome == "Milano centro"


def test_parser_raises_when_next_data_missing():
    with pytest.raises(ParserError):
        parse_results_page("<html><body>no script here</body></html>")


def test_parser_raises_on_invalid_json():
    bad = '<script id="__NEXT_DATA__" type="application/json">{not-json}</script>'
    with pytest.raises(ParserError):
        parse_results_page(bad)


def test_parser_skips_malformed_entries(make_realestate):
    """Entries without an id should be silently skipped."""
    import json

    good = make_realestate(listing_id=1)
    bad = {"id": "not-an-int", "title": "broken"}
    next_data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "totalAds": 2,
                                    "currentPage": 1,
                                    "results": [
                                        {"realEstate": good},
                                        {"realEstate": bad},
                                    ],
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script>"
    )
    page = parse_results_page(html)
    assert len(page.listings) == 1
    assert page.listings[0].id == 1


# ----- helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("557 m²", 557),
        ("1.234 m²", 1234),
        ("80 m²", 80),
        ("n.d.", None),
        (None, None),
        ("", None),
        ("circa 100 m²", 100),
    ],
)
def test_parse_surface(raw, expected):
    assert parse_surface(raw) == expected


@pytest.mark.parametrize(
    "price,expected",
    [
        (1, True),
        (100, True),
        (1000, True),
        (1111, True),
        (None, True),
        (250000, False),
        (399000, False),
    ],
)
def test_is_private_negotiation(price, expected):
    assert is_private_negotiation(price) is expected


@pytest.mark.parametrize(
    "title,desc,expected",
    [
        ("Villa all'asta", "", True),
        ("Splendido attico", "Vendita giudiziaria al miglior offerente", True),
        ("Splendido attico", "Casa nuova", False),
        ("ASTA giudiziaria", "", True),
        ("", "", False),
    ],
)
def test_is_auction(title, desc, expected):
    assert is_auction(title, desc) is expected


def test_listing_url():
    assert listing_url(123) == "https://www.immobiliare.it/annunci/123/"
