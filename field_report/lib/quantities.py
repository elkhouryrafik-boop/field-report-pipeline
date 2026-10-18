"""Spoken-quantity helpers: number words, source-checking, and a rule-based extractor.

Dependency-free so the backends, the s4 stage and the tests can share it.
A quantity is only ever reported when its number is actually in the source text
(digits or number words); nothing is derived, rounded up or invented.
"""
from __future__ import annotations

import re

UNIT_ENUM = ("m", "km", "m2", "m3", "m3/h", "no", "lm", "kVA", "kW", "ton", "%", "mm", "unit", "other")

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000}
NUMBER_WORDS = {**_UNITS, **_TENS, **_SCALES, "half": 0.5}
_TOKEN_RE = re.compile(r"[a-z]+", re.IGNORECASE)


def spoken_number_values(text: str) -> set[float]:
    """Every number the text spells out in words, including compounds.

    "one hundred and fifty" -> {1, 100, 50, 150}; "twenty five" -> {20, 5, 25}.
    """
    values: set[float] = set()
    run: list[float] = []

    def flush() -> None:
        if not run:
            return
        parts = list(run)
        k = 1
        while k < len(parts):
            if parts[k] in (100, 1000) and 1 <= parts[k - 1] <= 99:
                parts[k - 1:k + 1] = [parts[k - 1] * parts[k]]
            else:
                k += 1
        values.update(run)
        values.update(parts)
        values.add(sum(parts))
        run.clear()

    for tok in _TOKEN_RE.findall(text.lower()):
        if tok == "and" and run:
            continue
        if tok in NUMBER_WORDS:
            run.append(NUMBER_WORDS[tok])
            continue
        flush()
    flush()
    return values


def _digits_present(value, text: str) -> bool:
    if float(value) == int(value):
        forms = [str(int(value)), f"{int(value):,}"]
    else:
        forms = [str(value), str(value).replace(".", ",")]
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", text) for f in forms)


def number_in_text(value, src_text: str) -> bool:
    """Is this number actually in the source transcript (digits or spoken words)?"""
    return _digits_present(value, src_text) or value in spoken_number_values(src_text)


# unit phrase -> enum. Longest phrases first so "square metres" beats "metres".
_UNIT_PHRASES: list[tuple[str, str]] = [
    (r"square\s+met(?:re|er)s?|sq\.?\s*m|m2|m²", "m2"),
    (r"cubic\s+met(?:re|er)s?|m3|m³", "m3"),
    (r"kilomet(?:re|er)s?|km", "km"),
    (r"millimet(?:re|er)s?|mm", "mm"),
    (r"met(?:re|er)s?|m", "m"),
    (r"per\s*cent|percent|%", "%"),
    (r"tonnes?|tons?", "ton"),
    (r"sheets?|panels?|pieces?|items?|units?|bulbs?|doors?|windows?", "no"),
]
_NUM_WORD = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_NUM_RE = rf"(?:\d+(?:[.,]\d+)?|(?:(?:{_NUM_WORD})(?:\s+(?:and\s+)?(?:{_NUM_WORD}))*))"
_QUALIFIER_RE = r"(?P<q>about|around|approximately|roughly|nearly|almost|under|over|more than|less than|at least)?\s*"
_UNIT_ALT = "|".join(f"(?P<u{i}>{p})" for i, (p, _) in enumerate(_UNIT_PHRASES))
_QTY_RE = re.compile(
    rf"(?<![\w.])(?:{_QUALIFIER_RE})(?P<num>{_NUM_RE})\s*(?:-\s*)?(?:{_UNIT_ALT})(?![A-Za-z0-9])",
    re.IGNORECASE)


def _value_of(num: str) -> float | None:
    num = num.strip().lower()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", num):
        return float(num.replace(",", "."))
    vals = spoken_number_values(num)
    if not vals:
        return None
    parts = [NUMBER_WORDS[t] for t in _TOKEN_RE.findall(num) if t in NUMBER_WORDS]
    k = 1
    while k < len(parts):
        if parts[k] in (100, 1000) and 1 <= parts[k - 1] <= 99:
            parts[k - 1:k + 1] = [parts[k - 1] * parts[k]]
        else:
            k += 1
    return float(sum(parts))


def extract_quantities(text: str) -> list[dict]:
    """Quantities spoken in `text`: [{unit,value,value_text,confirmed,verbatim,note}].

    `confirmed` is False when the speaker hedged ("about", "under", ...); the
    report then marks the figure approximate rather than firm.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for m in _QTY_RE.finditer(text or ""):
        unit = None
        for i, (_, enum) in enumerate(_UNIT_PHRASES):
            if m.group(f"u{i}"):
                unit = enum
                break
        value = _value_of(m.group("num"))
        if unit is None or value is None or not number_in_text(
                int(value) if value == int(value) else value, text):
            continue
        if value == int(value):
            value = int(value)
        qualifier = (m.group("q") or "").strip().lower() or None
        key = (unit, value, m.start())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "unit": unit, "value": float(value), "value_text": None,
            "confirmed": qualifier is None,
            "verbatim": m.group(0).strip(),
            "note": f"stated as '{qualifier}'" if qualifier else None,
        })
    return out
