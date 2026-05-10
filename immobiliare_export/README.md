# immobiliare-export

CLI tool to scrape real-estate listings from
[immobiliare.it](https://www.immobiliare.it) into a local SQLite archive
and export them to a styled, multi-sheet `.xlsx` report.

* **One command, one Excel.** Every invocation produces a ready-to-share
  workbook.
* **Incremental.** From the second run on, only new / updated listings
  are downloaded — no full re-scrape.
* **Generic.** Works for any geographic area covered by immobiliare.it:
  you point it at one or more *search URLs* you copied from the website,
  no provincial codes are hard-coded.
* **Local only.** SQLite + `.xlsx` + log files; nothing is uploaded
  anywhere.

---

## Quickstart

```bash
# 1. install (editable mode is convenient during development)
pip install -e .

# 2. install the Chromium browser Playwright drives (one-shot)
bash scripts/setup_playwright.sh

# 3. run a scrape
cp ricerca.example.yml ricerca.yml          # edit to taste
python -m immobiliare_export --config ricerca.yml
```

The output is `./out/immobiliare_<YYYY-MM-DD>.xlsx`. Open it in Excel /
LibreOffice / Numbers — every row has a clickable link back to the
listing on immobiliare.it.

---

## How to obtain the right URL from immobiliare.it

1. Open immobiliare.it in your browser.
2. Apply the filters you want **visually**: Comune, Provincia, Regione,
   prezzo, superficie, "Da ristrutturare", and so on.
3. Copy the URL from the address bar.
4. Paste it into the `url:` field of your YAML config.

That's the only "source of truth" for what to scrape — the tool does not
contain a hard-coded list of cities, provinces, or regions.

Common URL shapes you can paste:

| Goal                            | URL                                                              |
|---------------------------------|------------------------------------------------------------------|
| One city                        | `https://www.immobiliare.it/vendita-case/milano/`                |
| Province                        | `https://www.immobiliare.it/vendita-case/venezia-provincia/`     |
| Region                          | `https://www.immobiliare.it/vendita-case/lombardia/`             |
| Vertical (rustici)              | `https://www.immobiliare.it/vendita-rustici-casali/sicilia/`     |
| Custom filters                  | (just copy the URL after applying filters in the browser)        |

---

## CLI flags

| Flag             | Default                                 | Purpose                                                   |
|------------------|-----------------------------------------|-----------------------------------------------------------|
| `--config`       | (required)                              | Path to the YAML config.                                  |
| `--output`       | `./out/immobiliare_<YYYY-MM-DD>.xlsx`   | Path to the produced workbook.                            |
| `--db`           | `./immobiliare.db`                      | Path to the SQLite archive.                               |
| `--full-rescan`  | off                                     | Behave as if it were the first run (zeroes `runs_missed`).|
| `--since`        | (last run start)                        | Mark as "novità" anything with `first_seen >= YYYY-MM-DD`.|
| `--dry-run`      | off                                     | Don't write DB or xlsx; print what would happen.          |
| `--search`       | (all)                                   | Run only the search with the given `nome`.                |
| `--headful`      | off                                     | Open the browser visibly (use for CAPTCHAs).              |
| `--log-file`     | none                                    | Append logs to a file in addition to stderr.              |
| `--verbose / -v` | off                                     | DEBUG-level logging.                                      |

---

## Configuration reference

```yaml
output_dir: ./out
delay_between_pages_sec: 2
max_pages_per_search: 100
fetch_full_description: false
headless: true
consecutive_known_to_stop: 25     # full page of "already known" → stop search
runs_missed_before_stale: 3       # missed for N runs → "non più disponibile"

searches:
  - nome: "Milano - tutta la città"
    url: https://www.immobiliare.it/vendita-case/milano/
    params:
      prezzoMassimo: 5000000
      superficieMinima: 80

  - nome: "Rustici Toscana"
    url: https://www.immobiliare.it/vendita-rustici-casali/toscana/
    params:
      superficieMinima: 200
      prezzoMassimo: 800000
```

`params` is appended to the query string of `url`. If you already
encoded a parameter in the pasted URL it wins (the browser used it as
the canonical form).

---

## Incremental mode — worked example

Suppose your search "Milano centro" matches 1,000 listings.

* **Run 1.** DB is empty, so all 1,000 listings are pulled and labelled
  as "new". The workbook's *Novità* sheet shows all of them.
* **Run 2** (next day). The tool re-orders the search by *date
  descending* and walks page by page. As soon as it sees 25 consecutive
  listings that already exist in the DB and have unchanged prices, it
  stops scanning that search — it has reached the part of the index it
  has already covered. Typical run: 1–3 pages, a handful of new entries.
* **Run 3** (a week later). 30 listings disappeared from the search
  results, 5 changed price.
  * The 5 with new prices are bumped: a new `price_history` row is
    written, *Variazioni di prezzo* in the workbook lists them.
  * The 30 missing ones get `runs_missed = 1`. They're still active.
* **Run 4, 5, 6.** The 30 missing listings keep gaining `runs_missed`.
  Once they hit `runs_missed_before_stale` (default 3) they're flipped
  to `attivo = FALSE` and the workbook renders them with a grey italic
  font under *Status = "non più disponibile"*.

---

## Output workbook layout

| Sheet                | What's in it                                                    |
|----------------------|------------------------------------------------------------------|
| **Listing**          | All active listings, with €/m² formula, highlight rows, filters |
| **Novità**           | Listings whose `first_seen` falls in the current run            |
| **Variazioni di prezzo** | Listings with ≥ 2 entries in `price_history`               |
| **Riepilogo**        | KPIs of the run + distributions and quantile statistics         |
| **Run history**      | Past runs (use to track inflow rhythm over time)                |
| **Configurazione**   | Dump of the YAML config + any errors encountered during scrape  |

---

## Scheduling

Run the tool periodically to receive a fresh `.xlsx` every day/week.

**Cron** (Linux/macOS, every day at 07:00):

```cron
0 7 * * * cd /path/to/project && /usr/bin/python -m immobiliare_export \
    --config ricerca.yml --log-file ./out/run.log
```

**Task Scheduler** (Windows): create a Basic Task that runs
`pythonw.exe -m immobiliare_export --config C:\path\ricerca.yml`.

---

## Known limits of the source site

* immobiliare.it is fronted by Cloudflare/DataDome anti-bot. Most pages
  go through, but a CAPTCHA may pop up. Re-run with `--headful` to
  solve it manually; cookies persist across runs in the Playwright
  profile.
* The `surface` field is sometimes a sum (e.g. terreno + costruito) or
  a range. The DB stores the raw string in `superficie_raw` and a
  best-effort numeric in `superficie_mq`. Treat the numeric as a
  **hint**, not a hard filter.
* Some listings use placeholder prices (1, 100, 1.000, 1.111, 999.000)
  to mean "trattativa privata". They are kept in the DB but flagged via
  `e_trattativa`.
* `totalAds` reported by the site is approximate.

---

## Adapting to other portals (future scope)

The fetcher and parser layers are deliberately decoupled from the rest
of the code. To support a different portal (casa.it, idealista, …):

1. Write a new `parser_<portal>.py` that returns a `PageData` object
   with the same shape.
2. Optionally, swap the pagination/URL builder in `config.py`.
3. Wire it into `__main__.py` behind a `portal:` field in the YAML.

The DB schema, the exporter, and the incremental machinery don't need
to change.

---

## Running the test suite

```bash
pip install -e ".[dev]"
pytest -q
```

The tests don't touch the network — they exercise the parser against
recorded HTML, the DB against in-memory SQLite, and the orchestrator
against a fake fetcher.

---

## Don'ts (by design)

* Don't bypass CAPTCHAs automatically; ask the user via `--headful`.
* Don't ship data to any external service; everything stays local.
* Don't auto-login to immobiliare.it; if needed, log in by hand in
  `--headful` and the cookies are reused.
* Don't run an LLM over descriptions inside the main pipeline. The raw
  description and the full `realEstate` JSON are saved in the DB so an
  external pass can do that later.
