"""Domain models and small pure helpers.

The ``Listing`` dataclass mirrors the row layout of the ``listings`` table in
SQLite. It is intentionally flat so that converting between dataclass /
dict / sqlite row stays trivial.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# Prices used by sellers as placeholders for "trattativa privata" /
# "prezzo su richiesta". Anything matching one of these is flagged but kept
# in the DB to preserve the raw signal.
PRIVATE_NEGOTIATION_PRICES = {1, 100, 999, 1000, 1111, 999000}

# Patterns that we treat as "auction" markers when they show up in either
# the title or the description of a listing.
_AUCTION_PATTERNS = (
    r"all['’]\s*asta",
    r"\basta\b",
    r"vendita\s+giudiziaria",
)
_AUCTION_RE = re.compile("|".join(_AUCTION_PATTERNS), re.IGNORECASE)


@dataclass
class Listing:
    """Single real-estate ad, normalized to the shape of our DB row.

    All optional fields default to ``None`` / sane empties; the parser fills
    in whatever is present in the upstream JSON and leaves the rest blank.
    ``raw_json`` keeps the full ``realEstate`` object so downstream analyses
    (e.g. an LLM pass on descriptions) can reconstruct the original payload
    without re-scraping.
    """

    id: int
    titolo: str = ""
    tipologia: str | None = None
    regione: str | None = None
    provincia: str | None = None
    comune: str | None = None
    indirizzo: str | None = None
    lat: float | None = None
    lng: float | None = None
    superficie_raw: str | None = None
    superficie_mq: int | None = None
    stato: str | None = None
    prezzo_corrente: int | None = None
    e_asta: bool = False
    e_trattativa: bool = False
    descrizione: str | None = None
    locali: int | None = None
    bagni: int | None = None
    ricerca_nome: str | None = None
    raw_json: str | None = None

    # The two timestamps below are managed by the database layer, not by
    # the parser. They are exposed here so callers can pass them around
    # uniformly when needed.
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    def to_db_row(self) -> dict[str, Any]:
        d = asdict(self)
        # first_seen / last_seen are managed by the DB layer.
        d.pop("first_seen", None)
        d.pop("last_seen", None)
        return d


def parse_surface(raw: str | None) -> int | None:
    """Extract the leading integer from a surface string.

    Examples:
        "557 m²"    -> 557
        "1.234 m²"  -> 1234
        "n.d."      -> None
        None        -> None

    The site sometimes uses surface as a sum of multiple parts ("100 + 50 m²")
    or as a range. We pick the first numeric token, but the caller is
    expected to also keep ``superficie_raw`` for any ambiguous case.
    """
    if not raw:
        return None
    m = re.search(r"(\d[\d\.\s]*)", raw)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def is_private_negotiation(price: int | None) -> bool:
    """True if the price looks like a placeholder for ``trattativa privata``."""
    if price is None:
        return True
    return price in PRIVATE_NEGOTIATION_PRICES


def is_auction(title: str | None, description: str | None) -> bool:
    """True if either the title or description looks like an auction listing."""
    blob = " ".join(s for s in (title, description) if s)
    if not blob:
        return False
    return bool(_AUCTION_RE.search(blob))


def listing_url(listing_id: int) -> str:
    """Canonical public URL for a listing on immobiliare.it."""
    return f"https://www.immobiliare.it/annunci/{listing_id}/"


def serialize_realestate(raw: dict[str, Any]) -> str:
    """Serialize a ``realEstate`` dict to a stable JSON string for storage."""
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)


def listing_from_realestate(
    real_estate: dict[str, Any],
    *,
    ricerca_nome: str | None = None,
) -> Listing | None:
    """Map a ``realEstate`` JSON node from __NEXT_DATA__ to a ``Listing``.

    Returns ``None`` if the entry is malformed (e.g. missing ID), so that
    the caller can skip it without bringing down the whole page.
    """
    try:
        listing_id = int(real_estate["id"])
    except (KeyError, TypeError, ValueError):
        return None

    title = (real_estate.get("title") or "").strip()
    price_value = None
    price_block = real_estate.get("price") or {}
    if isinstance(price_block, dict):
        try:
            price_value = int(price_block.get("value")) if price_block.get("value") is not None else None
        except (TypeError, ValueError):
            price_value = None

    properties = real_estate.get("properties") or []
    prop = properties[0] if properties else {}

    typology = ((prop.get("typology") or {}).get("name")) if isinstance(prop.get("typology"), dict) else None
    surface_raw = prop.get("surface")
    surface_mq = parse_surface(surface_raw)
    rooms = _safe_int(prop.get("rooms"))
    bathrooms = _safe_int(prop.get("bathrooms"))
    condition = prop.get("ga4Condition")
    description = prop.get("description")

    location = prop.get("location") or {}
    if not isinstance(location, dict):
        location = {}
    region = location.get("region") or location.get("regione")
    province = location.get("province") or location.get("provincia")
    city = location.get("city") or location.get("comune")
    address = location.get("address") or location.get("indirizzo")
    lat = _safe_float(location.get("latitude"))
    lng = _safe_float(location.get("longitude"))

    return Listing(
        id=listing_id,
        titolo=title,
        tipologia=typology,
        regione=region,
        provincia=province,
        comune=city,
        indirizzo=address,
        lat=lat,
        lng=lng,
        superficie_raw=surface_raw,
        superficie_mq=surface_mq,
        stato=condition,
        prezzo_corrente=price_value,
        e_asta=is_auction(title, description),
        e_trattativa=is_private_negotiation(price_value),
        descrizione=description,
        locali=rooms,
        bagni=bathrooms,
        ricerca_nome=ricerca_nome,
        raw_json=serialize_realestate(real_estate),
    )


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        # rooms can be "5+" on immobiliare.it; pick the leading digits.
        m = re.match(r"\d+", str(v))
        return int(m.group(0)) if m else None


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
