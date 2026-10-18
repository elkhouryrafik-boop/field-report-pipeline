"""Shared fixtures for the s5a/s5b/s5c tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review_run"


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A disposable run directory pre-loaded with the 3-cluster fixture."""
    d = tmp_path / "run"
    d.mkdir()
    (d / "logs").mkdir()
    for name in ("clusters.json", "transcripts_en.json"):
        shutil.copy(FIXTURES / name, d / name)
    return d


@pytest.fixture
def clusters(run_dir: Path) -> dict:
    return json.loads((run_dir / "clusters.json").read_text(encoding="utf-8"))


@pytest.fixture
def cfg() -> dict:
    return {"site": "Testville", "site_slug": "testville", "aliases": [],
            "date_from": "2030-01-14", "date_to": "2030-01-14",
            "inspectors": [], "export_txt": "x", "media_dir": "y"}


SAMPLE_DIR = Path(__file__).resolve().parent / "fixtures" / "sample"


@pytest.fixture(scope="module")
def built_run(tmp_path_factory):
    """One full offline s1-s10 run per test module (config path, run dir)."""
    from field_report import run
    base = tmp_path_factory.mktemp("built")
    sample = base / "sample"
    shutil.copytree(SAMPLE_DIR, sample)
    rd = base / "run"
    code = run.main(["--config", str(sample / "config.json"), "--run-dir", str(rd), "--offline", "--auto"])
    assert code == 0
    return sample / "config.json", rd


@pytest.fixture()
def fresh_run(built_run, tmp_path):
    """A private copy of the module's built run, safe to mutate (config path, run dir)."""
    cfg, rd = built_run
    dst = tmp_path / "run"
    shutil.copytree(rd, dst)
    return cfg, dst
