"""Privacy conventions (no named individuals), shared by the builders and the s10 gate.

Deliberately dependency-free: s8 writes an xlsx and must not be made to import
python-docx just to learn how the field team is named.
"""
from __future__ import annotations

import re

# No individual is ever named in a deliverable. The people who did the work are
# referred to collectively by this phrase; third parties (officials, residents,
# staff of other organisations) are referred to by role only.
FIELD_TEAM = "the field team"

# A title followed by a capitalised given name is how a person's name reaches a
# deliverable in practice — a title plus a name in a General Information
# table, "Eng. …" in a finding. `of` is excluded so "Mayor of the town" and
# "Head of Department" survive.
PERSON_TITLE_RE = re.compile(
    r"\b(?:Mayor|Director|Father|Imam|"
    r"Eng\.|Engineer|Dr\.|Doctor|Mr\.|Mrs\.|Ms\.|Prof\.)\s+"
    r"(?!of\b|the\b|and\b)"
    r"[A-Z][A-Za-z'\u2019-]+(?:\s+(?:[A-Z][A-Za-z'\u2019-]+|el-|al-|El-|Al-)[A-Za-z'\u2019-]*){0,3}"
)


def name_hits(text: str, inspectors: list[str] | None = None) -> list[str]:
    """Every personal name visible in `text`.

    Two independent detectors, because they fail differently: the configured
    surveyor names are exact and certain, the title pattern is a heuristic that
    catches the names nobody thought to configure.
    """
    hits = {m.group(0).strip() for m in PERSON_TITLE_RE.finditer(text)}
    for full in inspectors or []:
        full = full.strip()
        if full and re.search(rf"\b{re.escape(full)}\b", text):
            hits.add(full)
    return sorted(hits)
