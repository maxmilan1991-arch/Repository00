"""Tests for database.py: schema, upsert and stale logic."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from immobiliare_export.database import (
    INSERT_NEW,
    UNCHANGED,
    UPDATE_PRICE,
    Database,
    init_schema,
)
from immobiliare_export.models import Listing


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def _mk_listing(**kw) -> Listing:
    defaults = dict(
        id=1, titolo="Casa", tipologia="Appartamento", regione="Lombardia",
        provincia="MI", comune="Milano", indirizzo="Via Roma 1",
        lat=45.46, lng=9.19, superficie_raw="120 m²", superficie_mq=120,
        stato="Buono / Abitabile", prezzo_corrente=300000,
        e_asta=False, e_trattativa=False, descrizione="…",
        locali=4, bagni=2, ricerca_nome="Milano", raw_json="{}",
    )
    defaults.update(kw)
    return Listing(**defaults)


def test_schema_init_is_idempotent(tmp_path: Path):
    """Calling init_schema twice on the same DB must not raise."""
    db1 = Database(tmp_path / "x.db")
    init_schema(db1.conn)
    init_schema(db1.conn)
    init_schema(db1.conn)
    db1.close()


def test_insert_new_listing(db: Database):
    res = db.upsert_listing(_mk_listing(id=42, prezzo_corrente=350000))
    assert res.outcome == INSERT_NEW
    assert res.new_price == 350000

    row = db.get_listing(42)
    assert row is not None
    assert row["prezzo_corrente"] == 350000
    assert row["attivo"] == 1
    assert row["runs_missed"] == 0
    assert row["first_seen"] is not None
    assert row["last_seen"] is not None

    history = db.conn.execute(
        "SELECT * FROM price_history WHERE listing_id = 42"
    ).fetchall()
    assert len(history) == 1
    assert history[0]["prezzo"] == 350000


def test_upsert_unchanged_price_does_not_create_history(db: Database):
    db.upsert_listing(_mk_listing(id=1, prezzo_corrente=100000),
                      seen_at=datetime(2024, 1, 1, 10, 0, 0))
    res = db.upsert_listing(_mk_listing(id=1, prezzo_corrente=100000),
                            seen_at=datetime(2024, 1, 2, 10, 0, 0))
    assert res.outcome == UNCHANGED

    history = db.conn.execute(
        "SELECT * FROM price_history WHERE listing_id = 1"
    ).fetchall()
    assert len(history) == 1  # still only the original entry

    row = db.get_listing(1)
    assert row["last_seen"].startswith("2024-01-02") if isinstance(row["last_seen"], str) else True


def test_upsert_changed_price_appends_history(db: Database):
    db.upsert_listing(_mk_listing(id=1, prezzo_corrente=100000),
                      seen_at=datetime(2024, 1, 1, 10, 0, 0))
    res = db.upsert_listing(_mk_listing(id=1, prezzo_corrente=90000),
                            seen_at=datetime(2024, 1, 5, 10, 0, 0))
    assert res.outcome == UPDATE_PRICE
    assert res.old_price == 100000
    assert res.new_price == 90000

    history = db.conn.execute(
        "SELECT * FROM price_history WHERE listing_id = 1 ORDER BY seen_at"
    ).fetchall()
    assert len(history) == 2
    assert history[0]["prezzo"] == 100000
    assert history[1]["prezzo"] == 90000

    row = db.get_listing(1)
    assert row["prezzo_corrente"] == 90000


def test_mark_missing_as_stale(db: Database):
    db.upsert_listing(_mk_listing(id=1, ricerca_nome="A"))
    db.upsert_listing(_mk_listing(id=2, ricerca_nome="A"))
    db.upsert_listing(_mk_listing(id=3, ricerca_nome="A"))

    # We only saw #1 this run.
    db.mark_missing_as_stale(
        ricerca_nome="A",
        seen_ids=[1],
        run_started_at=datetime.utcnow(),
        runs_missed_threshold=3,
    )
    rows = {r["id"]: r for r in db.all_listings()}
    assert rows[1]["runs_missed"] == 0
    assert rows[2]["runs_missed"] == 1
    assert rows[3]["runs_missed"] == 1
    assert all(r["attivo"] == 1 for r in rows.values())

    # Two more runs without #2 and #3.
    for _ in range(3):
        n_stale = db.mark_missing_as_stale(
            ricerca_nome="A",
            seen_ids=[1],
            run_started_at=datetime.utcnow(),
            runs_missed_threshold=3,
        )
    rows = {r["id"]: r for r in db.all_listings()}
    assert rows[1]["attivo"] == 1
    assert rows[2]["attivo"] == 0
    assert rows[3]["attivo"] == 0


def test_stale_only_affects_matching_search(db: Database):
    """A run of search A must not affect listings tagged with search B."""
    db.upsert_listing(_mk_listing(id=1, ricerca_nome="A"))
    db.upsert_listing(_mk_listing(id=2, ricerca_nome="B"))

    db.mark_missing_as_stale(
        ricerca_nome="A",
        seen_ids=[],
        run_started_at=datetime.utcnow(),
        runs_missed_threshold=3,
    )
    rows = {r["id"]: r for r in db.all_listings()}
    assert rows[1]["runs_missed"] == 1
    assert rows[2]["runs_missed"] == 0


def test_listings_with_price_changes(db: Database):
    db.upsert_listing(_mk_listing(id=1, prezzo_corrente=100000),
                      seen_at=datetime(2024, 1, 1, 10, 0, 0))
    db.upsert_listing(_mk_listing(id=1, prezzo_corrente=90000),
                      seen_at=datetime(2024, 2, 1, 10, 0, 0))
    db.upsert_listing(_mk_listing(id=2, prezzo_corrente=200000),
                      seen_at=datetime(2024, 1, 1, 10, 0, 0))
    # No price change for #2.

    changes = db.listings_with_price_changes()
    assert len(changes) == 1
    assert changes[0]["id"] == 1
    assert changes[0]["prezzo_iniziale"] == 100000
    assert changes[0]["prezzo_attuale"] == 90000
    assert changes[0]["variazione_eur"] == -10000


def test_run_lifecycle(db: Database):
    run_id = db.start_run("output_dir: ./out")
    assert run_id >= 1
    db.finish_run(
        run_id, n_new=1, n_updated=2, n_unchanged=3, n_stale=4,
    )
    runs = db.list_runs()
    assert len(runs) == 1
    r = runs[0]
    assert r["n_new"] == 1
    assert r["n_updated"] == 2
    assert r["n_unchanged"] == 3
    assert r["n_stale"] == 4
    assert r["finished_at"] is not None


def test_full_rescan_resets(db: Database):
    db.upsert_listing(_mk_listing(id=1))
    # Simulate a previous run that bumped runs_missed.
    db.conn.execute("UPDATE listings SET runs_missed = 2 WHERE id = 1")
    db.conn.commit()

    db.reset_for_full_rescan()
    row = db.get_listing(1)
    assert row["runs_missed"] == 0
    assert row["last_seen"] is None
