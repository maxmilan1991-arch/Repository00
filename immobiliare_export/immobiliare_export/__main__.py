"""CLI entry point.

Usage:
    python -m immobiliare_export --config ricerca.yml [flags]

The orchestration logic lives here, on top of the (testable, side-effect-free)
parser/database/exporter modules. Anything that touches Playwright is
gated through ``Fetcher`` so unit tests can short-circuit it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import state as state_mod
from .config import AppConfig, SearchConfig, load_config
from .database import (
    INSERT_NEW,
    UNCHANGED,
    UPDATE_PRICE,
    Database,
)
from .description_parser import parse_built_surface
from .exporter import ExportContext, export_workbook
from .parser import ParserError, parse_results_page

logger = logging.getLogger("immobiliare_export")


# --------------------------------------------------------------- arg parsing


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="immobiliare-export",
        description=(
            "Scrape immobiliare.it search results into a local SQLite "
            "archive and export them to a styled .xlsx report."
        ),
    )
    p.add_argument("--config", required=True, help="path to the YAML config file")
    p.add_argument("--output", default=None,
                   help="output xlsx path (default: ./out/immobiliare_<YYYY-MM-DD>.xlsx)")
    p.add_argument("--db", default="./immobiliare.db", help="path to the SQLite DB")
    p.add_argument("--full-rescan", action="store_true",
                   help="ignore the DB state and re-scrape as if it were the first run")
    p.add_argument("--since", default=None,
                   help="mark as 'novità' anything with first_seen >= this date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't write DB or xlsx, just print what would happen")
    p.add_argument("--search", default=None,
                   help="only run the search with the given name (debug)")
    p.add_argument("--reparse-descriptions", action="store_true",
                   help="don't scrape; re-run the description parser on every "
                        "listing already in the DB and exit")
    p.add_argument("--headful", action="store_true",
                   help="open the browser visibly (useful for solving CAPTCHAs)")
    p.add_argument("--log-file", default=None,
                   help="append logs to this file in addition to stderr")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="enable DEBUG-level logging")
    return p


# ----------------------------------------------------------------- logging


def _configure_logging(*, log_file: str | None, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


# -------------------------------------------------- per-search scrape loop


def _scrape_search(
    *,
    fetcher,
    db: Database,
    search: SearchConfig,
    cfg: AppConfig,
    checkpoint: state_mod.Checkpoint,
    checkpoint_path: Path,
    error_log: list[dict[str, Any]],
    dry_run: bool,
    is_first_run: bool,
) -> tuple[int, int, int, set[int]]:
    """Iterate through the pages of a single search.

    Returns ``(n_new, n_updated, n_unchanged, seen_ids)`` for the search.
    """
    entry = checkpoint.entries.setdefault(
        search.nome, state_mod.CheckpointEntry(nome=search.nome)
    )

    n_new = n_updated = n_unchanged = 0
    consecutive_known_run = 0
    consecutive_errors = 0
    seen_ids: set[int] = set(entry.seen_ids)

    start_page = entry.last_completed_page + 1
    logger.info(
        "▶ ricerca %r — partenza dalla pagina %d (max %d)",
        search.nome, start_page, cfg.max_pages_per_search,
    )

    for page in range(start_page, cfg.max_pages_per_search + 1):
        url = search.render_url(page=page, sort_by_recent=True)
        logger.info("ricerca %r, pag %d: GET %s", search.nome, page, url)

        try:
            res = fetcher.fetch(url)
        except Exception as e:
            consecutive_errors += 1
            error_log.append({
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "url": url,
                "kind": type(e).__name__,
                "message": str(e),
            })
            logger.error("fetch fallito (%s)", e)
            if consecutive_errors >= 5:
                logger.error("troppi errori consecutivi su %r — abort search", search.nome)
                break
            continue
        consecutive_errors = 0

        try:
            page_data = parse_results_page(res.html, ricerca_nome=search.nome)
        except ParserError as e:
            error_log.append({
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "url": url,
                "kind": "ParserError",
                "message": str(e),
            })
            logger.error("parsing fallito: %s", e)
            consecutive_errors += 1
            if consecutive_errors >= 5:
                break
            continue

        if not page_data.listings:
            logger.info("pagina vuota — fine ricerca %r", search.nome)
            break

        page_new = page_updated = page_unchanged = 0

        if not dry_run:
            with db.transaction():
                for listing in page_data.listings:
                    res_up = db.upsert_listing(listing)
                    seen_ids.add(listing.id)
                    if res_up.outcome == INSERT_NEW:
                        n_new += 1
                        page_new += 1
                    elif res_up.outcome == UPDATE_PRICE:
                        n_updated += 1
                        page_updated += 1
                    else:
                        n_unchanged += 1
                        page_unchanged += 1
                    _store_built_surface(db, listing.id, listing.descrizione)
        else:
            for listing in page_data.listings:
                seen_ids.add(listing.id)
                # In dry-run we don't know the previous price; we conservatively
                # treat them as "would be new" when the DB row is missing.
                existing = db.get_listing(listing.id)
                if existing is None:
                    n_new += 1
                    page_new += 1
                elif existing["prezzo_corrente"] != listing.prezzo_corrente:
                    n_updated += 1
                    page_updated += 1
                else:
                    n_unchanged += 1
                    page_unchanged += 1

        logger.info(
            "ricerca %r, pag %d/%d: +%d nuovi, +%d aggiornati, %d invariati",
            search.nome, page, cfg.max_pages_per_search,
            page_new, page_updated, page_unchanged,
        )

        # Stop condition: a full page of "already known and unchanged"
        # signals we've passed the part of the index where novelty lives.
        if (
            not is_first_run
            and page_new == 0
            and page_updated == 0
            and page_unchanged >= cfg.consecutive_known_to_stop
        ):
            consecutive_known_run += page_unchanged
            logger.info(
                "ricerca %r: pagina di tutti già noti — stop incrementale",
                search.nome,
            )
            entry.last_completed_page = page
            entry.seen_ids = list(seen_ids)
            break

        entry.last_completed_page = page
        entry.seen_ids = list(seen_ids)
        if not dry_run:
            state_mod.save(checkpoint_path, checkpoint)

        # Stop if we've already seen all the listings the search promised.
        # The site reports ``totalAds`` so we don't need to walk past it.
        max_pages_by_total = -(-page_data.total_ads // 25)  # ceil
        if page >= max_pages_by_total > 0:
            logger.info("raggiunta l'ultima pagina effettiva (%d).", page)
            break

        fetcher.sleep_between_pages()

    entry.finished = True
    return n_new, n_updated, n_unchanged, seen_ids


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(log_file=args.log_file, verbose=args.verbose)

    cfg = load_config(args.config)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    if args.reparse_descriptions:
        # Don't touch the network. Just re-run the parser over the
        # existing DB and exit — no xlsx is emitted because no run
        # actually happened.
        db = Database(args.db)
        _reparse_all_descriptions(db)
        db.close()
        return 0

    if args.search:
        only = cfg.search_by_name(args.search)
        if only is None:
            logger.error("ricerca %r non trovata in config", args.search)
            return 2
        cfg.searches = [only]

    output_path = (
        Path(args.output)
        if args.output
        else cfg.output_dir / f"immobiliare_{datetime.now():%Y-%m-%d}.xlsx"
    )

    db = Database(args.db)
    is_first_run = db.latest_run() is None
    if args.full_rescan and not args.dry_run:
        logger.info("--full-rescan: azzero runs_missed e last_seen")
        db.reset_for_full_rescan()
        is_first_run = True

    run_started = datetime.utcnow()
    run_id = -1
    if not args.dry_run:
        run_id = db.start_run(cfg.raw_yaml)

    checkpoint_path = state_mod.default_checkpoint_path(args.db)
    sig = state_mod.signature_for_config(
        cfg.raw_yaml, [s.nome for s in cfg.searches]
    )
    checkpoint = state_mod.load(checkpoint_path)
    if checkpoint is None or checkpoint.config_signature != sig:
        checkpoint = state_mod.Checkpoint(
            config_signature=sig,
            started_at=run_started.isoformat(timespec="seconds"),
        )

    counters = {"n_new": 0, "n_updated": 0, "n_unchanged": 0, "n_stale": 0}
    error_log: list[dict[str, Any]] = []

    if args.dry_run:
        logger.warning("DRY-RUN: nessuna scrittura su DB o xlsx")

    fetcher_cm = _build_fetcher(cfg, args)

    try:
        with fetcher_cm as fetcher:
            for search in cfg.searches:
                cp_entry = checkpoint.entries.get(search.nome)
                if cp_entry and cp_entry.finished and not args.full_rescan:
                    logger.info(
                        "ricerca %r già completata in questo run — skip",
                        search.nome,
                    )
                    continue
                try:
                    n_new, n_upd, n_unch, seen_ids = _scrape_search(
                        fetcher=fetcher,
                        db=db,
                        search=search,
                        cfg=cfg,
                        checkpoint=checkpoint,
                        checkpoint_path=checkpoint_path,
                        error_log=error_log,
                        dry_run=args.dry_run,
                        is_first_run=is_first_run,
                    )
                except Exception as e:
                    logger.exception("ricerca %r fallita: %s", search.nome, e)
                    error_log.append({
                        "ts": datetime.utcnow().isoformat(timespec="seconds"),
                        "url": search.url,
                        "kind": type(e).__name__,
                        "message": str(e),
                    })
                    continue

                counters["n_new"] += n_new
                counters["n_updated"] += n_upd
                counters["n_unchanged"] += n_unch

                if not args.dry_run:
                    n_stale = db.mark_missing_as_stale(
                        ricerca_nome=search.nome,
                        seen_ids=seen_ids,
                        run_started_at=run_started,
                        runs_missed_threshold=cfg.runs_missed_before_stale,
                    )
                    counters["n_stale"] += n_stale
                    state_mod.save(checkpoint_path, checkpoint)
    finally:
        run_finished = datetime.utcnow()
        if not args.dry_run and run_id >= 0:
            db.finish_run(
                run_id,
                n_new=counters["n_new"],
                n_updated=counters["n_updated"],
                n_unchanged=counters["n_unchanged"],
                n_stale=counters["n_stale"],
            )

    if args.dry_run:
        logger.info("DRY-RUN counters: %s", counters)
        logger.info("output xlsx (would be) -> %s", output_path)
        db.close()
        return 0

    novita_since = None
    if args.since:
        try:
            novita_since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            logger.error("--since deve essere YYYY-MM-DD")
            db.close()
            return 2

    ctx = ExportContext(
        config_yaml=cfg.raw_yaml,
        run_started_at=run_started,
        run_finished_at=run_finished,
        run_counters=counters,
        errors=error_log,
        novita_since=novita_since,
    )
    out = export_workbook(db, output_path, ctx)
    logger.info("xlsx scritto in %s", out)

    state_mod.clear(checkpoint_path)
    db.close()
    return 0


def _store_built_surface(db: Database, listing_id: int, description: str | None) -> None:
    """Run the description parser and persist the result for one listing.

    Failures are non-fatal: a malformed description should never abort
    a scrape run. They're logged at DEBUG so the caller can investigate
    if recall looks suspiciously low.
    """
    try:
        result = parse_built_surface(description or "")
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("description parser failed for listing %d: %s", listing_id, e)
        return
    db.set_built_surface(
        listing_id,
        totale_edificato_mq=result["totale_edificato_mq"],
        componenti=result["componenti"],
        note=result["note_parsing"],
    )


def _reparse_all_descriptions(db: Database) -> int:
    """Re-run the parser over every listing already in the DB.

    Returns the number of listings that ended up with a non-NULL
    ``edificato_mq``. Useful for back-filling the columns on legacy
    rows or after parser improvements.
    """
    rows = db.iter_listings_for_reparse()
    n_hits = 0
    for row in rows:
        result = parse_built_surface(row["descrizione"] or "")
        db.set_built_surface(
            int(row["id"]),
            totale_edificato_mq=result["totale_edificato_mq"],
            componenti=result["componenti"],
            note=result["note_parsing"],
        )
        if result["totale_edificato_mq"] is not None:
            n_hits += 1
    logger.info(
        "reparse done: %d listings scanned, %d con superficie edificata",
        len(rows), n_hits,
    )
    return n_hits


def _build_fetcher(cfg: AppConfig, args: argparse.Namespace):
    """Produce a context-manager that owns the Fetcher lifecycle.

    For dry-runs that don't actually touch the network, callers can still
    pass a fake fetcher in tests. By default we wire up Playwright.
    """
    from .fetcher import Fetcher

    return Fetcher(
        headless=False if args.headful else cfg.headless,
        delay_between_pages_sec=cfg.delay_between_pages_sec,
        connect_to_existing_browser=cfg.connect_to_existing_browser,
        cdp_endpoint=cfg.cdp_endpoint,
    )


if __name__ == "__main__":
    sys.exit(main())
