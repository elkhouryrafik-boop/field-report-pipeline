"""The offline demo (`python -m field_report demo --offline`), end to end, per profile."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from field_report import demo
from field_report.__main__ import main as cli_main

REPO = Path(__file__).resolve().parents[1]
PROFILES = ["site-inspection", "damage-assessment", "event-log"]


@pytest.fixture(scope="module", params=PROFILES)
def demo_out(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(request.param) / "demo"
    assert demo.main(["--offline", "--profile", request.param, "--out", str(out)]) == 0
    return request.param, out


def test_deliverables_exist(demo_out):
    profile, out = demo_out
    names = {p.name for p in out.iterdir() if p.is_file()}
    assert {"report.docx", "review-sheet.docx", "run-manifest.json"} <= names
    assert ("quantities.xlsx" in names) == (profile == "damage-assessment")


def test_manifest_records_the_run(demo_out):
    profile, out = demo_out
    m = json.loads((out / "run-manifest.json").read_text(encoding="utf-8"))
    assert m["command"] == "demo" and m["demo_exit_code"] == 0
    assert "report.docx" in m["deliverables"]


def test_gate_passed(demo_out):
    _, out = demo_out
    v = json.loads((out / "work" / "s10_verification.json").read_text(encoding="utf-8"))
    assert v["status"] in ("ok", "partial")


def test_demo_is_rerunnable(demo_out):
    profile, out = demo_out
    assert demo.main(["--offline", "--profile", profile, "--out", str(out)]) == 0


def test_unknown_profile_exits_2(tmp_path):
    assert demo.main(["--offline", "--profile", "nope", "--out", str(tmp_path / "o")]) == 2


def test_switching_profile_removes_stale_quantities(tmp_path):
    out = tmp_path / "o"
    assert demo.main(["--offline", "--profile", "damage-assessment", "--out", str(out)]) == 0
    assert (out / "quantities.xlsx").is_file()
    assert demo.main(["--offline", "--profile", "site-inspection", "--out", str(out), "--force"]) == 0
    assert not (out / "quantities.xlsx").exists()


def test_cli_dispatches_demo(tmp_path):
    assert cli_main(["demo", "--offline", "--out", str(tmp_path / "o")]) == 0


def test_cli_without_arguments_is_a_usage_error(capsys):
    assert cli_main([]) == 2
    captured = capsys.readouterr()
    assert "demo" in captured.err + captured.out


def test_python_dash_m_runs_the_demo(tmp_path):
    out = tmp_path / "o"
    r = subprocess.run([sys.executable, "-m", "field_report", "demo", "--offline", "--out", str(out)],
                       cwd=REPO, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-500:]
    assert (out / "report.docx").is_file()


def test_no_network_needed(tmp_path, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("the offline demo opened a socket")
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert demo.main(["--offline", "--out", str(tmp_path / "o")]) == 0
