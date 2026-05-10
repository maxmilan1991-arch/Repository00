"""End-to-end-ish tests for the incremental scrape behaviour.

We exercise the orchestration logic (``__main__._scrape_search``) with a
fake fetcher backed by in-memory HTML pages, so we can verify the
new/updated/unchanged counters and the stop conditions without spinning
up Playwright.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from immobiliare_export import state as state_mod
from immobiliare_export.__main__ import _scrape_search
from immobiliare_export.config import AppConfig, SearchConfig
from immobiliare_export.database import Database
from immobiliare_export.fetcher import FetchResult


# -------------------- helpers / fakes ----------------------


def _build_html(listings: list[dict[str, Any]]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "totalAds": 9999,
                                    "currentPage": 1,
                                    "results": [
                                        {"realEstate": l} for l in listings
                                    ],
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def _re(listing_id: int, *, price: int = 200000, title: str = "Casa") -> dict:
    return {
        "id": listing_id,
        "title": title,
        "price": {"value": price},
        "properties": [
            {
                "typology": {"name": "Appartamento"},
                "surface": "100 m²",
                "rooms": 3,
                "bathrooms": 2,
                "ga4Condition": "Buono / Abitabile",
                "description": "...",
                "location": {
                    "city": "Milano",
                    "province": "MI",
                    "region": "Lombardia",
                    "address": "Via X",
                    "latitude": 45.0,
                    "longitude": 9.0,
                },
            }
        ],
    }


@dataclass
class FakeFetcher:
    """A fetcher that serves canned HTML pages keyed by page number."""

    pages_by_url: dict[int, list[dict[str, Any]]]

    def fetch(self, url: str) -> FetchResult:
        # Detect "pag=N" in the URL; default to page 1.
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        page = int(q.get("pag", ["1"])[0])
        listings = self.pages_by_url.get(page, [])
        if not listings:
            # mimic an empty page (server still returns __NEXT_DATA__).
            html = _build_html([])
        else:
            html = _build_html(listings)
        return FetchResult(url=url, html=html, status=200, attempts=1)

    def sleep_between_pages(self) -> None:
        return None


def _scrape(
    *, db, search, cfg, pages, dry_run=False, is_first_run=False, tmp_path
):
    fetcher = FakeFetcher(pages_by_url=pages)
    cp = state_mod.Checkpoint(config_signature="x", started_at="now")
    cp_path = Path(tmp_path) / "cp.json"
    return _scrape_search(
        fetcher=fetcher,
        db=db,
        search=search,
        cfg=cfg,
        checkpoint=cp,
        checkpoint_path=cp_path,
        error_log=[],
        dry_run=dry_run,
        is_first_run=is_first_run,
    )


def _make_cfg(**overrides) -> AppConfig:
    cfg = AppConfig(
        max_pages_per_search=10,
        consecutive_known_to_stop=25,
        runs_missed_before_stale=3,
        delay_between_pages_sec=0,
        searches=[],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "inc.db")
    yield d
    d.close()


# ---------------------- tests ------------------------------


def test_run1_dataset_of_50_is_all_new(db: Database, tmp_path: Path):
    # Two pages of 25 listings each → 50 unique IDs, all unseen.
    pages = {
        1: [_re(i) for i in range(1, 26)],
        2: [_re(i) for i in range(26, 51)],
        3: [],  # empty → terminator
    }
    cfg = _make_cfg()
    search = SearchConfig(nome="A", url="https://example.test/")
    n_new, n_upd, n_unch, seen = _scrape(
        db=db, search=search, cfg=cfg, pages=pages,
        is_first_run=True, tmp_path=tmp_path,
    )
    assert n_new == 50
    assert n_upd == 0
    assert n_unch == 0
    assert len(seen) == 50


def test_run2_identical_dataset_finds_nothing_new(db: Database, tmp_path: Path):
    pages = {
        1: [_re(i) for i in range(1, 26)],
        2: [_re(i) for i in range(26, 51)],
        3: [],
    }
    cfg = _make_cfg()
    search = SearchConfig(nome="A", url="https://example.test/")
    _scrape(db=db, search=search, cfg=cfg, pages=pages,
            is_first_run=True, tmp_path=tmp_path)

    # Second run, same data, same search.
    n_new, n_upd, n_unch, seen = _scrape(
        db=db, search=search, cfg=cfg, pages=pages,
        is_first_run=False, tmp_path=tmp_path,
    )
    assert n_new == 0
    assert n_upd == 0
    assert n_unch >= 25  # at least one full page consumed
    # Stop condition kicked in after the first full page of "all known".
    # We expect the loop to have stopped, so n_unch is 25, not 50.
    assert n_unch == 25


def test_run3_with_changes_and_drops(db: Database, tmp_path: Path):
    """Run 1: 50 listings. Run 3: 30 listings (20 dropped, 10 new), 5 priced changed."""
    initial_pages = {
        1: [_re(i) for i in range(1, 26)],
        2: [_re(i) for i in range(26, 51)],
        3: [],
    }
    cfg = _make_cfg()
    search = SearchConfig(nome="A", url="https://example.test/")
    _scrape(db=db, search=search, cfg=cfg, pages=initial_pages,
            is_first_run=True, tmp_path=tmp_path)

    # Run 3: keep IDs 1..30 (so 20 of the original disappeared); change the
    # price of 5 (1..5) and add 10 brand-new ones (51..60).
    new_listings = [
        _re(i, price=300000) if i <= 5 else _re(i)
        for i in range(1, 31)
    ] + [_re(i) for i in range(51, 61)]

    pages_after = {
        1: new_listings[:25],
        2: new_listings[25:40],
        3: [],
    }
    n_new, n_upd, n_unch, seen = _scrape(
        db=db, search=search, cfg=cfg, pages=pages_after,
        is_first_run=False, tmp_path=tmp_path,
    )
    assert n_new == 10
    assert n_upd == 5

    # Now apply the stale-marking that the orchestrator would normally do.
    n_stale = db.mark_missing_as_stale(
        ricerca_nome="A",
        seen_ids=seen,
        run_started_at=datetime.utcnow(),
        runs_missed_threshold=3,
    )
    assert n_stale == 0  # one missed run is below threshold

    rows = {r["id"]: r for r in db.all_listings()}
    # The 20 dropped ones should have runs_missed=1.
    missing_count = sum(
        1 for i in range(31, 51) if rows[i]["runs_missed"] == 1
    )
    assert missing_count == 20


def test_listings_become_stale_after_threshold(db: Database, tmp_path: Path):
    initial_pages = {1: [_re(i) for i in range(1, 11)], 2: []}
    cfg = _make_cfg(consecutive_known_to_stop=10)  # smaller threshold for the test
    search = SearchConfig(nome="A", url="https://example.test/")
    _scrape(db=db, search=search, cfg=cfg, pages=initial_pages,
            is_first_run=True, tmp_path=tmp_path)

    smaller_pages = {1: [_re(i) for i in range(1, 6)], 2: []}
    # 3 consecutive runs that miss IDs 6..10.
    for _ in range(3):
        _, _, _, seen = _scrape(
            db=db, search=search, cfg=cfg, pages=smaller_pages,
            is_first_run=False, tmp_path=tmp_path,
        )
        db.mark_missing_as_stale(
            ricerca_nome="A",
            seen_ids=seen,
            run_started_at=datetime.utcnow(),
            runs_missed_threshold=3,
        )

    rows = {r["id"]: r for r in db.all_listings()}
    for i in range(1, 6):
        assert rows[i]["attivo"] == 1
    for i in range(6, 11):
        assert rows[i]["attivo"] == 0


def test_stop_condition_full_known_page(db: Database, tmp_path: Path):
    """A page with 25 listings, all already-known and unchanged, should stop."""
    initial = {
        1: [_re(i) for i in range(1, 26)],
        2: [_re(i) for i in range(26, 51)],
        3: [],
    }
    cfg = _make_cfg()
    search = SearchConfig(nome="A", url="https://example.test/")
    _scrape(db=db, search=search, cfg=cfg, pages=initial,
            is_first_run=True, tmp_path=tmp_path)

    # Second run with the SAME data — page 1 is "all known" → stop after
    # page 1, so we never even fetch page 2.
    visited: list[int] = []

    @dataclass
    class TrackingFetcher(FakeFetcher):
        def fetch(self, url):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query)
            visited.append(int(q.get("pag", ["1"])[0]))
            return super().fetch(url)

    f = TrackingFetcher(pages_by_url=initial)
    cp = state_mod.Checkpoint(config_signature="x", started_at="now")
    n_new, n_upd, n_unch, _ = _scrape_search(
        fetcher=f, db=db, search=search, cfg=cfg,
        checkpoint=cp, checkpoint_path=Path(tmp_path) / "cp.json",
        error_log=[], dry_run=False, is_first_run=False,
    )
    assert n_new == 0
    assert n_upd == 0
    assert n_unch == 25
    assert visited == [1]
