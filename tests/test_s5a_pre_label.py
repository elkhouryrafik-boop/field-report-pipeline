"""s5a pre-label: deterministic transcript->photo binding and caption rows."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from field_report import run
from field_report.stages import s5a_pre_label as s5a

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample"

PHOTOS = [
    {"filename": "IMG-1.jpg", "ts": "2030-01-14T09:12:00"},
    {"filename": "IMG-2.jpg", "ts": "2030-01-14T09:25:00"},
    {"filename": "IMG-3.jpg", "ts": "2030-01-14T09:30:00"},
]
CAPTIONS = {"IMG-1.jpg": "Diagonal wall crack (synthetic scene, stop 1).",
            "IMG-2.jpg": "Ceiling water stain (synthetic scene, stop 1).",
            "IMG-3.jpg": "Missing stair handrail (synthetic scene, stop 2)."}


def audio(ts="2030-01-14T09:23:00"):
    return {"filename": "PTT-1.opus", "ts": ts}


def test_content_words_drop_noise_and_stem_plurals():
    words = s5a.content_words("Stop one, the stairs have a diagonal crack. Priority high.")
    assert "diagonal" in words and "crack" in words and "stair" in words
    assert "stop" not in words and "priority" not in words


def test_binding_follows_shared_words():
    b = s5a.propose_binding(audio(), {"en_text": "A diagonal crack runs up the wall."}, PHOTOS, CAPTIONS)
    assert b["photos"] == ["IMG-1.jpg"]
    assert b["basis"].startswith("shared words:") and len(b["basis"]) >= 10
    assert 0.5 < b["confidence"] <= 0.95


def test_ties_bind_to_every_best_photo():
    caps = {**CAPTIONS, "IMG-2.jpg": "Diagonal wall crack, second angle (synthetic scene)."}
    b = s5a.propose_binding(audio(), {"en_text": "Diagonal wall crack."}, PHOTOS[:2], caps)
    assert b["photos"] == ["IMG-1.jpg", "IMG-2.jpg"]


def test_no_shared_words_falls_back_to_the_nearest_photo_with_low_confidence():
    b = s5a.propose_binding(audio("2030-01-14T09:26:00"), {"en_text": "Something unrelated entirely."},
                            PHOTOS, CAPTIONS)
    assert b["photos"] == ["IMG-2.jpg"]
    assert b["confidence"] == 0.3 and "nearest photo in time" in b["basis"]


def test_a_stop_without_photos_proposes_no_binding():
    assert s5a.propose_binding(audio(), {"en_text": "A crack."}, [], {}) is None


@pytest.fixture()
def prelabelled(tmp_path):
    dst = tmp_path / "sample"
    shutil.copytree(FIXTURE, dst)
    rd = tmp_path / "run"
    assert run.main(["--config", str(dst / "config.json"), "--run-dir", str(rd), "--offline",
                     "--until", "s5a"]) == 0
    return rd, json.loads((rd / "prelabels.json").read_text(encoding="utf-8"))


def test_sample_prelabels_cover_every_photo_and_note(prelabelled):
    _, pre = prelabelled
    assert pre["counts"] == {"photos_in": 12, "photos_captioned": 12, "photos_failed": 0, "audios_in": 8,
                             "bindings_proposed": 8, "audios_unbound": 0}
    assert all(p["caption"] and p["sector_guess"] for p in pre["photos"])
    assert {b["audio"][:3] for b in pre["bindings"]} == {"PTT"}


def test_captions_none_is_a_choice_not_a_failure(prelabelled, tmp_path):
    rd, _ = prelabelled
    code = s5a.main(["--config", str(tmp_path / "sample" / "config.json"), "--run-dir", str(rd),
                     "--captions", "none", "--force"])
    assert code == 0
    pre = json.loads((rd / "prelabels.json").read_text(encoding="utf-8"))
    assert pre["counts"]["photos_captioned"] == 0 and pre["status"] == "ok"


def test_missing_caption_sidecar_is_a_partial_not_a_crash(tmp_path):
    dst = tmp_path / "sample"
    shutil.copytree(FIXTURE, dst)
    (dst / "export" / "IMG-20300114-WA0004.caption.json").unlink()
    rd = tmp_path / "run"
    code = run.main(["--config", str(dst / "config.json"), "--run-dir", str(rd), "--offline", "--auto"])
    assert code == 3
    pre = json.loads((rd / "prelabels.json").read_text(encoding="utf-8"))
    assert pre["counts"]["photos_failed"] == 1 and pre["status"] == "partial"
