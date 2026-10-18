"""s5b review server: persistence, resume, path safety.

Run: python -m pytest tests/test_s5b_server.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from field_report.stages import s5b_review as s5b  # noqa: E402


def test_initial_review_marks_every_stop_pending(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    assert len(review["stops"]) == 3
    assert all(s["verdict"] == "pending" for s in review["stops"])
    assert review["counts"]["pending"] == 3


def test_initial_review_seeds_bindings_from_prelabels(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    prelabels = {"photos": [], "bindings": [
        {"cluster_id": "C001", "audio": "PTT-1.opus", "photos": ["IMG-1.jpg"],
         "confidence": 0.9, "basis": "transcript says handrail; caption shows stair"}]}
    review = s5b.initial_review(clusters, transcripts, prelabels, cfg, reviewer="tester")
    a = review["stops"][0]["audios"][0]
    assert a["binding"] == ["IMG-1.jpg"]
    assert a["binding_source"] == "accepted"


def test_save_stop_persists_and_is_atomic(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    s5b.save_review(run_dir, review)
    stop = dict(review["stops"][0])
    stop["verdict"] = "approved"
    stop["reviewed_at"] = "2030-01-15T10:00:00Z"
    s5b.save_stop(run_dir, stop)
    on_disk = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
    assert on_disk["stops"][0]["verdict"] == "approved"
    assert on_disk["counts"]["approved"] == 1
    assert on_disk["counts"]["pending"] == 2
    assert not (run_dir / "review.json.tmp").exists()


def test_resume_finds_the_first_pending_stop(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    review["stops"][0]["verdict"] = "approved"
    s5b.save_review(run_dir, review)
    assert s5b.first_pending(review) == "C002"


def test_existing_review_is_reloaded_not_overwritten(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    review["stops"][0]["verdict"] = "approved"
    review["stops"][0]["human_note"] = "keep me"
    s5b.save_review(run_dir, review)
    reloaded = s5b.load_or_init(run_dir, clusters, transcripts, None, cfg, "tester")
    assert reloaded["stops"][0]["human_note"] == "keep me"


def test_new_cluster_after_recluster_is_added_as_pending(run_dir, cfg, clusters):
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    review["stops"] = review["stops"][:2]
    s5b.save_review(run_dir, review)
    reloaded = s5b.load_or_init(run_dir, clusters, transcripts, None, cfg, "tester")
    assert len(reloaded["stops"]) == 3
    assert reloaded["stops"][2]["verdict"] == "pending"


@pytest.mark.parametrize("attack", [
    "../../../etc/passwd", "..\\..\\windows\\system32\\config\\sam",
    "/etc/passwd", "IMG-1.jpg/../../secret.txt",
])
def test_media_path_traversal_is_refused(attack):
    manifest = {"items": [{"filename": "IMG-1.jpg", "abs_path": "C:/media/IMG-1.jpg",
                           "exists": True}]}
    assert s5b.resolve_media(manifest, attack) is None


def test_media_resolves_only_manifest_filenames():
    manifest = {"items": [{"filename": "IMG-1.jpg", "abs_path": "C:/media/IMG-1.jpg",
                           "exists": True}]}
    assert s5b.resolve_media(manifest, "IMG-1.jpg") == "C:/media/IMG-1.jpg"
    assert s5b.resolve_media(manifest, "IMG-2.jpg") is None


# ---------------------------------------------------------------------------
# finish_problems must agree with s5c.apply_review about what is a CONFLICT.
# A gate that says "clear" and then exits 4 wastes the reviewer's whole pass.
# ---------------------------------------------------------------------------

def _reviewed(run_dir: Path, cfg: dict, clusters: dict) -> dict:
    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    review = s5b.initial_review(clusters, transcripts, None, cfg, reviewer="tester")
    for s in review["stops"]:
        s["verdict"] = "approved"
        s["reviewed_at"] = "2030-01-15T10:00:00Z"
    return review


def _discard(review: dict, cluster_id: str) -> None:
    stop = next(s for s in review["stops"] if s["cluster_id"] == cluster_id)
    stop["verdict"] = "discarded"
    stop["human_note"] = "transit shots between sites"


def test_finish_flags_a_move_into_a_discarded_stop(run_dir, cfg, clusters):
    review = _reviewed(run_dir, cfg, clusters)
    _discard(review, "C003")
    review["stops"][0]["photos"][1]["move_to"] = "C003"
    assert any(p.startswith("CONFLICT") and "C003" in p
               for p in s5b.finish_problems(review))


def test_finish_flags_a_merge_with_a_discarded_stop(run_dir, cfg, clusters):
    review = _reviewed(run_dir, cfg, clusters)
    _discard(review, "C003")
    review["stops"][1]["merge_with"] = "C003"
    assert any(p.startswith("CONFLICT") and "C003" in p
               for p in s5b.finish_problems(review))


def test_finish_flags_a_self_merge(run_dir, cfg, clusters):
    review = _reviewed(run_dir, cfg, clusters)
    review["stops"][1]["merge_with"] = "C002"
    assert any(p.startswith("CONFLICT") for p in s5b.finish_problems(review))


def test_finish_flags_a_photo_moving_to_its_own_stop(run_dir, cfg, clusters):
    review = _reviewed(run_dir, cfg, clusters)
    review["stops"][0]["photos"][1]["move_to"] = "C001"
    assert any(p.startswith("CONFLICT") for p in s5b.finish_problems(review))


def test_finish_is_silent_on_a_clean_review(run_dir, cfg, clusters):
    review = _reviewed(run_dir, cfg, clusters)
    assert s5b.finish_problems(review) == []


def test_finish_agrees_with_the_applier_on_every_conflict(run_dir, cfg, clusters):
    """The gate's verdict and the applier's verdict must not diverge."""
    from field_report.stages import s5c_apply_review as s5c

    transcripts = json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))
    cases = []

    r = _reviewed(run_dir, cfg, clusters)
    _discard(r, "C003")
    r["stops"][0]["photos"][1]["move_to"] = "C003"
    cases.append(("move into discarded", r))

    r = _reviewed(run_dir, cfg, clusters)
    _discard(r, "C003")
    r["stops"][1]["merge_with"] = "C003"
    cases.append(("merge with discarded", r))

    r = _reviewed(run_dir, cfg, clusters)
    r["stops"][1]["split_at"] = "M00007"
    r["stops"][1]["merge_with"] = "C003"
    cases.append(("split and merge", r))

    for label, review in cases:
        gate = [p for p in s5b.finish_problems(review) if p.startswith("CONFLICT")]
        _, _, applier = s5c.apply_review(clusters, transcripts, review)
        applied = [p for p in applier if p.startswith("CONFLICT")]
        assert bool(gate) == bool(applied), (
            f"{label}: gate said {gate!r} but applier said {applied!r}")
