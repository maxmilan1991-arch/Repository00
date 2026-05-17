"""Estimate the built surface from an Italian real-estate description.

The structured ``superficie_mq`` field on immobiliare.it is ambiguous for
casali / rustici / bagli: it often reports the *land* surface, not the
*built* one. A buyer evaluating whether a property can host N housing
units needs the latter.

This module extracts that number by scanning the free-text description
with regex + a keyword classifier. It runs offline, in deterministic
time, with no LLM call — slower-but-better extraction is left to a
future LLM pass on the saved ``raw_json``.

Public entry point:

    parse_built_surface(text: str) -> dict

Returned shape::

    {
        "totale_edificato_mq": int | None,
        "componenti": [
            {"tipo": str, "mq": int, "frammento": str},
            ...
        ],
        "note_parsing": str,
    }

Recall target on real ads: ~70-80%. The "Audit parser" sheet in the
xlsx export lets a human spot-check every match.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------- numbers

# Italian thousands separator is ".", English/seldom-Italian is ",". A
# bare integer is fine too. We reject decimals — partial m² values are
# noise in this corpus.
_NUM = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+)"

# Unit token. Matches "mq", "m²", "m^2", "m 2", "metri quadrati", etc.
# Word-boundary on the right (``\b`` after the unit) is the caller's job
# because some units (``m²``) end in a non-word char.
_MQ_UNIT = r"(?:mq\.?|m\s*²|m\s*\^?\s*2|metri\s*quadr(?:i|ati))"

# "approximately"-style hedges.
_CIRCA = r"(?:circa|ca\.?|c\.?ca|~)"


_NUM_WORDS = {
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
}


# Patterns are tried in declaration order; the first matching pattern
# that doesn't overlap an earlier match wins.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "due appartamenti da 60 mq ciascuno" → 2 * 60 = 120. We require
    # an explicit per-unit marker (ciascuno / cadauno / l'uno / each)
    # because phrasings like "Due unità abitative da 90 mq e 60 mq"
    # describe *different* sizes — there the multiplier would inflate
    # the total. Also gated on the middle noun being a recognised
    # built-surface category (so "due piscine da 80 mq ciascuna" is
    # still excluded by the standard outdoor rule).
    ("multiplier", re.compile(
        r"\b(?P<count>due|tre|quattro|cinque|sei|sette|otto|nove|dieci"
        r"|[2-9]|\d{2,})\s+"
        r"(?P<noun>[a-zà-ÿ]+(?:\s+[a-zà-ÿ]+)?)\s+(?:da|di)\s+"
        rf"(?P<n>{_NUM})\s*{_MQ_UNIT}"
        r"\s*(?:ciascun[oa]|cadaun[oa]|cad\.?|l['’]un[oa]|each)\b",
        re.IGNORECASE,
    )),
    # "tra 200 e 250 mq" / "fra 200 ed 250 mq" → median
    ("range_tra", re.compile(
        rf"\b(?:tra|fra)\s+(?P<lo>{_NUM})\s+(?:e|ed)\s+(?P<hi>{_NUM})\s*{_MQ_UNIT}",
        re.IGNORECASE,
    )),
    # "200-250 mq" / "200 / 250 mq" → median
    ("range_dash", re.compile(
        rf"(?<!\w)(?P<lo>{_NUM})\s*[\-–/]\s*(?P<hi>{_NUM})\s*{_MQ_UNIT}",
        re.IGNORECASE,
    )),
    # "oltre 200 mq" / "più di 200 mq" / "almeno 200 mq" → 200 (lower bound)
    ("over", re.compile(
        rf"\b(?:oltre|pi[uù]\s+di|almeno)\s+(?P<n>{_NUM})\s*{_MQ_UNIT}",
        re.IGNORECASE,
    )),
    # "mq 220" / "mq. 220" / "mq circa 220"
    ("unit_then_num", re.compile(
        rf"\b{_MQ_UNIT}\s*(?:{_CIRCA}\s+)?(?P<n>{_NUM})\b",
        re.IGNORECASE,
    )),
    # "circa 220 mq" / "220 mq circa" / "220 mq" / "220mq"
    ("num_then_unit", re.compile(
        rf"(?:{_CIRCA}\s+)?(?P<n>{_NUM})\s*{_MQ_UNIT}(?:\s+{_CIRCA})?",
        re.IGNORECASE,
    )),
]

# --------------------------------------------------------------- categories

CATEGORIES: dict[str, list[str]] = {
    "fabbricato_principale": [
        r"\bmagazzino\s+abitabile\b",  # match before plain "magazzino"
        r"\bunit[àa]\s+abitativ[ae]\b",
        r"\babitazione\s+principale\b",
        r"\bcasa\s+principale\b",
        r"\bcasa\s+colonica\b",
        r"\bcas[ae]\b",
        r"\babitazion[ei]\b",
        r"\bvill[ae]\b",
        r"\bpalazz(?:o|in[ae])\b",
        r"\bdimor[ae]\b",
        r"\bedific(?:io|i)\b",
        r"\bimmobil[ei]\b",
        r"\bfabbricat[io]\b",
        r"\bcostruzion[ei]\b",
        r"\bmasseri[ae]\b",
        r"\bcasal[ei]\b",
        r"\bbagl(?:io|i)\b",
        r"\btrull[io]\b",
        r"\brustic[io]\b",
        r"\bcascin[ae]\b",
        r"\bcasolar[ei]\b",
        r"\bpoder[ei]\b",
        r"\bappartament[io]\b",
    ],
    "rudere": [
        r"\bruder[ei]\b",
        r"\bdirupo\b",
        r"\bcasa\s+diruta\b",
        r"\bimmobile\s+diruto\b",
        r"\bstruttura\s+dirupata\b",
        r"\bfabbricato\s+collabente\b",
        r"\bfabbricato\s+f\s*/?\s*2\b",
        r"\bstruttura\s+da\s+ricostruire\b",
        r"\bda\s+ricostruire\b",
        r"\bex\s+stalla\s+in\s+disuso\b",
        r"\bvecchia\s+casa\s+colonica\b",
        r"\bantico\s+fabbricato\b",
        r"\bdirupat[oi]\b",
        r"\bdirut[oi]\b",
        r"\bcollabent[ei]\b",
    ],
    "dependance": [
        r"\bd[eé]pendance\b",
        r"\bdependenza\b",
        r"\bforesteri[ae]\b",
        r"\bospitalit[àa]\b",
        r"\bguest\s+house\b",
        r"\bcasa\s+per\s+gli\s+ospiti\b",
        r"\bannesso\s+abitativo\b",
        r"\bsecondaria\s+unit[àa]\s+abitativa\b",
        r"\babitazione\s+secondaria\b",
    ],
    "annesso": [
        r"\banness[oi]\b",
        r"\bannesso\s+agricolo\b",
        r"\bstall[ae]\b",
        r"\bscuderi[ae]\b",
        r"\bfienil[ei]\b",
        r"\bmagazzin[oi]\b",
        r"\bdepositi?\b",
        r"\bripostigli[oi]?\b",
        r"\blocale\s+tecnico\b",
        r"\bgarage\b",
        r"\bautorimess[ae]\b",
        r"\bbox\b",
        r"\btettoia\s+chiusa\b",
        r"\bcapann[oi]\b",
        r"\bpollaio\s+in\s+muratura\b",
    ],
}

# Surfaces attached to these terms refer to land or uncovered outdoor
# areas and must NOT enter the built-surface total.
_EXCLUDED = [
    r"\bterrazz[oa]\b", r"\bterrazz[ei]\b", r"\bbalcon[ei]\b", r"\bloggia\b",
    r"\bveranda\s+scoperta\b", r"\bgiardin[oi]\b", r"\bparc[oi]\b",
    r"\bterren[oi]\b", r"\blott[oi]\b", r"\bfond[oi]\b",
    r"\bagrumet[oi]\b", r"\boliveto\b", r"\bvigneto\b", r"\bseminativ[oi]\b",
    r"\bpiscin[ae]\b", r"\bcortile\s+esterno\b", r"\bparcheggio\s+scoperto\b",
    r"\barea\s+pertinenziale\b", r"\bsuperficie\s+scoperta\b",
    r"\bpertinenze\s+esterne\b", r"\barea\s+esterna\b",
]

# Surfaces attached to these terms refer to potential (cubatura, future
# building rights) and don't represent existing built surface.
_POTENTIAL = [
    r"\bedificabili?t[àa]\b",
    r"\bedificabil[ei]\b",
    r"\bedificare\b",
    r"\bcubatura\b",
    r"\bvolumetria\s+edificabile\b",
    r"\bcubatura\s+concessa\b",
    r"\bpotenziale\s+edific",
]

# When one of these markers shows up with a single number, that number
# is the authoritative total — we don't sum components, to avoid double
# counting ("superficie lorda costruita: 380 mq, di cui casa 220 mq…").
_EXPLICIT_TOTAL_MARKERS = [
    r"superficie\s+lorda\s+(?:complessiva\s+)?(?:costruita|coperta|edificata)",
    r"totale\s+superficie\s+(?:coperta|costruita|edificata)",
    r"superficie\s+(?:complessiva\s+)?(?:edificata|costruita|coperta)",
    r"totale\s+(?:costruito|edificato|coperto)",
    r"complessivi(?:vamente)?\s+(?:costruiti|edificati|coperti)",
]

_EXPLICIT_TOTAL_RE = re.compile(
    rf"(?P<marker>(?:{'|'.join(_EXPLICIT_TOTAL_MARKERS)}))"
    rf"\s*[:\-]?\s*(?:di\s+)?(?:{_CIRCA}\s+)?"
    rf"(?:(?P<n1>{_NUM})\s*{_MQ_UNIT}|{_MQ_UNIT}\s*(?P<n2>{_NUM}))",
    re.IGNORECASE,
)

# Threshold above which the result probably deserves a human eyeball.
# 1500 m² is already a sizeable compound; very few residential listings
# legitimately exceed it without it being a luxury property.
_HIGH_VALUE_THRESHOLD = 1500


def parse_built_surface(text: str) -> dict[str, Any]:
    """Estimate total built surface from an Italian listing description.

    See module docstring for the output schema.
    """
    if not text or not isinstance(text, str):
        return _empty_result()

    txt = re.sub(r"\s+", " ", text).strip()
    if not txt:
        return _empty_result()

    # (1) Explicit total wins outright.
    explicit = _find_explicit_total(txt)
    if explicit is not None:
        notes = []
        if explicit["mq"] > _HIGH_VALUE_THRESHOLD:
            notes.append("valore eccezionalmente alto, verificare")
        return {
            "totale_edificato_mq": explicit["mq"],
            "componenti": [{
                "tipo": "totale_esplicito",
                "mq": explicit["mq"],
                "frammento": explicit["frammento"],
            }],
            "note_parsing": "; ".join(notes),
        }

    # (2) Collect every surface mention.
    mentions = _collect_surface_mentions(txt)
    if not mentions:
        return _empty_result()

    # (3) Classify each mention by the closest keyword. We bound the
    # scan window so a mention can't borrow the keyword that "belongs"
    # to a neighbouring mention.
    components: list[dict[str, Any]] = []
    for i, m in enumerate(mentions):
        prev_end = mentions[i - 1]["end"] if i > 0 else 0
        next_start = mentions[i + 1]["start"] if i + 1 < len(mentions) else None
        category = _classify(
            txt, m["start"], m["end"],
            prev_end=prev_end, next_start=next_start,
        )
        if category in CATEGORIES:
            components.append({
                "tipo": category,
                "mq": m["mq"],
                "frammento": m["frammento"],
            })
        # Otherwise: excluded / potential / unclassified — drop silently.

    if not components:
        return _empty_result()

    total = sum(c["mq"] for c in components)
    notes: list[str] = []
    if total > _HIGH_VALUE_THRESHOLD:
        notes.append("valore eccezionalmente alto, verificare")
    return {
        "totale_edificato_mq": total,
        "componenti": components,
        "note_parsing": "; ".join(notes),
    }


# --------------------------------------------------------------- internals

def _noun_is_built_category(noun: str) -> bool:
    """True if the (possibly multi-word) noun matches a built-surface kw.

    Used by the "due appartamenti da 60 mq" branch to decide whether the
    multiplier should fire. We require the keyword to span the whole
    noun, not just appear inside it, to avoid false multipliers on
    phrases like "due piscine".
    """
    for kws in CATEGORIES.values():
        for kw in kws:
            if re.fullmatch(kw, noun, re.IGNORECASE):
                return True
    return False


def _empty_result() -> dict[str, Any]:
    return {
        "totale_edificato_mq": None,
        "componenti": [],
        "note_parsing": "nessuna superficie costruita identificabile nel testo",
    }


def _to_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    cleaned = raw.replace(".", "").replace(",", "")
    try:
        return int(cleaned)
    except (TypeError, ValueError):
        return None


def _find_explicit_total(txt: str) -> dict[str, Any] | None:
    m = _EXPLICIT_TOTAL_RE.search(txt)
    if m is None:
        return None
    raw = m.group("n1") or m.group("n2")
    mq = _to_int(raw)
    if mq is None or mq < 5:
        return None
    return {"mq": mq, "frammento": m.group(0)}


def _collect_surface_mentions(txt: str) -> list[dict[str, Any]]:
    """Find every "<number> mq" mention without overlapping the others."""
    used = [False] * (len(txt) + 1)
    results: list[dict[str, Any]] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(txt):
            s, e = m.start(), m.end()
            if any(used[s:e]):
                continue
            if kind in ("range_tra", "range_dash"):
                lo = _to_int(m.group("lo"))
                hi = _to_int(m.group("hi"))
                if lo is None or hi is None:
                    continue
                mq = (lo + hi) // 2
            elif kind == "multiplier":
                # Only multiply when the noun is a recognised built-surface
                # category — otherwise we'd inflate things like "due
                # piscine da 80 mq" (excluded) or "due lotti di 5000 mq".
                noun = m.group("noun").lower()
                if not _noun_is_built_category(noun):
                    continue  # let later patterns pick the plain number
                count_raw = m.group("count").lower()
                count = _NUM_WORDS.get(count_raw) or _to_int(count_raw)
                per = _to_int(m.group("n"))
                if count is None or per is None or count < 2 or per < 5:
                    continue
                mq = count * per
            else:
                mq = _to_int(m.group("n"))
            if mq is None or mq < 5:
                # Likely a stray digit / "5 mq" toilets etc.
                continue
            for i in range(s, e):
                used[i] = True
            results.append({
                "mq": mq,
                "start": s,
                "end": e,
                "frammento": txt[s:e],
            })
    results.sort(key=lambda r: r["start"])
    return results


def _classify(
    txt: str, start: int, end: int,
    *,
    prev_end: int = 0,
    next_start: int | None = None,
) -> str | None:
    """Return the closest matching category for the mention at [start:end].

    Strategy: scan an 80-char window before the number and a 30-char
    window after, with two constraints to avoid borrowing the keyword
    that belongs to a neighbouring mention:

    * The *after*-window is clipped to ``next_start`` (the start of the
      next surface mention) and truncated at the first period — a new
      sentence usually introduces a new structure.
    * After-matches receive a constant proximity penalty, so a keyword
      *before* the number wins over a keyword equally close *after*.

    The *before*-window is left full-width on purpose: descriptions
    like "Due unità abitative da 90 mq e 60 mq" share the subject
    across both numbers and the second mention needs to see it.

    Excluded / potential keywords act as vetoes: if one is closer to
    the number than any built-surface keyword, the mention is dropped.

    ``prev_end`` is currently unused by the classifier — kept in the
    signature so a future tightening can re-introduce a lower bound
    without rewiring the call site.
    """
    del prev_end  # see docstring

    before = txt[max(0, start - 80):start]

    upper_after = end + 30
    if next_start is not None:
        upper_after = min(upper_after, next_start)
    after = txt[end:upper_after]
    # New sentence after the number describes something else.
    period_pos = after.find(".")
    if period_pos >= 0:
        after = after[:period_pos]

    candidates: list[tuple[int, str]] = []
    _add_matches(before, candidates, before_window=True)
    _add_matches(after, candidates, before_window=False)

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][1]


def _add_matches(window: str, out: list[tuple[int, str]], *, before_window: bool) -> None:
    """Append (distance, category) tuples for every kw found in *window*.

    Distance is measured from the surface number; the closer the
    keyword, the smaller the distance. After-window matches receive a
    small constant penalty so a "casa" appearing 5 chars *before* the
    number wins over a "casa" 5 chars *after*.
    """
    after_penalty = 0 if before_window else 30

    def emit(patterns: list[str], label: str) -> None:
        for kw in patterns:
            for m in re.finditer(kw, window, re.IGNORECASE):
                if before_window:
                    distance = len(window) - m.end()
                else:
                    distance = m.start()
                out.append((distance + after_penalty, label))

    for cat, kws in CATEGORIES.items():
        emit(kws, cat)
    emit(_EXCLUDED, "__excluded__")
    emit(_POTENTIAL, "__potential__")
