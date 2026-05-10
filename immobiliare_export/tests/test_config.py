"""Tests for config.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from immobiliare_export.config import (
    ConfigError,
    SearchConfig,
    load_config,
    load_config_from_dict,
)


def test_load_config_minimal(tmp_path: Path):
    p = tmp_path / "cfg.yml"
    p.write_text(
        "searches:\n"
        "  - nome: Test\n"
        "    url: https://www.immobiliare.it/vendita-case/milano/\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.searches) == 1
    assert cfg.searches[0].nome == "Test"
    assert cfg.delay_between_pages_sec == 2.0  # default
    assert cfg.max_pages_per_search == 100  # default


def test_load_config_rejects_missing_searches(tmp_path: Path):
    p = tmp_path / "cfg.yml"
    p.write_text("output_dir: ./out\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_config_rejects_duplicate_names():
    with pytest.raises(ConfigError):
        load_config_from_dict({
            "searches": [
                {"nome": "X", "url": "https://x"},
                {"nome": "X", "url": "https://y"},
            ]
        })


def test_search_render_url_appends_pagination():
    s = SearchConfig(
        nome="Milano",
        url="https://www.immobiliare.it/vendita-case/milano/",
        params={"prezzoMassimo": 500000},
    )
    url = s.render_url(page=2, sort_by_recent=True)
    # All three params should be present in some order.
    assert "prezzoMassimo=500000" in url
    assert "criterio=dataannuncio" in url
    assert "ordine=desc" in url
    assert "pag=2" in url


def test_search_render_url_preserves_user_query():
    """If the user pasted a URL with ordering already in it, keep theirs."""
    s = SearchConfig(
        nome="Milano",
        url="https://www.immobiliare.it/vendita-case/milano/?criterio=prezzo&ordine=asc",
    )
    url = s.render_url(page=1, sort_by_recent=True)
    assert "criterio=prezzo" in url
    assert "ordine=asc" in url


def test_search_by_name():
    cfg = load_config_from_dict({
        "searches": [
            {"nome": "A", "url": "https://a"},
            {"nome": "B", "url": "https://b"},
        ]
    })
    assert cfg.search_by_name("B").url == "https://b"
    assert cfg.search_by_name("Z") is None


def test_invalid_numeric_options():
    with pytest.raises(ConfigError):
        load_config_from_dict({
            "max_pages_per_search": 0,
            "searches": [{"nome": "A", "url": "https://a"}],
        })
