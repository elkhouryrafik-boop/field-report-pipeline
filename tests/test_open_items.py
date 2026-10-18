"""lib/open_items.py and lib/privacy.py: the single definition of 'open item'."""
from __future__ import annotations

import json

import pytest

from field_report.lib import open_items, privacy, profiles


@pytest.fixture(scope="module")
def findings(built_run):
    _, rd = built_run
    return json.loads((rd / "findings.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gaps(built_run):
    _, rd = built_run
    return json.loads((rd / "gaps.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prof():
    return profiles.load("site-inspection")


@pytest.mark.parametrize("q,expected", [
    ({"unit": "m", "value": 12.0}, "12 m"),
    ({"unit": "m", "value": 12.5}, "12.5 m"),
    ({"unit": "no", "value": 3}, "3 no."),
    ({"unit": "other", "value": 7}, "7"),
    ({"unit": "m2", "value": None, "value_text": "a large area"}, "a large area m2"),
    ({"unit": "other", "value": None, "value_text": None}, "?"),
    ({"unit": "kW", "value": 0}, "0 kW"),
])
def test_quantity_text(q, expected):
    assert open_items.quantity_text(q) == expected


def test_items_are_numbered_from_one_without_gaps(findings, gaps, prof):
    items = open_items.collect(findings, gaps, prof)
    assert [i["item_no"] for i in items] == list(range(1, len(items) + 1))


def test_keys_are_unique(findings, gaps, prof):
    keys = [i["key"] for i in open_items.collect(findings, gaps, prof)]
    assert len(keys) == len(set(keys))


def test_collect_is_deterministic(findings, gaps, prof):
    assert open_items.collect(findings, gaps, prof) == open_items.collect(findings, gaps, prof)


def test_every_item_starts_unanswered(findings, gaps, prof):
    assert all(i["answer"] is None for i in open_items.collect(findings, gaps, prof))


def test_item_kinds_are_known(findings, gaps, prof):
    kinds = {i["kind"] for i in open_items.collect(findings, gaps, prof)}
    assert kinds <= {"quantity", "no_photo", "severity", "gap", "sector_absence"}
    assert "quantity" in kinds


def test_every_gap_becomes_an_item(findings, gaps, prof):
    items = open_items.collect(findings, gaps, prof)
    assert sum(1 for i in items if i["kind"] == "gap") == len(gaps["gaps"])


def test_collect_without_gaps_has_no_gap_items(findings, prof):
    assert not [i for i in open_items.collect(findings, None, prof) if i["kind"] == "gap"]


def test_confirmed_quantities_are_not_open(findings, gaps, prof):
    before = open_items.collect(findings, gaps, prof)
    for s in findings["sectors"]:
        for a in s["assets"]:
            for q in a.get("quantities", []):
                q["confirmed"] = True
    try:
        after = open_items.collect(findings, gaps, prof)
        assert not [i for i in after if i["kind"] == "quantity"]
        assert len(after) < len(before)
        assert open_items.report_markers(findings) == sum(
            1 for s in findings["sectors"] if s.get("present", True)
            for a in s["assets"] if not a.get("photos"))
    finally:
        for s in findings["sectors"]:
            for a in s["assets"]:
                for q in a.get("quantities", []):
                    q["confirmed"] = False


def test_report_markers_match_unconfirmed_quantity_items(findings, gaps, prof):
    items = open_items.collect(findings, gaps, prof)
    n = sum(1 for i in items if i["kind"] in ("quantity", "no_photo"))
    assert open_items.report_markers(findings) == n


def test_severity_items_get_the_scale_as_options(findings, gaps, prof):
    for i in open_items.collect(findings, gaps, prof):
        opts = open_items.options_for(i, prof)
        if i["kind"] == "severity":
            assert opts == open_items.rating_options(prof)
            assert set(prof["scale"]["values"]) <= set(opts)
        else:
            assert opts == open_items.ANSWER_OPTIONS


def test_a_profile_without_classification_has_no_severity_items(findings, gaps):
    prof = profiles.load("event-log")
    assert not prof["classify"]
    assert not [i for i in open_items.collect(findings, gaps, prof) if i["kind"] in ("severity", "sector_absence")]


# --- privacy -------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Spoke to Mr. Nobody Atall about access.",
    "Mr. Example Person checked the slab.",
    "Mrs. Nobody Atall attended.",
    "Mrs. Aziza confirmed.",
])
def test_titles_followed_by_a_name_are_flagged(text):
    assert privacy.name_hits(text)


@pytest.mark.parametrize("text", [
    "Mayor of the town asked for a copy.",
    "The engineer of the works was absent.",
    "Head of Department was informed.",
    "No names here at all.",
    "",
])
def test_roles_without_names_are_not_flagged(text):
    assert privacy.name_hits(text) == []


def test_configured_inspector_names_are_always_flagged():
    assert privacy.name_hits("Visit led by Pat Quinn today.", ["Pat Quinn"]) == ["Pat Quinn"]


def test_blank_inspector_entries_are_ignored():
    assert privacy.name_hits("Visit today.", ["", "  "]) == []


def test_field_team_phrase_is_collective():
    assert privacy.FIELD_TEAM == "the field team"
