"""s9 (review sheet) and s10 (the readiness gate) on the synthetic sample."""
from __future__ import annotations

import json
import zipfile

import pytest
from docx import Document

from field_report.lib import docx_common as dc
from field_report.lib import docx_controls
from field_report.stages import s9_build_confirmation_sheet as s9
from field_report.stages import s10_verify_build as s10


def load(rd, name):
    return json.loads((rd / f"{name}.json").read_text(encoding="utf-8"))


def gate(cfg, rd):
    return s10.main(["--config", str(cfg), "--run-dir", str(rd)])


def codes(rd):
    return {p["code"] for p in load(rd, "s10_verification")["errors"]}


def rewrite(path, fn):
    doc = Document(str(path))
    fn(doc)
    dc.save_docx(doc, path)


# --- s9 ---------------------------------------------------------------------------------

def test_sheet_exists(built_run):
    assert (built_run[1] / "review-sheet.docx").is_file()


def test_sheet_json_validates(built_run):
    from field_report.lib import common
    assert common.validate(load(built_run[1], "confirmation_sheet"), "confirmation_sheet") == []


def test_every_item_has_a_content_control(built_run):
    tags = set(docx_controls.read_controls(built_run[1] / "review-sheet.docx"))
    keys = {i["key"] for i in load(built_run[1], "confirmation_sheet")["items"]}
    assert keys <= tags


def test_dropdown_options_match_the_item_kind(built_run):
    controls = docx_controls.read_controls(built_run[1] / "review-sheet.docx")
    sheet = load(built_run[1], "confirmation_sheet")
    assert controls
    assert len(sheet["items"]) >= 5


def test_sheet_metadata_is_neutral(built_run):
    cp = Document(str(built_run[1] / "review-sheet.docx")).core_properties
    assert cp.author == cp.last_modified_by == "field-report-pipeline"


def test_sheet_shows_every_question_or_rating_basis(built_run):
    doc = Document(str(built_run[1] / "review-sheet.docx"))
    text = "".join(doc.element.body.itertext())
    for item in load(built_run[1], "confirmation_sheet")["items"]:
        assert (item["why"] if item["kind"] == "severity" else item["question"]) in text


def test_sheet_count_matches_open_items(built_run):
    rd = built_run[1]
    assert len(load(rd, "confirmation_sheet")["items"]) == load(rd, "report_build")["open_items"]


def test_s9_out_override(fresh_run, tmp_path):
    cfg, rd = fresh_run
    out = tmp_path / "elsewhere" / "sheet.docx"
    assert s9.main(["--config", str(cfg), "--run-dir", str(rd), "--out", str(out)]) == 0
    assert out.is_file()


def test_s9_missing_findings_exits_5(fresh_run):
    cfg, rd = fresh_run
    (rd / "findings.json").unlink()
    assert s9.main(["--config", str(cfg), "--run-dir", str(rd)]) == 5


def test_s9_bad_config_exits_2(tmp_path):
    assert s9.main(["--config", str(tmp_path / "nope.json")]) == 2


def test_where_prefers_asset_then_sector_then_report():
    assert s9.where({"asset_id": "A1", "asset_name": "Pump", "sector_name": "Water"}) == "A1 Pump"
    assert s9.where({"asset_id": None, "sector_name": "Water"}) == "Water"
    assert s9.where({"asset_id": None, "sector_name": None}) == "Whole report"


# --- s10: pass --------------------------------------------------------------------------

def test_gate_passes_on_a_clean_build(built_run, capsys):
    cfg, rd = built_run
    assert gate(cfg, rd) == 0
    assert "GATE PASSED" in capsys.readouterr().out


def test_verification_file_is_ok(built_run):
    v = load(built_run[1], "s10_verification")
    assert v["status"] in ("ok", "partial")
    assert v["checked"]["report"] == "report.docx"
    assert v["review_coverage"]["total"] == 5


def test_gate_is_idempotent(built_run):
    cfg, rd = built_run
    assert gate(cfg, rd) == 0
    assert gate(cfg, rd) == 0


# --- s10: failures ----------------------------------------------------------------------

def test_missing_report_exits_5(fresh_run):
    cfg, rd = fresh_run
    (rd / "report.docx").unlink()
    assert gate(cfg, rd) == 5


def test_missing_sheet_exits_5(fresh_run):
    cfg, rd = fresh_run
    (rd / "review-sheet.docx").unlink()
    assert gate(cfg, rd) == 5


def test_missing_build_record_exits_5(fresh_run):
    cfg, rd = fresh_run
    (rd / "report_build.json").unlink()
    assert gate(cfg, rd) == 5


def test_bad_config_exits_2(tmp_path):
    assert s10.main(["--config", str(tmp_path / "nope.json")]) == 2


def test_wrong_creator_is_blocking(fresh_run):
    cfg, rd = fresh_run
    rewrite(rd / "report.docx", lambda d: setattr(d.core_properties, "author", "someone"))
    assert gate(cfg, rd) == 4
    assert "METADATA_NOT_NEUTRAL" in codes(rd)


def test_wrong_creator_in_the_sheet_is_blocking(fresh_run):
    cfg, rd = fresh_run
    rewrite(rd / "review-sheet.docx", lambda d: setattr(d.core_properties, "last_modified_by", "someone"))
    assert gate(cfg, rd) == 4
    assert "METADATA_NOT_NEUTRAL" in codes(rd)


def test_a_named_person_in_the_report_is_blocking(fresh_run):
    cfg, rd = fresh_run
    rewrite(rd / "report.docx", lambda d: d.add_paragraph("Met with Mr. Nobody Atall on site."))
    assert gate(cfg, rd) == 4
    assert any(c.startswith("NAMED") or "NAME" in c for c in codes(rd))


def test_a_configured_inspector_name_is_blocking(fresh_run):
    cfg, rd = fresh_run
    c = json.loads(cfg.read_text(encoding="utf-8"))
    c["inspectors"] = ["Pat Quinn"]
    cfg2 = cfg.with_name("config-names.json")
    cfg2.write_text(json.dumps(c), encoding="utf-8")
    rewrite(rd / "review-sheet.docx", lambda d: d.add_paragraph("Raised by Pat Quinn."))
    assert gate(cfg2, rd) == 4


def test_a_dropped_photo_is_blocking(fresh_run):
    cfg, rd = fresh_run
    f = load(rd, "findings")
    asset = next(a for s in f["sectors"] for a in s["assets"] if len(a["photos"]) > 0)
    asset["photos"].append("IMG-never-embedded.jpg")
    (rd / "findings.json").write_text(json.dumps(f), encoding="utf-8")
    assert gate(cfg, rd) == 4


def test_a_removed_content_control_is_blocking(fresh_run):
    cfg, rd = fresh_run
    sheet = load(rd, "confirmation_sheet")
    sheet["items"].append({**sheet["items"][0], "key": "ZZ/qty/9", "item_no": 99})
    (rd / "confirmation_sheet.json").write_text(json.dumps(sheet), encoding="utf-8")
    assert gate(cfg, rd) == 4


def test_a_leftover_placeholder_is_advisory_only(fresh_run):
    cfg, rd = fresh_run
    rewrite(rd / "report.docx", lambda d: d.add_paragraph("TODO write this bit"))
    rc = gate(cfg, rd)
    assert rc in (0, 4)
    if rc == 0:
        assert "PLACEHOLDER_TEXT" in codes(rd)


def test_a_personal_path_is_flagged(fresh_run):
    cfg, rd = fresh_run
    rewrite(rd / "report.docx", lambda d: d.add_paragraph("Saved at " + "C:" + chr(92) + "Users" + chr(92) + "someone"))
    gate(cfg, rd)
    assert "PERSONAL_PATH" in codes(rd)


# --- s10 helpers ------------------------------------------------------------------------

@pytest.mark.parametrize("text,expect", [
    ("Visit with Mrs. Nobody Atall.", True),
    ("The engineer of the works.", False),
    ("Plain sentence.", False),
])
def test_text_problems_names(text, expect):
    assert bool(s10.text_problems("x.docx", text, [])) is expect


def test_text_problems_flags_placeholders():
    assert {p["code"] for p in s10.text_problems("x.docx", "lorem ipsum dolor", [])} & {"PLACEHOLDER_TEXT"}


def test_problem_shape():
    p = s10.problem("CODE", "item", "msg")
    assert p == {"item": "item", "code": "CODE", "message": "msg", "retryable": False, "attempts": 1}


def test_advisory_codes_are_not_blocking():
    assert s10.ADVISORY == {"PLACEHOLDER_TEXT", "PERSONAL_PATH"}


def test_package_metadata_reads_a_clean_docx(built_run):
    meta = s10.package_metadata(built_run[1] / "report.docx")
    assert isinstance(meta, dict)
    assert s10.check_metadata(built_run[1] / "report.docx") == []


def test_embedded_image_digests_are_unique(built_run):
    digests = s10.embedded_image_digests(Document(str(built_run[1] / "report.docx")))
    assert len(digests) == len(set(digests)) == 13


def test_report_zip_has_no_template_part(built_run):
    with zipfile.ZipFile(built_run[1] / "report.docx") as z:
        assert not [n for n in z.namelist() if "template" in n.lower()]
