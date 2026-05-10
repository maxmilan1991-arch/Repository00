"""YAML config parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the YAML config is missing required fields or malformed."""


@dataclass
class SearchConfig:
    nome: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)

    def render_url(self, page: int = 1, sort_by_recent: bool = True) -> str:
        """Compose the full URL with merged query params for the given page.

        We always *append* params here; we do not try to parse and rewrite
        an existing query string in the user-supplied URL. If the URL the
        user pasted already encodes ordering or filters, those keys win
        (because the browser used them as canonical), but the params from
        the YAML add to it.
        """
        from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

        parsed = urlparse(self.url)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))

        merged: dict[str, Any] = {}
        merged.update({str(k): str(v) for k, v in self.params.items()})
        # User URL params take precedence — the user pasted them on purpose.
        merged.update(existing)

        if sort_by_recent and "criterio" not in merged:
            # Best guess for "ordina per data annuncio decrescente"; if
            # immobiliare.it changes the parameter name in the future, the
            # parser still works because it only depends on __NEXT_DATA__.
            merged["criterio"] = "dataannuncio"
            merged["ordine"] = "desc"

        if page and page > 1:
            merged["pag"] = str(page)

        new_query = urlencode(merged, doseq=True)
        return urlunparse(parsed._replace(query=new_query))


@dataclass
class AppConfig:
    output_dir: Path = Path("./out")
    delay_between_pages_sec: float = 2.0
    max_pages_per_search: int = 100
    fetch_full_description: bool = False
    headless: bool = True
    consecutive_known_to_stop: int = 25
    runs_missed_before_stale: int = 3
    searches: list[SearchConfig] = field(default_factory=list)
    raw_yaml: str = ""

    def search_by_name(self, name: str) -> SearchConfig | None:
        for s in self.searches:
            if s.nome == name:
                return s
        return None


def load_config(path: str | Path) -> AppConfig:
    """Read a YAML file and return a validated ``AppConfig``."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")

    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ConfigError("top-level YAML must be a mapping")

    return _build_config(data, raw_yaml=raw)


def load_config_from_dict(data: dict[str, Any]) -> AppConfig:
    """Same as ``load_config``, but takes an already-parsed dict.

    Useful for tests that want to skip touching the filesystem.
    """
    return _build_config(data, raw_yaml=yaml.safe_dump(data, allow_unicode=True))


def _build_config(data: dict[str, Any], *, raw_yaml: str) -> AppConfig:
    raw_searches = data.get("searches")
    if not raw_searches or not isinstance(raw_searches, list):
        raise ConfigError("config must define a non-empty 'searches' list")

    searches: list[SearchConfig] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw_searches):
        if not isinstance(item, dict):
            raise ConfigError(f"searches[{i}] must be a mapping")
        nome = item.get("nome")
        url = item.get("url")
        if not nome or not isinstance(nome, str):
            raise ConfigError(f"searches[{i}].nome is required")
        if not url or not isinstance(url, str):
            raise ConfigError(f"searches[{i}].url is required")
        if nome in seen_names:
            raise ConfigError(f"duplicate search name: {nome!r}")
        seen_names.add(nome)
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ConfigError(f"searches[{i}].params must be a mapping")
        searches.append(SearchConfig(nome=nome, url=url, params=dict(params)))

    cfg = AppConfig(
        output_dir=Path(data.get("output_dir", "./out")),
        delay_between_pages_sec=float(data.get("delay_between_pages_sec", 2.0)),
        max_pages_per_search=int(data.get("max_pages_per_search", 100)),
        fetch_full_description=bool(data.get("fetch_full_description", False)),
        headless=bool(data.get("headless", True)),
        consecutive_known_to_stop=int(data.get("consecutive_known_to_stop", 25)),
        runs_missed_before_stale=int(data.get("runs_missed_before_stale", 3)),
        searches=searches,
        raw_yaml=raw_yaml,
    )

    if cfg.max_pages_per_search < 1:
        raise ConfigError("max_pages_per_search must be >= 1")
    if cfg.delay_between_pages_sec < 0:
        raise ConfigError("delay_between_pages_sec must be >= 0")
    if cfg.consecutive_known_to_stop < 1:
        raise ConfigError("consecutive_known_to_stop must be >= 1")
    if cfg.runs_missed_before_stale < 1:
        raise ConfigError("runs_missed_before_stale must be >= 1")

    return cfg
