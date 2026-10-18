"""The automated stage chain s1-s6 on the synthetic sample, fully offline."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from field_report import run
from field_report.lib import common

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture()
def sample(tmp_path) -> Path:
    """A private copy of the sample so runs never write next to the committed fixtures."""
    dst = tmp_path / "sample"
    shutil.copytree(FIXTURE, dst)
    return dst / "config.json"


def go(config: Path, run_dir: Path, *extra: str) -> int:
    return run.main(["--config", str(config), "--run-dir", str(run_dir), "--offline", *extra])


def load(run_dir: Path, name: str) -> dict:
    return json.loads((run_dir / f"{name}.json").read_text(encoding="utf-8"))


def test_offline_auto_run_produces_schema_valid_findings_with_five_stops(sample, tmp_path):
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    findings = load(rd, "findings")
    assert common.validate(findings, "findings") == []
    assert len(findings["stops"]) == 5
    assert findings["profile"] == "site-inspection"
    assigned = [a["id"] for s in findings["sectors"] if s["present"] for a in s["assets"]]
    assert len(assigned) == len(set(assigned)) >= 5
    # every present asset cites evidence; every photo is used once
    photos = [p for s in findings["sectors"] for a in s["assets"] for p in a["photos"]]
    assert len(photos) == len(set(photos)) == 12
    for s in findings["sectors"]:
        for a in s["assets"]:
            if s["present"]:
                assert a["source_evidence"]


def test_every_intermediate_artifact_validates(sample, tmp_path):
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    for name in ("messages", "scoped", "media_manifest", "gaps", "transcripts", "transcripts_en", "clusters",
                 "prelabels", "review", "clusters_verified", "findings"):
        payload = load(rd, name)
        schema = "transcripts_en" if name == "transcripts_en_verified" else name
        assert common.validate(payload, schema) == [], name
        assert payload["status"] in ("ok", "partial"), name


def test_auto_accepted_stops_are_marked_unreviewed(sample, tmp_path):
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    review = load(rd, "review")
    assert review["meta"]["auto_accepted"] is True
    assert all(s["human_note"] == "auto-accepted, unreviewed" for s in review["stops"])
    verified = load(rd, "clusters_verified")
    assert not any(c["human_verified"] for c in verified["clusters"])
    findings = load(rd, "findings")
    assert findings["meta"]["human_reviewed"] is False
    assert not any(a.get("human_verified") for s in findings["sectors"] for a in s["assets"])


def test_without_auto_the_chain_stops_at_the_review_gate(sample, tmp_path, capsys):
    rd = tmp_path / "run"
    assert go(sample, rd) == 0
    assert "GATE s5b" in capsys.readouterr().out
    assert not (rd / "review.json").exists() and not (rd / "findings.json").exists()
    manifest = load(rd, "run-manifest")
    assert manifest["gates"]["s5b"] == "open" and "s6" not in manifest["stages"]


def test_run_manifest_records_backends_and_every_stage(sample, tmp_path):
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    m = load(rd, "run-manifest")
    assert m["tool"] == "field-report-pipeline" and m["offline"] is True and m["auto"] is True
    assert m["backends"]["asr"] == "sidecar" and m["backends"]["captions"] == "sidecar"
    assert m["backends"]["classify"] == "keyword"
    for stage in ("s1", "s2", "s3", "s4", "s5", "s5a", "s5b", "s5c", "s6"):
        assert m["stages"][stage]["status"] == "ok", stage
    assert str(tmp_path) not in json.dumps({k: v for k, v in m.items() if k != "run_dir"})


def test_stages_are_idempotent(sample, tmp_path, capsys):
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    before = (rd / "findings.json").read_bytes()
    capsys.readouterr()
    assert go(sample, rd, "--auto") == 0
    assert capsys.readouterr().out.count("SKIP") >= 6
    assert (rd / "findings.json").read_bytes() == before


def test_skipping_the_gate_is_recorded_and_ships_unverified(sample, tmp_path):
    rd = tmp_path / "run"
    assert go(sample, rd, "--skip-gate", "s5b") == 0
    m = load(rd, "run-manifest")
    assert m["gates"]["s5b"]["skipped"] is True and m["stages"]["s5c"]["status"] == "skipped"
    assert common.validate(load(rd, "findings"), "findings") == []


def test_damage_profile_uses_its_scale_and_categories(tmp_path):
    src = EXAMPLES / "sample-damage"
    dst = tmp_path / "dmg"
    shutil.copytree(src, dst)
    cfg = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    assert cfg["profile"] == "damage-assessment"
    rd = tmp_path / "run"
    assert go(dst / "config.json", rd, "--auto") == 0
    findings = load(rd, "findings")
    assert common.validate(findings, "findings") == []
    assert len(findings["stops"]) == 5
    assert [s["sector_name"] for s in findings["sectors"]] == [
        "Structure", "Roof", "Openings", "Services", "Access", "External works"]
    ratings = {a["severity"] for s in findings["sectors"] if s["present"] for a in s["assets"]}
    assert ratings <= {"Destroyed", "Severe", "Moderate", "Minor", "None"}
    roof = next(s for s in findings["sectors"] if s["sector_name"] == "Roof")
    assert roof["sector_severity"] == "Severe"
    quantities = [q for s in findings["sectors"] for a in s["assets"] for q in a["quantities"]]
    assert {"mm", "m", "no"} <= {q["unit"] for q in quantities}


def test_event_log_profile_logs_every_stop_without_classifying(sample, tmp_path):
    cfg = json.loads(sample.read_text(encoding="utf-8"))
    cfg["profile"] = "event-log"
    sample.write_text(json.dumps(cfg), encoding="utf-8")
    rd = tmp_path / "run"
    assert go(sample, rd, "--auto") == 0
    findings = load(rd, "findings")
    assert common.validate(findings, "findings") == []
    assert len(findings["sectors"]) == 1 and findings["overall"]["damage_level"] == "N/A"
    assets = findings["sectors"][0]["assets"]
    assert len(assets) == 5 and {a["severity"] for a in assets} == {"N/A"}


def test_unknown_profile_in_config_exits_2(sample, tmp_path):
    cfg = json.loads(sample.read_text(encoding="utf-8"))
    cfg["profile"] = "no-such-profile"
    sample.write_text(json.dumps(cfg), encoding="utf-8")
    assert go(sample, tmp_path / "run", "--auto") == 2


def test_missing_upstream_exits_5(sample, tmp_path):
    assert run.main(["--config", str(sample), "--run-dir", str(tmp_path / "run"), "--offline",
                     "--only", "s6"]) == 5


def test_a_missing_sidecar_is_a_partial_run_not_a_crash(sample, tmp_path):
    (sample.parent / "export" / "PTT-20300114-WA0003.transcript.json").unlink()
    rd = tmp_path / "run"
    code = go(sample, rd, "--auto")
    assert code == 3
    assert load(rd, "transcripts")["counts"]["failed"] == 1
    assert common.validate(load(rd, "findings"), "findings") == []


def test_config_paths_resolve_relative_to_the_config_file(sample, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = common.load_config(str(sample))
    assert Path(cfg["export_txt"]).is_file() and Path(cfg["media_dir"]).is_dir()
    assert cfg["site_slug"] == "northwind-demo" and cfg["inspectors"] == ["Alex Demo", "Sam Sample"]


def test_config_hash_ignores_where_the_folder_lives(sample, tmp_path):
    other = tmp_path / "elsewhere"
    shutil.copytree(sample.parent, other)
    assert common.config_sha256(common.load_config(str(sample))) == \
        common.config_sha256(common.load_config(str(other / "config.json")))
