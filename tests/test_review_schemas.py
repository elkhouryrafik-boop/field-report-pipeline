"""Schema acceptance for the s5a/s5b artifacts.

Run: python -m pytest tests/test_review_schemas.py -v
"""

from __future__ import annotations

from pathlib import Path

from field_report.lib import common

REPO = Path(__file__).resolve().parents[1]


def _envelope(stage: str) -> dict:
    return {
        "status": "ok",
        "errors": [],
        "meta": {
            "stage": stage,
            "site": "testville",
            "generated_at": "2030-01-15T10:00:00Z",
            "config_sha256": "a" * 64,
            "input_counts": {},
            "output_counts": {},
        },
    }


def _minimal_prelabels() -> dict:
    p = _envelope("s5a_pre_label")
    p["photos"] = [{
        "filename": "IMG-1.jpg", "cluster_id": "C001",
        "caption": "Missing stair handrail, landing (synthetic scene, stop 2).",
        "sector_guess": 5, "sector_confidence": 0.8,
        "visible_damage": True,
        "countable": {"what": "handrail posts", "n": 3},
    }]
    p["bindings"] = [{
        "cluster_id": "C001", "audio": "PTT-1.opus", "photos": ["IMG-1.jpg"],
        "confidence": 0.91,
        "basis": "transcript says handrail; caption shows stair",
    }]
    p["counts"] = {"photos_in": 1, "photos_captioned": 1, "photos_failed": 0,
                   "audios_in": 1, "bindings_proposed": 1, "audios_unbound": 0}
    return p


def _minimal_review() -> dict:
    r = _envelope("s5b_review")
    r["meta"]["reviewer"] = "reviewer"
    r["meta"]["clusters_total"] = 1
    r["meta"]["prelabels_sha256"] = "b" * 64
    r["stops"] = [{
        "cluster_id": "C001", "verdict": "approved",
        "reviewed_at": "2030-01-15T10:05:00Z",
        "human_note": None, "gps_override": None,
        "split_at": None, "merge_with": None,
        "photos": [{"filename": "IMG-1.jpg", "caption_override": None,
                    "excluded": False, "move_to": None}],
        "audios": [{"file": "PTT-1.opus", "text_override": None, "en_override": None,
                    "binding": ["IMG-1.jpg"], "binding_source": "accepted",
                    "dropped": False, "drop_reason": None}],
    }]
    r["counts"] = {"approved": 1, "discarded": 0, "pending": 0,
                   "bindings_accepted": 1, "bindings_corrected": 0, "bindings_manual": 0,
                   "photos_excluded": 0, "photos_moved": 0, "texts_edited": 0}
    return r


def test_minimal_prelabels_validates():
    assert common.validate(_minimal_prelabels(), "prelabels") == []


def test_minimal_review_validates():
    assert common.validate(_minimal_review(), "review") == []


def test_caption_may_be_null_when_the_vision_call_failed():
    p = _minimal_prelabels()
    p["photos"][0]["caption"] = None
    p["photos"][0]["sector_guess"] = None
    p["photos"][0]["sector_confidence"] = None
    p["photos"][0]["visible_damage"] = None
    assert common.validate(p, "prelabels") == []


def test_binding_confidence_out_of_range_is_rejected():
    p = _minimal_prelabels()
    p["bindings"][0]["confidence"] = 1.4
    assert common.validate(p, "prelabels") != []


def test_binding_without_basis_is_rejected():
    p = _minimal_prelabels()
    del p["bindings"][0]["basis"]
    assert common.validate(p, "prelabels") != []


def test_unknown_verdict_is_rejected():
    r = _minimal_review()
    r["stops"][0]["verdict"] = "maybe"
    assert common.validate(r, "review") != []


def test_unknown_binding_source_is_rejected():
    r = _minimal_review()
    r["stops"][0]["audios"][0]["binding_source"] = "vibes"
    assert common.validate(r, "review") != []


def test_dropped_audio_requires_a_reason():
    r = _minimal_review()
    r["stops"][0]["audios"][0]["dropped"] = True
    assert common.validate(r, "review") != []
    r["stops"][0]["audios"][0]["drop_reason"] = "coordination chatter"
    assert common.validate(r, "review") == []


def test_discarded_stop_requires_a_note():
    r = _minimal_review()
    r["stops"][0]["verdict"] = "discarded"
    assert common.validate(r, "review") != []
    r["stops"][0]["human_note"] = "transit shots between sites"
    assert common.validate(r, "review") == []
