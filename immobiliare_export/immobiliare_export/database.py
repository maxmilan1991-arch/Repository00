"""SQLite persistence: schema, upserts and incremental queries.

A run of the scraper interacts with the DB through three primary entry
points:

* :func:`init_schema` — idempotent ``CREATE TABLE … IF NOT EXISTS`` block;
* :func:`Database.upsert_listing` — applies the new/updated/unchanged logic
  for a single listing in one transaction;
* :func:`Database.mark_missing_as_stale` — at the end of a search run, bumps
  ``runs_missed`` for the active listings that were not seen, and flips
  the ``attivo`` flag once the threshold is crossed.

All timestamps are written as ISO-8601 strings ("UTC naive" by default) so
they stay sortable and trivially diffable by hand.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import Listing

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id              INTEGER PRIMARY KEY,
    titolo          TEXT,
    tipologia       TEXT,
    regione         TEXT,
    provincia       TEXT,
    comune          TEXT,
    indirizzo       TEXT,
    lat             REAL,
    lng             REAL,
    superficie_raw  TEXT,
    superficie_mq   INTEGER,
    stato           TEXT,
    prezzo_corrente INTEGER,
    e_asta          INTEGER NOT NULL DEFAULT 0,
    e_trattativa    INTEGER NOT NULL DEFAULT 0,
    descrizione     TEXT,
    locali          INTEGER,
    bagni           INTEGER,
    ricerca_nome    TEXT,
    first_seen      TIMESTAMP,
    last_seen       TIMESTAMP,
    runs_missed     INTEGER NOT NULL DEFAULT 0,
    attivo          INTEGER NOT NULL DEFAULT 1,
    raw_json        TEXT,
    edificato_mq            INTEGER,
    edificato_componenti    TEXT,
    edificato_note          TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id      INTEGER NOT NULL,
    seen_at         TIMESTAMP NOT NULL,
    prezzo          INTEGER,
    FOREIGN KEY (listing_id) REFERENCES listings(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    durata_sec      INTEGER,
    n_new           INTEGER NOT NULL DEFAULT 0,
    n_updated       INTEGER NOT NULL DEFAULT 0,
    n_unchanged     INTEGER NOT NULL DEFAULT 0,
    n_stale         INTEGER NOT NULL DEFAULT 0,
    config_yaml     TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_attivo ON listings(attivo);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen);
CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON price_history(listing_id, seen_at);
"""


# Constants returned by upsert_listing so callers can tally counts.
INSERT_NEW = "new"
UPDATE_PRICE = "updated"
UNCHANGED = "unchanged"


@dataclass
class UpsertResult:
    listing_id: int
    outcome: str  # one of INSERT_NEW / UPDATE_PRICE / UNCHANGED
    old_price: int | None
    new_price: int | None


class Database:
    """Lightweight wrapper around a SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn: sqlite3.Connection = sqlite3.connect(
            self.path, detect_types=sqlite3.PARSE_DECLTYPES
        )
        self.conn.row_factory = sqlite3.Row
        # Foreign key enforcement is opt-in in SQLite; we want it.
        self.conn.execute("PRAGMA foreign_keys = ON;")
        init_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------------- runs
    def start_run(self, config_yaml: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, config_yaml) VALUES (?, ?)",
            (_now_iso(), config_yaml),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        n_new: int,
        n_updated: int,
        n_unchanged: int,
        n_stale: int,
    ) -> None:
        finished = _now_iso()
        row = self.conn.execute(
            "SELECT started_at FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        durata = None
        if row and row["started_at"]:
            try:
                start_dt = _parse_iso(row["started_at"])
                durata = int((datetime.utcnow() - start_dt).total_seconds())
            except Exception:
                durata = None
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, durata_sec = ?, n_new = ?, n_updated = ?,
                n_unchanged = ?, n_stale = ?
            WHERE id = ?
            """,
            (finished, durata, n_new, n_updated, n_unchanged, n_stale, run_id),
        )
        self.conn.commit()

    def list_runs(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY id DESC"
            ).fetchall()
        )

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # -------------------------------------------------------------- listings
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic page-level transaction.

        Ensures that if the process is killed mid-page we either have all
        the rows for that page or none of them.
        """
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_listing(self, listing_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()

    def upsert_listing(
        self,
        listing: Listing,
        *,
        seen_at: datetime | None = None,
    ) -> UpsertResult:
        """Apply the incremental rules for a single listing.

        * Not in DB              -> INSERT row + INSERT price_history entry.
        * In DB, same price      -> UPDATE last_seen, reset runs_missed.
        * In DB, different price -> UPDATE prezzo_corrente + INSERT history.

        ``ricerca_nome`` is only written on first insert; on subsequent
        encounters we keep the *first* search that surfaced the listing.
        """
        ts_iso = _to_iso(seen_at or datetime.utcnow())
        existing = self.get_listing(listing.id)
        new_price = listing.prezzo_corrente

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO listings (
                    id, titolo, tipologia, regione, provincia, comune,
                    indirizzo, lat, lng, superficie_raw, superficie_mq,
                    stato, prezzo_corrente, e_asta, e_trattativa,
                    descrizione, locali, bagni, ricerca_nome,
                    first_seen, last_seen, runs_missed, attivo, raw_json
                ) VALUES (
                    :id, :titolo, :tipologia, :regione, :provincia, :comune,
                    :indirizzo, :lat, :lng, :superficie_raw, :superficie_mq,
                    :stato, :prezzo_corrente, :e_asta, :e_trattativa,
                    :descrizione, :locali, :bagni, :ricerca_nome,
                    :first_seen, :last_seen, 0, 1, :raw_json
                )
                """,
                {
                    "id": listing.id,
                    "titolo": listing.titolo,
                    "tipologia": listing.tipologia,
                    "regione": listing.regione,
                    "provincia": listing.provincia,
                    "comune": listing.comune,
                    "indirizzo": listing.indirizzo,
                    "lat": listing.lat,
                    "lng": listing.lng,
                    "superficie_raw": listing.superficie_raw,
                    "superficie_mq": listing.superficie_mq,
                    "stato": listing.stato,
                    "prezzo_corrente": listing.prezzo_corrente,
                    "e_asta": int(bool(listing.e_asta)),
                    "e_trattativa": int(bool(listing.e_trattativa)),
                    "descrizione": listing.descrizione,
                    "locali": listing.locali,
                    "bagni": listing.bagni,
                    "ricerca_nome": listing.ricerca_nome,
                    "first_seen": ts_iso,
                    "last_seen": ts_iso,
                    "raw_json": listing.raw_json,
                },
            )
            self.conn.execute(
                "INSERT INTO price_history (listing_id, seen_at, prezzo) "
                "VALUES (?, ?, ?)",
                (listing.id, ts_iso, new_price),
            )
            return UpsertResult(listing.id, INSERT_NEW, None, new_price)

        old_price = existing["prezzo_corrente"]
        # Always refresh the volatile fields (title, condition, address,
        # description) since the seller may have edited them.
        self.conn.execute(
            """
            UPDATE listings
            SET titolo = :titolo,
                tipologia = COALESCE(:tipologia, tipologia),
                regione = COALESCE(:regione, regione),
                provincia = COALESCE(:provincia, provincia),
                comune = COALESCE(:comune, comune),
                indirizzo = COALESCE(:indirizzo, indirizzo),
                lat = COALESCE(:lat, lat),
                lng = COALESCE(:lng, lng),
                superficie_raw = COALESCE(:superficie_raw, superficie_raw),
                superficie_mq = COALESCE(:superficie_mq, superficie_mq),
                stato = COALESCE(:stato, stato),
                e_asta = :e_asta,
                e_trattativa = :e_trattativa,
                descrizione = COALESCE(:descrizione, descrizione),
                locali = COALESCE(:locali, locali),
                bagni = COALESCE(:bagni, bagni),
                last_seen = :last_seen,
                runs_missed = 0,
                attivo = 1,
                raw_json = COALESCE(:raw_json, raw_json)
            WHERE id = :id
            """,
            {
                "id": listing.id,
                "titolo": listing.titolo,
                "tipologia": listing.tipologia,
                "regione": listing.regione,
                "provincia": listing.provincia,
                "comune": listing.comune,
                "indirizzo": listing.indirizzo,
                "lat": listing.lat,
                "lng": listing.lng,
                "superficie_raw": listing.superficie_raw,
                "superficie_mq": listing.superficie_mq,
                "stato": listing.stato,
                "e_asta": int(bool(listing.e_asta)),
                "e_trattativa": int(bool(listing.e_trattativa)),
                "descrizione": listing.descrizione,
                "locali": listing.locali,
                "bagni": listing.bagni,
                "last_seen": ts_iso,
                "raw_json": listing.raw_json,
            },
        )

        if old_price == new_price:
            return UpsertResult(listing.id, UNCHANGED, old_price, new_price)

        self.conn.execute(
            "UPDATE listings SET prezzo_corrente = ? WHERE id = ?",
            (new_price, listing.id),
        )
        self.conn.execute(
            "INSERT INTO price_history (listing_id, seen_at, prezzo) "
            "VALUES (?, ?, ?)",
            (listing.id, ts_iso, new_price),
        )
        return UpsertResult(listing.id, UPDATE_PRICE, old_price, new_price)

    def set_built_surface(
        self,
        listing_id: int,
        *,
        totale_edificato_mq: int | None,
        componenti: list[dict] | None,
        note: str,
    ) -> None:
        """Persist the description-parser estimate for one listing.

        ``componenti`` is serialised as compact JSON so the audit sheet
        can replay the raw match list without joining another table.
        """
        comp_json = json.dumps(componenti or [], ensure_ascii=False)
        self.conn.execute(
            """
            UPDATE listings
            SET edificato_mq = ?,
                edificato_componenti = ?,
                edificato_note = ?
            WHERE id = ?
            """,
            (totale_edificato_mq, comp_json, note or "", listing_id),
        )
        self.conn.commit()

    def iter_listings_for_reparse(self) -> Iterable[sqlite3.Row]:
        """Yield every row that has at least a snippet to re-parse."""
        return self.conn.execute(
            "SELECT id, descrizione FROM listings WHERE descrizione IS NOT NULL"
        ).fetchall()

    def reset_for_full_rescan(self) -> None:
        """Zero ``runs_missed`` and ``last_seen`` ahead of a full rescan.

        Per the spec, a ``--full-rescan`` should behave as if it were the
        first run: the stop conditions based on "consecutive known"
        already work because we still compare against the existing rows,
        but we don't want listings that happen to be missed during the
        rescan to immediately be flagged as stale.
        """
        self.conn.execute(
            "UPDATE listings SET runs_missed = 0, last_seen = NULL"
        )
        self.conn.commit()

    def mark_missing_as_stale(
        self,
        *,
        ricerca_nome: str,
        seen_ids: Iterable[int],
        run_started_at: datetime,
        runs_missed_threshold: int,
    ) -> int:
        """Bump ``runs_missed`` and flip ``attivo`` for missing listings.

        Operates only on listings whose ``ricerca_nome`` matches the search
        we just ran (a listing surfaced by another search shouldn't get
        decayed by an unrelated run that didn't look for it).

        Returns the number of listings flipped to inactive in this call.
        """
        seen_set = list({int(i) for i in seen_ids})
        cursor = self.conn.cursor()

        if seen_set:
            placeholders = ",".join(["?"] * len(seen_set))
            params: list[Any] = [ricerca_nome] + seen_set
            cursor.execute(
                f"""
                UPDATE listings
                SET runs_missed = runs_missed + 1
                WHERE ricerca_nome = ?
                  AND attivo = 1
                  AND id NOT IN ({placeholders})
                """,
                params,
            )
        else:
            cursor.execute(
                """
                UPDATE listings
                SET runs_missed = runs_missed + 1
                WHERE ricerca_nome = ? AND attivo = 1
                """,
                (ricerca_nome,),
            )

        cursor.execute(
            """
            UPDATE listings
            SET attivo = 0
            WHERE ricerca_nome = ?
              AND attivo = 1
              AND runs_missed >= ?
            """,
            (ricerca_nome, runs_missed_threshold),
        )
        n_stale = cursor.rowcount
        self.conn.commit()
        return n_stale

    # ----------------------------------------------------------- read views
    def all_active_listings(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM listings WHERE attivo = 1 ORDER BY id"
            ).fetchall()
        )

    def all_listings(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT * FROM listings ORDER BY id").fetchall()
        )

    def listings_first_seen_after(
        self, since: datetime
    ) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM listings WHERE first_seen >= ? "
                "ORDER BY first_seen DESC",
                (_to_iso(since),),
            ).fetchall()
        )

    def listings_with_price_changes(self) -> list[dict[str, Any]]:
        """Return listings that have at least 2 price-history entries.

        For each, we compute initial / current price and the delta.
        Sorted by absolute % variation desc, so the biggest movers are
        always at the top of the report.
        """
        rows = self.conn.execute(
            """
            SELECT l.id, l.titolo, l.comune, l.prezzo_corrente,
                   MIN(ph.seen_at) AS first_seen_at,
                   MAX(ph.seen_at) AS last_seen_at,
                   COUNT(ph.listing_id) AS n_entries
            FROM listings l
            JOIN price_history ph ON ph.listing_id = l.id
            GROUP BY l.id
            HAVING n_entries >= 2
            """
        ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            history = self.conn.execute(
                "SELECT prezzo, seen_at FROM price_history "
                "WHERE listing_id = ? ORDER BY seen_at ASC",
                (r["id"],),
            ).fetchall()
            if len(history) < 2:
                continue
            initial = history[0]["prezzo"]
            current = history[-1]["prezzo"]
            if initial is None or current is None or initial == current:
                continue
            delta = current - initial
            pct = (delta / initial * 100.0) if initial else 0.0
            results.append(
                {
                    "id": r["id"],
                    "titolo": r["titolo"],
                    "comune": r["comune"],
                    "prezzo_iniziale": initial,
                    "prezzo_attuale": current,
                    "variazione_eur": delta,
                    "variazione_pct": pct,
                    "data_prima": history[0]["seen_at"],
                    "data_ultima": history[-1]["seen_at"],
                }
            )

        results.sort(key=lambda d: abs(d["variazione_pct"]), reverse=True)
        return results

    def count_active(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE attivo = 1"
            ).fetchone()["n"]
        )


def init_schema(conn: sqlite3.Connection) -> None:
    """Run the idempotent schema creation script and any column migrations.

    Fresh DBs get every column from ``SCHEMA_SQL``. Older DBs created
    before a column existed are upgraded in place with ``ALTER TABLE
    ADD COLUMN`` — SQLite has no ``IF NOT EXISTS`` for columns, so we
    probe ``PRAGMA table_info`` first.
    """
    conn.executescript(SCHEMA_SQL)
    _migrate_add_columns_if_missing(
        conn,
        table="listings",
        columns=[
            ("edificato_mq", "INTEGER"),
            ("edificato_componenti", "TEXT"),
            ("edificato_note", "TEXT"),
        ],
    )
    conn.commit()


def _migrate_add_columns_if_missing(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def _now_iso() -> str:
    return _to_iso(datetime.utcnow())


def _to_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(sep=" ")


def _parse_iso(s: str) -> datetime:
    # Accept both " " and "T" separators.
    return datetime.fromisoformat(s.replace("T", " "))
