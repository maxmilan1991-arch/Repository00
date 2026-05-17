"""Tests for the fetch-full-description path and --refetch-descriptions.

The other fetcher behaviour (retries / rate-limit / stealth) lives
behind Playwright and is exercised manually; here we focus on the
orchestration layer that decides *when* to fetch what.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from immobiliare_export.__main__ import (
    _fetch_details_for_page,
    _refetch_all_descriptions,
    _scrape_search,
)
from immobiliare_export import state as state_mod
from immobiliare_export.config import AppConfig, SearchConfig
from immobiliare_export.database import Database
from immobiliare_export.fetcher import FetchResult
from immobiliare_export.models import Listing
from immobiliare_export.parser import parse_results_page


# ---------------------------------------------------------- HTML helpers

def _index_html(listing_ids: list[int]) -> str:
    payload = {
        "props": {"pageProps": {"dehydratedState": {"queries": [{
            "state": {"data": {
                "totalAds": len(listing_ids),
                "currentPage": 1,
                "results": [
                    {"realEstate": {
                        "id": lid,
                        "title": f"Annuncio {lid}",
                        "price": {"value": 100000 + lid},
                        "properties": [{
                            "typology": {"name": "Casa"},
                            "surface": "100 m²",
                            "rooms": 3,
                            "bathrooms": 1,
                            "ga4Condition": "Buono",
                            "description": f"snippet breve #{lid}",
                            "location": {
                                "city": "Roma", "province": "RM",
                                "region": "Lazio", "address": "Via X",
                                "latitude": 41.9, "longitude": 12.5,
                            },
                        }],
                    }} for lid in listing_ids
                ],
            }},
        }]}}}
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def _detail_html(listing_id: int, description: str) -> str:
    """Mimic the shape of immobiliare.it's __NEXT_DATA__ on a detail page.

    The path we exercise is
    ``props.pageProps.dehydratedState.queries[0].state.data.detail
    .realEstate.properties[0].description``.
    """
    payload = {
        "props": {"pageProps": {"dehydratedState": {"queries": [{
            "state": {"data": {"detail": {"realEstate": {
                "id": listing_id,
                "properties": [{
                    "description": description,
                }],
            }}}},
        }]}}}
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


# ------------------------------------------------------------ fakes

@dataclass
class CountingFetcher:
    """Resolves the URL to a canned HTML body and counts the calls.

    Per-URL responses live in ``responses``. URLs not present default
    to an empty page; if you want detail-page fetches to fail, raise
    inside ``handle_unknown``.
    """
    responses: dict[str, str]
    calls: list[str] = field(default_factory=list)
    sleeps: int = 0

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if url not in self.responses:
            # Default: behave as a 404-ish blank page (no __NEXT_DATA__).
            return FetchResult(url=url, html="", status=404, attempts=1)
        return FetchResult(
            url=url, html=self.responses[url], status=200, attempts=1,
        )

    def sleep_between_pages(self) -> None:
        self.sleeps += 1


def _mk_cfg(*, fetch_full_description: bool) -> AppConfig:
    return AppConfig(
        max_pages_per_search=2,
        consecutive_known_to_stop=25,
        runs_missed_before_stale=3,
        delay_between_pages_sec=0,
        fetch_full_description=fetch_full_description,
        searches=[],
    )


def _run_scrape(*, fetcher, db, cfg, tmp_path):
    cp = state_mod.Checkpoint(config_signature="x", started_at="now")
    return _scrape_search(
        fetcher=fetcher,
        db=db,
        search=SearchConfig(nome="A", url="https://example.test/"),
        cfg=cfg,
        checkpoint=cp,
        checkpoint_path=Path(tmp_path) / "cp.json",
        error_log=[],
        dry_run=False,
        is_first_run=True,
    )


# --------------------------------------------------------- tests


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "fetcher-test.db")
    yield d
    d.close()


def test_no_detail_fetch_when_flag_off(db: Database, tmp_path: Path):
    """fetch_full_description: false → only the index page is requested.

    The two GETs are: page 1 (3 listings) and page 2 (empty terminator).
    Zero detail-page requests should fire.
    """
    listings = [1, 2, 3]
    responses = {
        # Any URL with no `pag=` parameter or pag=1 hits the index.
        f"https://example.test/?criterio=dataannuncio&ordine=desc":
            _index_html(listings),
    }
    # The orchestrator uses SearchConfig.render_url which always sets
    # criterio=dataannuncio&ordine=desc and `pag` only from page 2 on,
    # so encode both variants we expect to see.
    responses["https://example.test/?criterio=dataannuncio&ordine=desc&pag=2"] = (
        _index_html([])
    )
    fetcher = CountingFetcher(responses=responses)
    cfg = _mk_cfg(fetch_full_description=False)
    n_new, *_ = _run_scrape(fetcher=fetcher, db=db, cfg=cfg, tmp_path=tmp_path)
    assert n_new == 3
    # No detail-page URLs were requested.
    detail_calls = [u for u in fetcher.calls if "/annunci/" in u]
    assert detail_calls == []
    # Only one index page is fetched — totalAds=3 < page size, so the
    # loop's "max_pages_by_total" cap fires after the first page.
    assert len(fetcher.calls) == 1


def test_detail_fetch_when_flag_on(db: Database, tmp_path: Path):
    """fetch_full_description: true → one extra GET per listing on the page.

    Total GETs = index pages (2: with data + empty terminator) + 3 details.
    """
    listings = [10, 11, 12]
    responses = {
        "https://example.test/?criterio=dataannuncio&ordine=desc":
            _index_html(listings),
        "https://example.test/?criterio=dataannuncio&ordine=desc&pag=2":
            _index_html([]),
    }
    for lid in listings:
        responses[f"https://www.immobiliare.it/annunci/{lid}/"] = _detail_html(
            lid, f"Casa principale di 200 mq, dependance di 50 mq. Listing {lid}.",
        )
    fetcher = CountingFetcher(responses=responses)
    cfg = _mk_cfg(fetch_full_description=True)
    n_new, *_ = _run_scrape(fetcher=fetcher, db=db, cfg=cfg, tmp_path=tmp_path)
    assert n_new == 3

    detail_calls = [u for u in fetcher.calls if "/annunci/" in u]
    assert sorted(detail_calls) == sorted(
        f"https://www.immobiliare.it/annunci/{lid}/" for lid in listings
    )
    # Total = 1 index + 3 detail = 4 (totalAds=3 caps the index loop).
    assert len(fetcher.calls) == 4

    # The DB now has the full description and a non-null edificato_mq
    # (parser found "200 mq casa + 50 mq dependance" → 250).
    row = db.get_listing(10)
    assert row["description_full"] is not None
    assert "200 mq" in row["description_full"]
    assert row["edificato_mq"] == 250


def test_detail_fetch_failure_is_skipped_not_fatal(db: Database, tmp_path: Path):
    """A single failing detail page must not abort the whole scrape."""
    listings = [21, 22, 23]
    responses = {
        "https://example.test/?criterio=dataannuncio&ordine=desc":
            _index_html(listings),
        "https://example.test/?criterio=dataannuncio&ordine=desc&pag=2":
            _index_html([]),
        # Only 21 and 23 have detail pages; 22 will hit the 404 default.
        "https://www.immobiliare.it/annunci/21/":
            _detail_html(21, "Casa di 120 mq."),
        "https://www.immobiliare.it/annunci/23/":
            _detail_html(23, "Casa di 80 mq."),
    }
    fetcher = CountingFetcher(responses=responses)
    cfg = _mk_cfg(fetch_full_description=True)
    n_new, *_ = _run_scrape(fetcher=fetcher, db=db, cfg=cfg, tmp_path=tmp_path)
    assert n_new == 3  # all upserted even though one detail failed

    # 21 and 23 got the full description; 22 fell back to the snippet.
    assert db.get_listing(21)["description_full"] is not None
    assert db.get_listing(22)["description_full"] is None
    assert db.get_listing(23)["description_full"] is not None


def test_refetch_descriptions_visits_every_listing(db: Database):
    """Pre-populate the DB with 4 listings, run --refetch-descriptions
    through ``_refetch_all_descriptions``, and verify every detail page
    was hit exactly once and every record was updated.
    """
    # Seed: 4 listings, with the index-page snippet only.
    for lid in (101, 102, 103, 104):
        listing = Listing(
            id=lid, titolo=f"L{lid}", descrizione=f"snippet {lid}",
            raw_json="{}",
        )
        db.upsert_listing(listing)

    responses = {}
    for lid in (101, 102, 103, 104):
        responses[f"https://www.immobiliare.it/annunci/{lid}/"] = _detail_html(
            lid, f"Casa di {100 + lid} mq con annesso di 30 mq.",
        )
    fetcher = CountingFetcher(responses=responses)
    n_updated, n_with_built = _refetch_all_descriptions(fetcher, db)

    assert n_updated == 4
    assert n_with_built == 4
    assert sorted(fetcher.calls) == sorted(
        f"https://www.immobiliare.it/annunci/{lid}/" for lid in (101, 102, 103, 104)
    )
    # Every row now carries the longer description and a numeric edificato.
    for lid in (101, 102, 103, 104):
        row = db.get_listing(lid)
        assert row["description_full"] is not None
        assert "Casa di" in row["description_full"]
        assert row["edificato_mq"] is not None
    # sleep_between_pages was called once per fetch — proves the rate
    # limit is honoured between detail requests too.
    assert fetcher.sleeps == 4
