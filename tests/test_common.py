"""Acceptance tests for lib/common.py.

Run: python -m pytest tests/test_common.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_report.lib import common

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# (a) WA_HEADER_RE — must match both the real-data U+202F NNBSP separator and
#     a plain U+0020 space separator before am/pm.
# ---------------------------------------------------------------------------

def test_wa_header_re_matches_u202f_separator():
    line = "14/01/2030, 9:37\u202fam - Alex Demo: test message"
    m = common.WA_HEADER_RE.match(line)
    assert m is not None
    assert m.group("d") == "14/01/2030"
    assert m.group("h") == "9"
    assert m.group("mi") == "37"
    assert m.group("ap") == "am"
    assert m.group("body") == "Alex Demo: test message"


def test_wa_header_re_matches_u0020_separator():
    line = "14/01/2030, 9:37 am - Alex Demo: test message"
    m = common.WA_HEADER_RE.match(line)
    assert m is not None
    assert m.group("body") == "Alex Demo: test message"


def test_wa_header_re_rejects_non_header_line():
    assert common.WA_HEADER_RE.match("just a continuation line") is None


# ---------------------------------------------------------------------------
# (b) finalize — n_in=5, n_out=0 -> status=failed, EMPTY_RESULT_WITH_INPUTS, exit 4
# ---------------------------------------------------------------------------

def test_finalize_empty_result_with_inputs():
    payload = {"errors": []}
    code = common.finalize(payload, n_in=5, n_out=0, item_errors=[])
    assert code == 4
    assert payload["status"] == "failed"
    codes = [e["code"] for e in payload["errors"]]
    assert "EMPTY_RESULT_WITH_INPUTS" in codes


def test_finalize_empty_ok_exempts_gaps():
    # gaps.json is the sole legitimate exemption: n_in>0, n_out=0, empty_ok=True -> ok
    payload = {"errors": []}
    code = common.finalize(payload, n_in=5, n_out=0, item_errors=[], empty_ok=True)
    assert code == 0
    assert payload["status"] == "ok"


def test_finalize_all_ok_no_errors():
    payload = {"errors": []}
    code = common.finalize(payload, n_in=5, n_out=5, item_errors=[])
    assert code == 0
    assert payload["status"] == "ok"


def test_finalize_partial_with_item_errors():
    payload = {"errors": []}
    item_err = common.err("M00001", "TRANSCRIBE_FAILED", "boom", retryable=True, attempts=3)
    code = common.finalize(payload, n_in=5, n_out=4, item_errors=[item_err])
    assert code == 3
    assert payload["status"] == "partial"
    assert item_err in payload["errors"]


def test_finalize_zero_input_zero_output_is_not_empty_result_violation():
    # n_in == 0 -> the EMPTY_RESULT_WITH_INPUTS rule does not apply (n_in > 0 required)
    payload = {"errors": []}
    code = common.finalize(payload, n_in=0, n_out=0, item_errors=[])
    assert code == 0
    assert payload["status"] == "ok"


# ---------------------------------------------------------------------------
# (c) retry — an always-failing fn is called exactly 3 times (1 + 2 retries)
# ---------------------------------------------------------------------------

def test_retry_calls_always_failing_fn_exactly_three_times():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("nope")

    result, error = common.retry(always_fails, "item-1")
    assert calls["n"] == 3
    assert result is None
    assert error is not None
    assert error["item"] == "item-1"
    assert error["attempts"] == 3


def test_retry_succeeds_on_first_try_without_extra_calls():
    calls = {"n": 0}

    def always_succeeds():
        calls["n"] += 1
        return "ok-value"

    result, error = common.retry(always_succeeds, "item-2")
    assert calls["n"] == 1
    assert result == "ok-value"
    assert error is None


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def fails_twice_then_succeeds():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "recovered"

    result, error = common.retry(fails_twice_then_succeeds, "item-3")
    assert calls["n"] == 3
    assert result == "recovered"
    assert error is None


def test_retry_respects_custom_cap():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("nope")

    result, error = common.retry(always_fails, "item-4", cap=0)
    assert calls["n"] == 1
    assert result is None
    assert error["attempts"] == 1


# ---------------------------------------------------------------------------
# (d) write_stage — leaves no .tmp behind; rejects a payload missing "status"
# ---------------------------------------------------------------------------

def _valid_gaps_payload(cfg: dict) -> dict:
    return {
        "status": "ok",
        "errors": [],
        "meta": common.envelope("s2_scope", cfg["site"], cfg, {"a": 1}, {"a": 1})["meta"],
        "gaps": [],
        "counts": {
            "total": 0,
            "media_omitted": 0,
            "file_missing_on_disk": 0,
            "unparsable_filename": 0,
            "unreadable_file": 0,
        },
    }


@pytest.fixture()
def sample_cfg(tmp_path):
    export_txt = tmp_path / "export.txt"
    export_txt.write_text("dummy", encoding="utf-8")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    return {
        "site": "Testville",
        "site_slug": "testville",
        "date_from": "2030-01-01",
        "date_to": "2030-01-02",
        "inspectors": ["Someone"],
        "export_txt": str(export_txt),
        "media_dir": str(media_dir),
    }


def test_write_stage_writes_valid_payload_and_leaves_no_tmp(tmp_path, sample_cfg):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = _valid_gaps_payload(sample_cfg)
    common.write_stage(run_dir, "gaps", payload, "gaps")

    out = run_dir / "gaps.json"
    tmp = run_dir / "gaps.json.tmp"
    assert out.is_file()
    assert not tmp.exists()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"


def test_write_stage_rejects_payload_missing_status(tmp_path, sample_cfg):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = _valid_gaps_payload(sample_cfg)
    del payload["status"]

    with pytest.raises(ValueError):
        common.write_stage(run_dir, "gaps", payload, "gaps")

    out = run_dir / "gaps.json"
    tmp = run_dir / "gaps.json.tmp"
    assert not out.exists()
    assert not tmp.exists()


def test_write_stage_no_orphaned_tmp_after_repeated_failed_writes(tmp_path, sample_cfg):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = _valid_gaps_payload(sample_cfg)
    del payload["status"]
    for _ in range(3):
        with pytest.raises(ValueError):
            common.write_stage(run_dir, "gaps", payload, "gaps")
    assert list(run_dir.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Additional coverage: config loading, sha256 determinism, slugify, ts parsing,
# read_stage upstream-missing/failed handling, get_logger, err().
# ---------------------------------------------------------------------------

def test_load_config_valid(tmp_path, sample_cfg):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(sample_cfg), encoding="utf-8")
    cfg = common.load_config(str(cfg_path))
    assert cfg["site"] == "Testville"
    assert cfg["site_slug"] == "testville"


def test_load_config_missing_file_raises_config_error(tmp_path):
    with pytest.raises(common.ConfigError):
        common.load_config(str(tmp_path / "does_not_exist.json"))


def test_load_config_bad_json_raises_config_error(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(common.ConfigError):
        common.load_config(str(p))


def test_load_config_rejects_unknown_keys(tmp_path, sample_cfg):
    sample_cfg["not_a_real_key"] = "oops"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(sample_cfg), encoding="utf-8")
    with pytest.raises(common.ConfigError):
        common.load_config(str(p))


def test_load_config_rejects_nonexistent_export_txt(tmp_path, sample_cfg):
    sample_cfg["export_txt"] = str(tmp_path / "nope.txt")
    p = tmp_path / "config.json"
    p.write_text(json.dumps(sample_cfg), encoding="utf-8")
    with pytest.raises(common.ConfigError):
        common.load_config(str(p))


def test_config_sha256_deterministic_and_key_order_independent():
    cfg1 = {"site": "A", "date_from": "2030-01-01"}
    cfg2 = {"date_from": "2030-01-01", "site": "A"}
    assert common.config_sha256(cfg1) == common.config_sha256(cfg2)
    assert len(common.config_sha256(cfg1)) == 64


def test_slugify_handles_accents_and_punctuation():
    assert common.slugify("Depot") == "depot"
    assert common.slugify("North Yard (annex)!") == "north-yard-annex"
    assert common.slugify("Café Zone") == "cafe-zone"
    # non-Latin-only input has no ASCII fallback -> default placeholder
    assert common.slugify("工厂") == "site"


def test_parse_wa_ts_am_pm_and_noon_midnight():
    assert common.parse_wa_ts("14/01/2030", "9", "37", "am").isoformat() == "2030-01-14T09:37:00"
    assert common.parse_wa_ts("14/01/2030", "1", "00", "pm").isoformat() == "2030-01-14T13:00:00"
    assert common.parse_wa_ts("14/01/2030", "12", "00", "pm").isoformat() == "2030-01-14T12:00:00"
    assert common.parse_wa_ts("14/01/2030", "12", "00", "am").isoformat() == "2030-01-14T00:00:00"


def test_read_stage_missing_upstream_raises(tmp_path):
    with pytest.raises(common.MissingUpstream):
        common.read_stage(tmp_path, "messages")


def test_read_stage_failed_status_raises(tmp_path):
    p = tmp_path / "messages.json"
    p.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    with pytest.raises(common.MissingUpstream):
        common.read_stage(tmp_path, "messages")


def test_err_shape():
    e = common.err("M00001", "FILE_MISSING", "no such file", retryable=True, attempts=2)
    assert e == {
        "item": "M00001",
        "code": "FILE_MISSING",
        "message": "no such file",
        "retryable": True,
        "attempts": 2,
    }


def test_get_logger_writes_to_file(tmp_path):
    (tmp_path / "logs").mkdir()
    logger = common.get_logger(tmp_path, "s1_parse_chat", "INFO")
    logger.info("hello from test")
    for h in logger.handlers:
        h.flush()
    log_file = tmp_path / "logs" / "s1_parse_chat.log"
    assert log_file.is_file()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_declared_stop_number_reads_digits_and_words():
    assert common.declared_sector("Stop two, east stair") == 2
    assert common.declared_sector("sector 12: roof") == 12
    assert common.declared_sector("Stop 7") == 7
    assert common.declared_sector("nothing declared here") is None
    custom = r"(?i)\bbay\s+(\d+)\b"
    assert common.declared_sector("Bay 4 is clear", custom) == 4


def test_config_defaults_and_profile_validation(tmp_path, sample_cfg):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(sample_cfg), encoding="utf-8")
    assert common.load_config(str(p))["profile"] == "site-inspection"
    sample_cfg["profile"] = "does-not-exist"
    p.write_text(json.dumps(sample_cfg), encoding="utf-8")
    with pytest.raises(common.ConfigError):
        common.load_config(str(p))


def test_sha256_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello", encoding="utf-8")
    import hashlib
    expected = hashlib.sha256(b"hello").hexdigest()
    assert common.sha256_file(p) == expected
