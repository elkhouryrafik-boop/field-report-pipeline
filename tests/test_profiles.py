"""Profiles are data files validated by a schema; the code never special-cases a profile name."""
from __future__ import annotations

import copy
import json

import pytest

from field_report.lib import profiles

BUILT_IN = ["damage-assessment", "event-log", "site-inspection"]


def test_built_in_profiles_are_available_and_valid():
    assert set(BUILT_IN) <= set(profiles.available())
    for name in BUILT_IN:
        prof = profiles.load(name)
        assert prof["name"] == name
        assert profiles.validate_profile(prof) == []


def test_default_profile_is_site_inspection():
    assert profiles.profile_of({}) == "site-inspection"
    assert profiles.profile_of({"profile": "event-log"}) == "event-log"


def test_scales_and_categories_match_the_spec():
    site = profiles.load("site-inspection")
    assert site["categories"][0]["name"] == "Structure"
    assert site["scale"]["values"] == ["High", "Medium", "Low"]
    dmg = profiles.load("damage-assessment")
    assert dmg["scale"]["values"] == ["Destroyed", "Severe", "Moderate", "Minor", "None"]
    assert dmg["quantities_sheet"] is True
    assert profiles.load("event-log")["classify"] is False


def test_unknown_profile_is_an_error_with_the_choices():
    with pytest.raises(profiles.ProfileError, match="available"):
        profiles.load("no-such-profile")


@pytest.mark.parametrize("mutate, fragment", [
    (lambda p: p["scale"].update(default="Nope"), "scale.default"),
    (lambda p: p["categories"].append(copy.deepcopy(p["categories"][0])), "unique"),
    (lambda p: p.update(stop_regex="no group at all"), "capture group"),
    (lambda p: p.update(stop_regex="(unclosed"), "regex"),
    (lambda p: p["scale"]["keywords"].update({"Nope": ["x"]}), "scale.keywords"),
    (lambda p: p.pop("font"), "font"),
])
def test_invalid_profiles_are_rejected(mutate, fragment):
    prof = copy.deepcopy(profiles.load("site-inspection"))
    mutate(prof)
    problems = profiles.validate_profile(prof)
    assert problems and any(fragment in p for p in problems), problems


def test_profile_dir_env_adds_user_profiles(tmp_path, monkeypatch):
    mine = copy.deepcopy(profiles.load("event-log"))
    mine["name"] = "my-profile"
    (tmp_path / "my-profile.json").write_text(json.dumps(mine), encoding="utf-8")
    monkeypatch.setenv("FRP_PROFILE_DIR", str(tmp_path))
    profiles._load.cache_clear()
    try:
        assert "my-profile" in profiles.available()
        assert profiles.load("my-profile")["name"] == "my-profile"
    finally:
        profiles._load.cache_clear()


def test_scale_values_always_include_not_applicable():
    for name in BUILT_IN:
        prof = profiles.load(name)
        assert profiles.NOT_APPLICABLE in profiles.scale_values(prof)
        rank = profiles.severity_rank(prof)
        assert rank[profiles.NOT_APPLICABLE] == max(rank.values())
