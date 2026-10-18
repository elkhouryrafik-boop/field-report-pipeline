"""Report -> review sheet -> filled sheet -> ingest-review -> rebuilt report, on the synthetic sample."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from docx import Document

from field_report import demo, run
from field_report.lib import docx_common as dc
from field_report.lib import docx_controls
from field_report.stages import ingest_confirmations
from field_report.tools import fill_review_sheet

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample"


@pytest.fixture()
def built(tmp_path):
    sample = tmp_path / "sample"
    shutil.copytree(SAMPLE, sample)
    rd = tmp_path / "run"
    code = run.main(["--config", str(sample / "config.json"), "--run-dir", str(rd), "--offline", "--auto"])
    assert code == 0
    return sample / "config.json", rd


def load(rd: Path, name: str) -> dict:
    return json.loads((rd / f"{name}.json").read_text(encoding="utf-8"))


def test_full_chain_passes_the_gate(built):
    _, rd = built
    verification = load(rd, "s10_verification")
    assert verification["status"] == "ok"
    assert (rd / "report.docx").is_file() and (rd / "review-sheet.docx").is_file()


def test_sheet_controls_match_the_sheet_json(built):
    _, rd = built
    tags = set(docx_controls.read_controls(rd / "review-sheet.docx"))
    keys = {i["key"] for i in load(rd, "confirmation_sheet")["items"]}
    assert keys and keys <= tags


def test_filled_sheet_is_read_back_by_ingest(built, tmp_path):
    cfg, rd = built
    sheet = load(rd, "confirmation_sheet")
    qty = next(i["key"] for i in sheet["items"] if i["kind"] == "quantity")
    rating = next(i["key"] for i in sheet["items"] if i["kind"] == "severity")
    before = load(rd, "findings")
    asset_id = rating.split("/")[0]
    current = next(a["severity"] for s in before["sectors"] for a in s["assets"] if a["id"] == asset_id)
    new = next(v for v in ("High", "Medium", "Low") if v != current)

    filled = tmp_path / "filled.docx"
    n = fill_review_sheet.fill(rd / "review-sheet.docx", filled,
                               {qty: "No — see correction", f"{qty}#correction": "4", rating: new},
                               yes_all=True)
    plain = [i for i in sheet["items"] if i["kind"] != "severity"]
    assert n == len(plain) + 2          # every Yes row, the typed correction, the changed rating

    answers = ingest_confirmations.read_answers_from_docx(filled)
    assert answers[qty] == "4"
    assert answers[rating] == new

    code = ingest_confirmations.main(["--config", str(cfg), "--run-dir", str(rd), "--from-docx", str(filled)])
    assert code in (0, 3)          # 3 = some rows (the missing-file gap) need a person
    after = load(rd, "findings")
    aid, idx = qty.split("/qty/")
    asset = next(a for s in after["sectors"] for a in s["assets"] if a["id"] == aid)
    assert asset["quantities"][int(idx)]["value"] == 4 and asset["quantities"][int(idx)]["confirmed"] is True
    changed = next(a for s in after["sectors"] for a in s["assets"] if a["id"] == asset_id)
    assert changed["severity"] == new and changed["human_verified"] is True
    patches = load(rd, "confirmation_patches")
    assert patches["applied"] and any(p["key"] == qty for p in patches["applied"])


def test_rebuilt_report_has_fewer_open_markers(built, tmp_path):
    cfg, rd = built
    markers_before = load(rd, "report_build")["markers"]
    filled = tmp_path / "filled.docx"
    fill_review_sheet.fill(rd / "review-sheet.docx", filled, yes_all=True)
    ingest_confirmations.main(["--config", str(cfg), "--run-dir", str(rd), "--from-docx", str(filled)])
    args = ["--config", str(cfg), "--run-dir", str(rd), "--offline", "--auto", "--only", "s7,s8,s9,s10"]
    assert run.main(args) == 0
    assert markers_before > 0
    assert load(rd, "report_build")["markers"] == 0
    assert load(rd, "s10_verification")["status"] == "ok"


def test_gate_catches_a_tampered_report(built):
    cfg, rd = built
    doc = Document(str(rd / "report.docx"))
    doc.core_properties.author = "someone else"
    dc.save_docx(doc, rd / "report.docx")
    from field_report.stages import s10_verify_build
    assert s10_verify_build.main(["--config", str(cfg), "--run-dir", str(rd)]) == 4
    codes = {p["code"] for p in load(rd, "s10_verification")["errors"]}
    assert "METADATA_NOT_NEUTRAL" in codes


@pytest.mark.parametrize("profile", ["site-inspection", "damage-assessment", "event-log"])
def test_demo_offline_every_profile(tmp_path, profile):
    out = tmp_path / "demo"
    assert demo.main(["--offline", "--profile", profile, "--out", str(out)]) == 0
    names = {p.name for p in out.iterdir()}
    assert {"report.docx", "review-sheet.docx", "run-manifest.json"} <= names
    assert ("quantities.xlsx" in names) == (profile == "damage-assessment")
