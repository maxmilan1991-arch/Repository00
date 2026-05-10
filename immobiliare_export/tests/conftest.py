"""Shared test fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the package importable when running pytest from the project root
# without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_realestate(
    *,
    listing_id: int,
    title: str = "Splendido attico",
    price: int | None = 250000,
    typology: str = "Appartamento",
    surface: str | None = "120 m²",
    rooms: int | None = 4,
    bathrooms: int | None = 2,
    condition: str = "Buono / Abitabile",
    description: str = "Descrizione di esempio.",
    province: str = "MI",
    city: str = "Milano",
    address: str = "Via Roma 1",
    region: str = "Lombardia",
    lat: float = 45.46,
    lng: float = 9.19,
) -> dict[str, Any]:
    return {
        "id": listing_id,
        "title": title,
        "price": {"value": price} if price is not None else {"value": None},
        "properties": [
            {
                "typology": {"name": typology},
                "surface": surface,
                "rooms": rooms,
                "bathrooms": bathrooms,
                "ga4Condition": condition,
                "description": description,
                "location": {
                    "city": city,
                    "province": province,
                    "region": region,
                    "address": address,
                    "latitude": lat,
                    "longitude": lng,
                },
            }
        ],
    }


def _build_next_data(
    realestates: list[dict[str, Any]],
    *,
    total_ads: int | None = None,
    current_page: int = 1,
) -> dict[str, Any]:
    if total_ads is None:
        total_ads = len(realestates)
    return {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "totalAds": total_ads,
                                    "currentPage": current_page,
                                    "results": [
                                        {"realEstate": re} for re in realestates
                                    ],
                                }
                            }
                        }
                    ]
                }
            }
        }
    }


def _wrap_html(next_data: dict[str, Any]) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )


@pytest.fixture
def make_realestate():
    return _build_realestate


@pytest.fixture
def make_page_html():
    """Factory: list of dicts overriding ``_build_realestate`` -> HTML string."""

    def _factory(
        overrides: list[dict[str, Any]] | None = None,
        *,
        total_ads: int | None = None,
        current_page: int = 1,
    ) -> str:
        overrides = overrides or [{"listing_id": i} for i in range(1, 26)]
        realestates = []
        for i, ov in enumerate(overrides, start=1):
            payload = {"listing_id": i, **ov}
            realestates.append(_build_realestate(**payload))
        return _wrap_html(
            _build_next_data(
                realestates, total_ads=total_ads, current_page=current_page
            )
        )

    return _factory


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"
