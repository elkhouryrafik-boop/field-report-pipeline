"""Acceptance tests for field_report/stages/s5c_apply_review.py.

Run: python -m pytest tests/test_s5c_apply.py -v
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from field_report.lib import common

from field_report.stages import s5c_apply_review as s5c

REPO = Path(__file__).resolve().parents[1]


def _stop(cluster_id: str, photos: list[str], audios: list[str], **kw) -> dict:
    stop = {
        "cluster_id": cluster_id, "verdict": "approved",
        "reviewed_at": "2030-01-15T10:00:00Z",
        "human_note": None, "gps_override": None,
        "split_at": None, "merge_with": None,
        "photos": [{"filename": f, "caption_override": None,
                    "excluded": False, "move_to": None} for f in photos],
        "audios": [{"file": a, "text_override": None, "en_override": None,
                    "binding": [], "binding_source": "none",
                    "dropped": False, "drop_reason": None} for a in audios],
    }
    stop.update(kw)
    return stop


@pytest.fixture
def review() -> dict:
    """A review that approves all three fixture stops and changes nothing."""
    return {
        "status": "ok", "errors": [],
        "meta": {"stage": "s5b_review", "site": "Testville",
                 "generated_at": "2030-01-15T10:00:00Z",
                 "config_sha256": "0" * 64,
                 "input_counts": {}, "output_counts": {},
                 "reviewer": "tester", "clusters_total": 3,
                 "prelabels_sha256": None},
        "stops": [
            _stop("C001", ["IMG-1.jpg", "IMG-2.jpg"], ["PTT-1.opus"]),
            _stop("C002", ["IMG-3.jpg", "IMG-4.jpg"], ["PTT-2.opus", "PTT-3.opus"]),
            _stop("C003", ["IMG-5.jpg"], []),
        ],
        "counts": {"approved": 3, "discarded": 0, "pending": 0,
                   "bindings_accepted": 0, "bindings_corrected": 0, "bindings_manual": 0,
                   "photos_excluded": 0, "photos_moved": 0, "texts_edited": 0},
    }


@pytest.fixture
def transcripts(run_dir: Path) -> dict:
    return json.loads((run_dir / "transcripts_en.json").read_text(encoding="utf-8"))


def test_approve_everything_changes_nothing_structural(clusters, transcripts, review):
    cv, tv, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    assert [c["id"] for c in cv["clusters"]] == ["C001", "C002", "C003"]
    assert len(cv["clusters"][0]["photos"]) == 2
    assert len(tv["items"]) == 3


def test_every_verified_cluster_is_marked_human_verified(clusters, transcripts, review):
    cv, _, _ = s5c.apply_review(clusters, transcripts, review)
    assert all(c["human_verified"] for c in cv["clusters"])


def test_output_validates_against_the_clusters_schema(clusters, transcripts, review):
    cv, tv, _ = s5c.apply_review(clusters, transcripts, review)
    assert common.validate(cv, "clusters_verified") == []
    assert common.validate(tv, "transcripts_en") == []


def test_apply_is_idempotent(clusters, transcripts, review):
    a = s5c.apply_review(copy.deepcopy(clusters), copy.deepcopy(transcripts), review)[0]
    b = s5c.apply_review(copy.deepcopy(clusters), copy.deepcopy(transcripts), review)[0]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_pending_stop_is_reported_as_a_problem(clusters, transcripts, review):
    review["stops"][1]["verdict"] = "pending"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any("C002" in p and "pending" in p for p in problems)


def test_stop_with_no_verdict_is_carried_through_unverified(clusters, transcripts, review):
    review["stops"] = [s for s in review["stops"] if s["cluster_id"] != "C003"]
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    c003 = next(c for c in cv["clusters"] if c["id"] == "C003")
    assert c003["human_verified"] is False
    assert any("C003" in p and "no verdict" in p for p in problems)


def test_excluded_photo_is_removed_from_the_cluster(clusters, transcripts, review):
    review["stops"][0]["photos"][1]["excluded"] = True
    review["stops"][0]["photos"][1]["exclude_reason"] = "blurry duplicate"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    names = [p["filename"] for p in cv["clusters"][0]["photos"]]
    assert names == ["IMG-1.jpg"]
    assert problems == []


def test_caption_override_lands_in_photo_captions(clusters, transcripts, review):
    review["stops"][0]["photos"][0]["caption_override"] = "East stair, handrail missing at landing"
    cv, _, _ = s5c.apply_review(clusters, transcripts, review)
    assert cv["clusters"][0]["photo_captions"]["IMG-1.jpg"] == "East stair, handrail missing at landing"


def test_binding_lands_in_audio_bindings(clusters, transcripts, review):
    review["stops"][0]["audios"][0]["binding"] = ["IMG-1.jpg"]
    review["stops"][0]["audios"][0]["binding_source"] = "accepted"
    cv, _, _ = s5c.apply_review(clusters, transcripts, review)
    assert cv["clusters"][0]["audio_bindings"]["PTT-1.opus"] == ["IMG-1.jpg"]


def test_binding_to_an_excluded_photo_is_a_conflict(clusters, transcripts, review):
    review["stops"][0]["photos"][0]["excluded"] = True
    review["stops"][0]["photos"][0]["exclude_reason"] = "private property"
    review["stops"][0]["audios"][0]["binding"] = ["IMG-1.jpg"]
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "IMG-1.jpg" in p for p in problems)


def test_dropped_audio_is_removed_from_cluster_and_transcripts(clusters, transcripts, review):
    review["stops"][1]["audios"][1]["dropped"] = True
    review["stops"][1]["audios"][1]["drop_reason"] = "coordination chatter"
    cv, tv, _ = s5c.apply_review(clusters, transcripts, review)
    assert [a["filename"] for a in cv["clusters"][1]["audios"]] == ["PTT-2.opus"]
    assert "PTT-3.opus" not in [i["file"] for i in tv["items"]]


def test_text_overrides_replace_transcript_text_and_keep_the_original(clusters, transcripts, review):
    review["stops"][1]["audios"][0]["en_override"] = "This is the east stair."
    review["stops"][1]["audios"][0]["text_override"] = "   ."
    _, tv, _ = s5c.apply_review(clusters, transcripts, review)
    item = next(i for i in tv["items"] if i["file"] == "PTT-2.opus")
    assert item["en_text"] == "This is the east stair."
    assert item["text_clean"] == "   ."
    assert item["human_edited"] is True
    assert item["en_text_machine"] == "This is the east stair and [unclear]."


def test_moved_photo_leaves_source_and_joins_target(clusters, transcripts, review):
    review["stops"][0]["photos"][1]["move_to"] = "C002"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    src = [p["filename"] for p in cv["clusters"][0]["photos"]]
    tgt = [p["filename"] for p in cv["clusters"][1]["photos"]]
    assert src == ["IMG-1.jpg"]
    assert "IMG-2.jpg" in tgt


def test_moving_a_photo_into_a_discarded_stop_is_a_conflict(clusters, transcripts, review):
    review["stops"][2]["verdict"] = "discarded"
    review["stops"][2]["human_note"] = "transit shots"
    review["stops"][0]["photos"][1]["move_to"] = "C003"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "C003" in p for p in problems)


def test_moving_to_an_unknown_stop_is_a_conflict(clusters, transcripts, review):
    review["stops"][0]["photos"][1]["move_to"] = "C099"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "C099" in p for p in problems)


def test_discarded_stop_is_dropped_and_recorded(clusters, transcripts, review):
    review["stops"][2]["verdict"] = "discarded"
    review["stops"][2]["human_note"] = "transit shots between sites"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert [c["id"] for c in cv["clusters"]] == ["C001", "C002"]
    assert cv["discarded"] == [{"cluster_id": "C003",
                                "reason": "transit shots between sites"}]


def test_split_partitions_messages_at_the_named_message(clusters, transcripts, review):
    review["stops"][1]["split_at"] = "M00007"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    ids = [c["id"] for c in cv["clusters"]]
    assert ids == ["C001", "C002a", "C002b", "C003"]
    a = next(c for c in cv["clusters"] if c["id"] == "C002a")
    b = next(c for c in cv["clusters"] if c["id"] == "C002b")
    assert a["message_ids"] == ["M00005", "M00006"]
    assert b["message_ids"] == ["M00007", "M00008"]
    assert [p["filename"] for p in a["photos"]] == ["IMG-3.jpg"]
    assert [p["filename"] for p in b["photos"]] == ["IMG-4.jpg"]
    assert [x["filename"] for x in a["audios"]] == ["PTT-2.opus"]
    assert [x["filename"] for x in b["audios"]] == ["PTT-3.opus"]


def test_split_preserves_every_message_id(clusters, transcripts, review):
    review["stops"][1]["split_at"] = "M00007"
    cv, _, _ = s5c.apply_review(clusters, transcripts, review)
    before = set(clusters["clusters"][1]["message_ids"])
    after = set()
    for c in cv["clusters"]:
        if c["origin"].startswith("C002"):
            after |= set(c["message_ids"])
    assert before == after


def test_split_at_a_message_not_in_the_stop_is_a_conflict(clusters, transcripts, review):
    review["stops"][1]["split_at"] = "M00002"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "M00002" in p for p in problems)


def test_merge_joins_two_stops_and_preserves_every_message(clusters, transcripts, review):
    review["stops"][1]["merge_with"] = "C003"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    assert [c["id"] for c in cv["clusters"]] == ["C001", "C002"]
    merged = cv["clusters"][1]
    assert merged["message_ids"] == ["M00005", "M00006", "M00007", "M00008", "M00009"]
    assert len(merged["photos"]) == 3
    assert merged["origin"] == "C002+C003"


def test_merge_with_a_discarded_stop_is_a_conflict(clusters, transcripts, review):
    review["stops"][2]["verdict"] = "discarded"
    review["stops"][2]["human_note"] = "transit"
    review["stops"][1]["merge_with"] = "C003"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") for p in problems)


def test_split_and_merge_on_the_same_stop_is_a_conflict(clusters, transcripts, review):
    review["stops"][1]["split_at"] = "M00007"
    review["stops"][1]["merge_with"] = "C003"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "C002" in p for p in problems)


def test_review_is_remapped_when_cluster_ids_shift(clusters, transcripts, review):
    """s5 --force renumbered the stops; the verdicts must follow their messages."""
    shifted = copy.deepcopy(clusters)
    for c, new_id in zip(shifted["clusters"], ["C001", "C007", "C008"]):
        c["id"] = new_id
    cv, _, problems = s5c.apply_review(shifted, transcripts, review)
    assert not any(p.startswith("CONFLICT") for p in problems)
    c007 = next(c for c in cv["clusters"] if c["id"] == "C007")
    assert c007["human_verified"] is True


def test_orphaned_verdicts_are_reported_not_silently_dropped(clusters, transcripts, review):
    reclustered = copy.deepcopy(clusters)
    reclustered["clusters"] = reclustered["clusters"][:2]
    reclustered["counts"]["clusters"] = 2
    _, _, problems = s5c.apply_review(reclustered, transcripts, review)
    assert any("C003" in p and "orphan" in p.lower() for p in problems)


# ---------------------------------------------------------------------------
# Regressions: each of these once shipped a human decision
# wrong, or lost it, at exit 0.
# ---------------------------------------------------------------------------


def test_reviewer_gps_correction_is_applied(clusters, transcripts, review):
    """The reviewer re-pinned the stop; the machine's pin must not survive."""
    review["stops"][0]["gps_override"] = {"lat": 0.0012, "lng": 0.0021}
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    c001 = next(c for c in cv["clusters"] if c["id"] == "C001")
    assert c001["gps"] == {"lat": 0.0012, "lng": 0.0021}
    assert c001["gps_inherited"] is False
    assert c001["gps_message_id"] is None
    assert c001["gps_source"] == "reviewer"


def test_gps_source_records_where_the_pin_came_from(clusters, transcripts, review):
    cv, _, _ = s5c.apply_review(clusters, transcripts, review)
    by_id = {c["id"]: c for c in cv["clusters"]}
    assert by_id["C001"]["gps_source"] == "pin"        # fixture C001 carries its own
    assert by_id["C003"]["gps_source"] is None         # and C003 has no pin at all


def test_three_stops_merged_into_one_keep_every_message(clusters, transcripts, review):
    """C002 -> C001 and C003 -> C001: pair-wise application dropped the second."""
    review["stops"][1]["merge_with"] = "C001"
    review["stops"][2]["merge_with"] = "C001"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert not any(p.startswith("CONFLICT") for p in problems), problems
    assert len(cv["clusters"]) == 1
    site = cv["clusters"][0]
    assert site["message_ids"] == [f"M{i:05d}" for i in range(1, 10)]
    assert site["origin"] == "C001+C002+C003"
    assert len(site["photos"]) == 5


def test_merging_into_a_stop_that_is_being_split_is_a_conflict(clusters, transcripts, review):
    review["stops"][1]["split_at"] = "M00007"
    review["stops"][2]["merge_with"] = "C002"
    _, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert any(p.startswith("CONFLICT") and "C003" in p for p in problems), problems


def test_a_moved_photo_takes_its_message_with_it(clusters, transcripts, review):
    """The message must leave the source, or it is counted in two stops at once."""
    review["stops"][1]["photos"][0]["move_to"] = "C001"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    by_id = {c["id"]: c for c in cv["clusters"]}
    assert "M00005" in by_id["C001"]["message_ids"]
    assert "M00005" not in by_id["C002"]["message_ids"]
    assert cv["counts"]["messages_assigned"] == cv["counts"]["messages_in"]
    assert cv["counts"]["messages_unassigned"] == 0


def test_discarded_messages_are_accounted_not_lost(clusters, transcripts, review):
    review["stops"][2]["verdict"] = "discarded"
    review["stops"][2]["human_note"] = "transit footage"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert not any(p.startswith("CONFLICT") for p in problems), problems
    assert cv["counts"]["messages_dropped"] == 1
    assert cv["counts"]["messages_unassigned"] == 0


def test_a_stop_with_no_media_is_remapped_by_its_messages(clusters, transcripts, review):
    """Media overlap scores 0 for a pin-only stop, which used to orphan its verdict."""
    for stop, ids in zip(review["stops"], (["M00001", "M00002", "M00003", "M00004"],
                                           ["M00005", "M00006", "M00007", "M00008"],
                                           ["M00009"])):
        stop["message_ids"] = ids
    review["stops"][2]["photos"] = []
    review["stops"][2]["human_note"] = "east stair, not the west one"

    shifted = copy.deepcopy(clusters)
    for c, new_id in zip(shifted["clusters"], ["C001", "C007", "C008"]):
        c["id"] = new_id
    shifted["clusters"][2]["photos"] = []

    cv, _, problems = s5c.apply_review(shifted, transcripts, review)
    assert not any("orphan" in p.lower() for p in problems), problems
    c008 = next(c for c in cv["clusters"] if c["id"] == "C008")
    assert c008["human_note"] == "east stair, not the west one"
    assert c008["human_verified"] is True


# --- the dot convention: an asset may carry several pins (s5 writes gps_points) ---


def _with_pins(clusters: dict) -> dict:
    """C002 pinned twice, each photo bound to the pin it was taken at."""
    c2 = clusters["clusters"][1]
    c2["gps_points"] = [
        {"lat": 0.0012, "lng": 0.0021, "message_id": "M00005", "ts": "2030-01-14T10:56:00"},
        {"lat": 0.0012, "lng": 0.0021, "message_id": "M00007", "ts": "2030-01-14T10:58:00"},
    ]
    c2["photos"][0]["pin_message_id"] = "M00005"
    c2["photos"][1]["pin_message_id"] = "M00007"
    return clusters


def test_split_gives_each_half_only_its_own_pins(clusters, transcripts, review):
    clusters = _with_pins(clusters)
    review["stops"][1]["split_at"] = "M00007"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert not any(p.startswith("CONFLICT") for p in problems), problems
    halves = {c["id"]: c for c in cv["clusters"] if c["id"].startswith("C002")}
    assert [g["message_id"] for g in halves["C002a"]["gps_points"]] == ["M00005"]
    assert [g["message_id"] for g in halves["C002b"]["gps_points"]] == ["M00007"]


def test_merge_unions_the_pin_lists(clusters, transcripts, review):
    clusters = _with_pins(clusters)
    clusters["clusters"][0]["gps_points"] = [
        {"lat": 0.0012, "lng": 0.0021, "message_id": "M00002", "ts": "2030-01-14T10:50:00"}]
    review["stops"][1]["merge_with"] = "C001"
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert not any(p.startswith("CONFLICT") for p in problems), problems
    site = cv["clusters"][0]
    assert [g["message_id"] for g in site["gps_points"]] == ["M00002", "M00005", "M00007"]


def test_a_moved_photo_takes_its_pin_with_it(clusters, transcripts, review):
    """pin_message_id must still resolve in the stop that now owns the photo."""
    clusters = _with_pins(clusters)
    review["stops"][1]["photos"][1]["move_to"] = "C001"   # IMG-4.jpg, pinned at M00007
    cv, _, problems = s5c.apply_review(clusters, transcripts, review)
    assert problems == []
    c001 = next(c for c in cv["clusters"] if c["id"] == "C001")
    moved = next(p for p in c001["photos"] if p["filename"] == "IMG-4.jpg")
    assert moved["pin_message_id"] == "M00007"
    assert "M00007" in {g["message_id"] for g in c001["gps_points"]}
