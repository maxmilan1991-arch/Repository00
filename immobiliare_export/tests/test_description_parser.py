"""Tests for the built-surface description parser.

Cases cover the categories listed in the spec:
* simple sums of 2-3 components,
* explicit totals that override component-sum,
* outdoor surfaces (terrazza, giardino, oliveto, …) that must NOT
  participate in the total,
* combinations of habitable + rudere + outbuilding,
* multi-unit phrasings ("due appartamenti da 60 mq"),
* number-format variants ("ca. 220", "mq 220", "220 mq circa", "1.200"),
* texts without any surface mention,
* "edificabilità di X mq" potential rights that don't count.
"""

from __future__ import annotations

import pytest

from immobiliare_export.description_parser import parse_built_surface


# --------------------------------------------------- 1) simple component sum

def test_two_components_summed():
    r = parse_built_surface(
        "Vendesi compendio composto da casa principale di 220 mq e "
        "dependance di 80 mq."
    )
    assert r["totale_edificato_mq"] == 300
    tipi = sorted(c["tipo"] for c in r["componenti"])
    assert tipi == ["dependance", "fabbricato_principale"]


def test_three_components_summed():
    r = parse_built_surface(
        "Abitazione di 220 mq, dependance di 80 mq, locale tecnico di 30 mq."
    )
    assert r["totale_edificato_mq"] == 330
    assert len(r["componenti"]) == 3


def test_two_outbuildings_summed():
    r = parse_built_surface("Stalla di 40 mq + magazzino di 25 mq.")
    assert r["totale_edificato_mq"] == 65
    assert all(c["tipo"] == "annesso" for c in r["componenti"])


def test_complex_compound():
    r = parse_built_surface(
        "Complesso composto da: casa principale 180 mq, foresteria 75 mq, "
        "annesso agricolo 45 mq."
    )
    assert r["totale_edificato_mq"] == 300
    by_type = {c["tipo"]: c["mq"] for c in r["componenti"]}
    assert by_type["fabbricato_principale"] == 180
    assert by_type["dependance"] == 75
    assert by_type["annesso"] == 45


# --------------------------------------------------- 2) explicit total wins

def test_explicit_total_overrides_components():
    r = parse_built_surface(
        "Superficie lorda complessiva costruita: 380 mq, di cui casa "
        "principale 220 mq, dependance 100 mq, stalla 60 mq."
    )
    assert r["totale_edificato_mq"] == 380
    assert r["componenti"][0]["tipo"] == "totale_esplicito"
    assert len(r["componenti"]) == 1


def test_totale_superficie_coperta():
    r = parse_built_surface("Totale superficie coperta: 420 mq.")
    assert r["totale_edificato_mq"] == 420
    assert r["componenti"][0]["tipo"] == "totale_esplicito"


def test_superficie_edificata_alias():
    r = parse_built_surface("Superficie edificata 250 mq.")
    assert r["totale_edificato_mq"] == 250


# --------------------------------------------------- 3) outdoor not summed

def test_terrazzo_not_counted():
    r = parse_built_surface(
        "Casa di 220 mq con terrazzo di 30 mq affacciato sul giardino."
    )
    assert r["totale_edificato_mq"] == 220


def test_giardino_alone_yields_none():
    r = parse_built_surface(
        "Lotto con giardino di 1500 mq, oliveto di 3000 mq."
    )
    assert r["totale_edificato_mq"] is None
    assert r["componenti"] == []


def test_terreno_oliveto_vigneto_excluded():
    r = parse_built_surface(
        "Casa di 180 mq, terreno di 5000 mq, oliveto di 2000 mq, vigneto "
        "di 1000 mq."
    )
    assert r["totale_edificato_mq"] == 180


def test_piscina_not_counted():
    r = parse_built_surface(
        "Villa di 350 mq con piscina esterna di 80 mq e giardino."
    )
    assert r["totale_edificato_mq"] == 350


# --------------------------------------------------- 4) rudere + abitabile

def test_rudere_simple():
    r = parse_built_surface("Rudere di circa 80 mq da ristrutturare.")
    assert r["totale_edificato_mq"] == 80
    assert r["componenti"][0]["tipo"] == "rudere"


def test_rudere_pre_unit():
    r = parse_built_surface("Vendesi 150mq di rudere in pietra.")
    assert r["totale_edificato_mq"] == 150
    assert r["componenti"][0]["tipo"] == "rudere"


def test_rudere_with_unit_first():
    r = parse_built_surface("Rudere da ristrutturare di circa mq 200.")
    assert r["totale_edificato_mq"] == 200
    assert r["componenti"][0]["tipo"] == "rudere"


def test_rudere_plus_habitable():
    r = parse_built_surface(
        "Casa abitabile di 180 mq con annesso rudere di 90 mq."
    )
    assert r["totale_edificato_mq"] == 270
    tipi = sorted(c["tipo"] for c in r["componenti"])
    assert tipi == ["fabbricato_principale", "rudere"]


# --------------------------------------------------- 5) multi-unit phrasing

def test_due_appartamenti_da():
    r = parse_built_surface(
        "Edificio diviso in due appartamenti da 60 mq ciascuno."
    )
    assert r["totale_edificato_mq"] == 120


def test_due_unita_abitative():
    r = parse_built_surface("Due unità abitative da 90 mq e 60 mq.")
    assert r["totale_edificato_mq"] == 150


def test_fabbricato_a_and_b():
    r = parse_built_surface("Fabbricato A: 90 mq; fabbricato B: 60 mq.")
    assert r["totale_edificato_mq"] == 150
    assert all(c["tipo"] == "fabbricato_principale" for c in r["componenti"])


# --------------------------------------------------- 6) number-format variants

def test_circa_prefix():
    r = parse_built_surface("Casa di circa 220 mq.")
    assert r["totale_edificato_mq"] == 220


def test_circa_abbreviation():
    r = parse_built_surface("Casa di ca. 220 mq.")
    assert r["totale_edificato_mq"] == 220


def test_circa_postfix():
    r = parse_built_surface("Casa di 220 mq circa.")
    assert r["totale_edificato_mq"] == 220


def test_unit_then_number():
    r = parse_built_surface("Casa principale di mq 180.")
    assert r["totale_edificato_mq"] == 180


def test_m_squared_symbol():
    r = parse_built_surface("Fabbricato di 150 m².")
    assert r["totale_edificato_mq"] == 150


def test_metri_quadrati_spelled_out():
    r = parse_built_surface("Villa di 300 metri quadrati con giardino.")
    assert r["totale_edificato_mq"] == 300


def test_no_whitespace_between_number_and_unit():
    r = parse_built_surface("Rudere di 80mq.")
    assert r["totale_edificato_mq"] == 80


def test_thousands_separator_with_dot():
    r = parse_built_surface(
        "Casale di 1.200 mq complessivi distribuiti su più livelli."
    )
    # Cross-check the number was parsed as 1200, not 1.
    assert r["totale_edificato_mq"] == 1200


def test_range_with_tra_e():
    r = parse_built_surface("Abitazione tra 200 e 250 mq.")
    assert r["totale_edificato_mq"] == 225


def test_range_with_dash():
    r = parse_built_surface("Casa di 200-250 mq da ristrutturare.")
    # 200-250 picked as range → 225. Classifier sees "casa" before → fabbricato.
    assert r["totale_edificato_mq"] == 225


def test_oltre_uses_lower_bound():
    r = parse_built_surface("Oltre 200 mq di casa colonica.")
    assert r["totale_edificato_mq"] == 200


# --------------------------------------------------- 7) no surface at all

def test_empty_string():
    r = parse_built_surface("")
    assert r["totale_edificato_mq"] is None
    assert r["componenti"] == []
    assert "nessuna superficie" in r["note_parsing"]


def test_none_input():
    r = parse_built_surface(None)  # type: ignore[arg-type]
    assert r["totale_edificato_mq"] is None


def test_text_without_surfaces():
    r = parse_built_surface(
        "Splendida villa immersa nella natura, ottima esposizione e "
        "vista panoramica. Trattativa riservata."
    )
    assert r["totale_edificato_mq"] is None


def test_only_outdoor_surfaces():
    r = parse_built_surface(
        "Terreno agricolo di 5000 mq con oliveto centenario di 2000 mq."
    )
    assert r["totale_edificato_mq"] is None


# --------------------------------------------------- 8) edificabilità potenziale

def test_edificabilita_not_counted():
    r = parse_built_surface(
        "Lotto edificabile con edificabilità di 500 mq concessa dal piano."
    )
    assert r["totale_edificato_mq"] is None


def test_cubatura_not_counted():
    r = parse_built_surface(
        "Casa di 180 mq con cubatura aggiuntiva concessa di 200 mq."
    )
    # The 180 must count; the 200 (cubatura concessa) must not.
    assert r["totale_edificato_mq"] == 180


def test_potential_buildings_skipped():
    r = parse_built_surface(
        "Possibilità di edificare ulteriori 300 mq sul lotto residuo."
    )
    assert r["totale_edificato_mq"] is None


# --------------------------------------------------- 9) high-value warning

def test_high_value_triggers_warning():
    r = parse_built_surface("Casale di 2.000 mq tutto edificato.")
    assert r["totale_edificato_mq"] == 2000
    assert "eccezionalmente alto" in r["note_parsing"]


# --------------------------------------------------- 10) audit data shape

def test_components_carry_original_fragment():
    r = parse_built_surface(
        "Casa di 220 mq con annessa stalla di 40 mq."
    )
    fragments = [c["frammento"].lower() for c in r["componenti"]]
    assert any("220" in f for f in fragments)
    assert any("40" in f for f in fragments)
