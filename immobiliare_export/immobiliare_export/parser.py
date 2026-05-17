"""Extract structured listings from the __NEXT_DATA__ JSON of a results page.

immobiliare.it ships the entire structured payload of each results page in a
``<script id="__NEXT_DATA__" type="application/json">…</script>`` tag. We
prefer parsing that payload over scraping the rendered DOM:

* it is far more stable across UI redesigns;
* every listing on the page is fully described;
* it includes geo-coordinates and condition fields that the visible card
  doesn't surface.

Path inside __NEXT_DATA__:
    data.props.pageProps.dehydratedState.queries[0].state.data
        .totalAds        -> int
        .currentPage     -> int
        .results[]       -> 25 ads, each with .realEstate
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .models import Listing, listing_from_realestate


class ParserError(ValueError):
    """Raised when __NEXT_DATA__ is missing or has an unexpected shape."""


@dataclass
class PageData:
    total_ads: int
    current_page: int
    listings: list[Listing]
    raw_results: list[dict[str, Any]]


_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


class _NextDataExtractor(HTMLParser):
    """Tolerant extractor that copes with stray attributes/whitespace.

    We try the regex first because it's faster and matches what the site
    actually emits. The HTML parser is a fallback for the rare cases where
    the script tag carries unusual attribute ordering.
    """

    def __init__(self) -> None:
        super().__init__()
        self._inside = False
        self.payload: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if attrs_d.get("id") == "__NEXT_DATA__":
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside and self.payload is None:
            self.payload = data


def extract_next_data(html: str) -> dict[str, Any]:
    """Return the parsed JSON contents of the __NEXT_DATA__ <script> tag."""
    if not html:
        raise ParserError("empty HTML")

    m = _NEXT_DATA_RE.search(html)
    payload = m.group(1) if m else None

    if not payload:
        # Fallback for unusual attribute orderings.
        ex = _NextDataExtractor()
        ex.feed(html)
        payload = ex.payload

    if not payload:
        raise ParserError("__NEXT_DATA__ script tag not found")

    payload = payload.strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise ParserError(f"__NEXT_DATA__ is not valid JSON: {e}") from e


def parse_results_page(
    html: str,
    *,
    ricerca_nome: str | None = None,
) -> PageData:
    """Parse a results page and return a ``PageData`` snapshot."""
    nd = extract_next_data(html)

    try:
        queries = nd["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as e:
        raise ParserError(f"unexpected __NEXT_DATA__ shape: {e}") from e

    # Some queries on the page may be unrelated (e.g. autocompletes); pick
    # the first one that has the expected shape.
    state_data: dict[str, Any] | None = None
    for q in queries or []:
        cand = (q or {}).get("state", {}).get("data")
        if isinstance(cand, dict) and "results" in cand:
            state_data = cand
            break
    if state_data is None:
        raise ParserError("no query in __NEXT_DATA__ contained a results array")

    results = state_data.get("results") or []
    if not isinstance(results, list):
        raise ParserError("'results' is not a list")

    listings: list[Listing] = []
    raw_results: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        real_estate = entry.get("realEstate")
        if not isinstance(real_estate, dict):
            continue
        listing = listing_from_realestate(real_estate, ricerca_nome=ricerca_nome)
        if listing is not None:
            listings.append(listing)
            raw_results.append(entry)

    total_ads = int(state_data.get("totalAds") or 0)
    current_page = int(state_data.get("currentPage") or 1)

    return PageData(
        total_ads=total_ads,
        current_page=current_page,
        listings=listings,
        raw_results=raw_results,
    )


def extract_full_description(html: str) -> str | None:
    """Pull the full description text from a listing detail page.

    The index page (``parse_results_page``) carries only a short
    teaser. The detail page (``immobiliare.it/annunci/<id>/``) ships
    the full free-text description inside its ``__NEXT_DATA__``. The
    typical path is

        props.pageProps.dehydratedState.queries[*].state.data
            .detail.realEstate.properties[*].description

    but the site occasionally serves a flatter shape (no ``detail``
    wrapper), so we probe both. Returns ``None`` — never raises — when
    the description can't be found; the caller falls back to the
    snippet captured from the index page.
    """
    try:
        nd = extract_next_data(html)
    except ParserError:
        return None
    try:
        queries = nd["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    if not isinstance(queries, list):
        return None

    for q in queries:
        data = (q or {}).get("state", {}).get("data")
        if not isinstance(data, dict):
            continue
        # Two shapes seen in the wild: data.detail.realEstate.properties
        # and data.realEstate.properties (no ``detail`` wrapper).
        for candidate in (data.get("detail"), data):
            if not isinstance(candidate, dict):
                continue
            real_estate = candidate.get("realEstate")
            if not isinstance(real_estate, dict):
                continue
            for prop in real_estate.get("properties") or []:
                if not isinstance(prop, dict):
                    continue
                desc = prop.get("description") or prop.get("descrizione")
                if isinstance(desc, str) and desc.strip():
                    return desc.strip()
    return None
