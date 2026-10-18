"""Report profiles: what one run assesses, how stops are classified, and the wording used.

A profile is a data file, `field_report/profiles/<name>.json`, validated against
`schemas/profile.schema.json`. `config.profile` names one of them. To add your own,
drop a JSON file next to the built-in ones (or point `FRP_PROFILE_DIR` at a folder);
nothing in the code changes. See docs/PROFILES.md.

Built in:
  site-inspection     default; disciplines + High/Medium/Low priority
  damage-assessment   elements + None..Destroyed scale; a quantities sheet
  event-log           no classification; chronological stops
"""
from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path

import jsonschema

PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "profile.schema.json"
DEFAULT_PROFILE = "site-inspection"
# Sentinel rating for categories nothing was recorded in; valid on every scale.
NOT_APPLICABLE = "N/A"


class ProfileError(Exception):
    """Unknown or invalid profile (callers exit 2)."""


def _search_dirs() -> list[Path]:
    dirs = [PROFILE_DIR]
    extra = os.environ.get("FRP_PROFILE_DIR")
    if extra:
        dirs.insert(0, Path(extra))
    return dirs


def available() -> list[str]:
    names: set[str] = set()
    for d in _search_dirs():
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.json"))
    return sorted(names)


def validate_profile(data: dict) -> list[str]:
    """Schema problems plus the cross-field rules a schema cannot express."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    problems = [f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
                for e in v.iter_errors(data)]
    if problems:
        return problems
    ids = [c["id"] for c in data["categories"]]
    if len(set(ids)) != len(ids):
        problems.append("categories: ids must be unique")
    names = [c["name"] for c in data["categories"]]
    if len(set(names)) != len(names):
        problems.append("categories: names must be unique")
    scale = data["scale"]
    if scale["default"] not in scale["values"]:
        problems.append(f"scale.default {scale['default']!r} is not in scale.values")
    for value in scale["keywords"]:
        if value not in scale["values"]:
            problems.append(f"scale.keywords: {value!r} is not in scale.values")
    for key in ("stop_regex", "explicit_regex"):
        pattern = data["stop_regex"] if key == "stop_regex" else scale.get(key)
        if pattern is None:
            continue
        try:
            rx = re.compile(pattern)
        except re.error as e:
            problems.append(f"{key}: not a valid regex ({e})")
            continue
        if rx.groups != 1:
            problems.append(f"{key}: needs exactly one capture group, has {rx.groups}")
    if data["classify"] and not any(c["keywords"] for c in data["categories"]):
        problems.append("classify is true but no category has keywords")
    return problems


@cache
def _load(name: str) -> dict:
    for d in _search_dirs():
        p = d / f"{name}.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ProfileError(f"profile {name!r} is not valid JSON: {e}") from e
            problems = validate_profile(data)
            if problems:
                raise ProfileError(f"profile {name!r} is invalid: {problems[:4]}")
            if data["name"] != name:
                raise ProfileError(f"profile file {p.name} declares name {data['name']!r}")
            return data
    raise ProfileError(f"unknown profile {name!r}; available: {', '.join(available())}")


def load(name: str) -> dict:
    """The validated profile dict (shared, treat as read-only)."""
    return _load(name)


def profile_of(cfg: dict) -> str:
    return (cfg or {}).get("profile") or DEFAULT_PROFILE


def profile(cfg: dict) -> dict:
    return load(profile_of(cfg))


def categories_for(cfg: dict) -> dict[int, dict]:
    return {c["id"]: c for c in profile(cfg)["categories"]}


def scale_values(prof: dict) -> list[str]:
    """Legal ratings for an asset: the scale plus the N/A sentinel."""
    vals = list(prof["scale"]["values"])
    if NOT_APPLICABLE not in vals:
        vals.append(NOT_APPLICABLE)
    return vals


def severity_rank(prof: dict) -> dict[str, int]:
    """Lower = more severe. N/A sorts last."""
    return {v: i for i, v in enumerate(scale_values(prof))}


def locality_line(cfg: dict) -> str:
    """The cover's site line."""
    return cfg["site"]
