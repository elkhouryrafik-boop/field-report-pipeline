"""Classification backends: which category a piece of evidence belongs to, and how bad it is.

The `keyword` backend is deterministic and driven entirely by the profile
(`categories[].keywords` weights and `scale`), so it runs offline and the same input always
gives the same answer. The experimental `anthropic` backend implements the same
`classify(unit)` interface.

A unit is one thing the inspector said and showed: a voice note with the photos it refers
to, or a photo on its own. Nothing is invented: when no category scores, the unit comes back
with `category_id=None` and the stage lists it as unclassified rather than guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from field_report.backends import BackendUnavailable
from field_report.lib import profiles

# A hinted category (from a caption) counts as this many keyword points: enough to break a tie
# or place a note with no keyword, not enough to outvote a clear keyword in the text.
HINT_WEIGHT = 2


@dataclass
class Unit:
    cluster_id: str
    text: str = ""                                   # English transcript text
    captions: list[str] = field(default_factory=list)
    category_hints: list[str] = field(default_factory=list)   # category names
    rating_hints: list[str] = field(default_factory=list)     # scale values


@dataclass
class Classification:
    category_id: int | None
    rating: str
    confidence: float
    basis: str


def _term_re(term: str) -> re.Pattern:
    parts = [re.escape(p) for p in term.strip().split()]
    return re.compile(r"(?<![A-Za-z])" + r"\s+".join(parts) + r"(?:e?s)?(?![A-Za-z])", re.IGNORECASE)


class KeywordClassifier:
    name = "keyword"

    def __init__(self, profile: dict) -> None:
        self.profile = profile
        self._cat_terms = {
            c["id"]: [(t, w, _term_re(t)) for t, w in c["keywords"].items()]
            for c in profile["categories"]}
        self._names = {c["name"].lower(): c["id"] for c in profile["categories"]}
        self._names.update({c["short"].lower(): c["id"] for c in profile["categories"]})
        scale = profile["scale"]
        self._values = list(scale["values"])
        self._canon = {v.lower(): v for v in self._values}
        self._explicit = re.compile(scale["explicit_regex"]) if scale.get("explicit_regex") else None
        self._scale_kw = {v: [_term_re(t) for t in terms] for v, terms in scale["keywords"].items()}

    def info(self) -> dict:
        return {"model": "keyword-rules", "prompt_version": "n/a"}

    # -- category -------------------------------------------------------
    def _category(self, unit: Unit) -> tuple[int | None, str]:
        if not self.profile["classify"]:
            only = self.profile["categories"][0]["id"]
            return only, "profile does not classify; single category"
        scores = {cid: 0 for cid in self._cat_terms}
        found: dict[int, list[str]] = {cid: [] for cid in self._cat_terms}
        for cid, terms in self._cat_terms.items():
            for text in [unit.text, *unit.captions]:
                for term, weight, rx in terms:
                    n = len(rx.findall(text))
                    if n:
                        scores[cid] += weight * min(n, 2)
                        if term not in found[cid]:
                            found[cid].append(term)
        for hint in unit.category_hints:
            cid = self._names.get((hint or "").lower())
            if cid is not None:
                scores[cid] += HINT_WEIGHT
                found[cid].append(f"caption:{hint}")
        best = max(scores.values())
        if best <= 0:
            return None, "no keyword or caption hint matched"
        winners = [cid for cid, sc in scores.items() if sc == best]
        cid = min(winners)
        return cid, "matched " + ", ".join(found[cid][:6])

    # -- rating ---------------------------------------------------------
    def _rating(self, unit: Unit) -> tuple[str, str]:
        if self._explicit:
            m = self._explicit.search(unit.text)
            if m and m.group(1).lower() in self._canon:
                return self._canon[m.group(1).lower()], "stated by the speaker"
        for value in self._values:               # most severe first
            for rx in self._scale_kw.get(value, []):
                if rx.search(unit.text) or any(rx.search(c) for c in unit.captions):
                    return value, f"keyword for {value}"
        hints = [self._canon[h.lower()] for h in unit.rating_hints if h and h.lower() in self._canon]
        if hints:
            return min(hints, key=self._values.index), "caption hint"
        return self.profile["scale"]["default"], "scale default"

    def classify(self, unit: Unit) -> Classification:
        cid, why_cat = self._category(unit)
        rating, why_rate = self._rating(unit)
        if not self.profile["classify"]:
            rating = profiles.NOT_APPLICABLE
        confidence = 0.0 if cid is None else (0.9 if why_rate == "stated by the speaker" else 0.7)
        return Classification(cid, rating, confidence, f"{why_cat}; rating: {why_rate}")


def get_classifier(name: str, profile: dict, **kwargs):
    if name == "keyword":
        return KeywordClassifier(profile)
    if name == "anthropic":
        try:
            from field_report.backends import anthropic_backend
        except ImportError as e:
            raise BackendUnavailable(
                "the experimental anthropic backend is not available in this build "
                "(pip install field-report-pipeline[anthropic])") from e
        return anthropic_backend.get_classifier(profile, **kwargs)
    raise BackendUnavailable(f"unknown classify backend {name!r}; choose keyword or anthropic")
