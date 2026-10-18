"""Regenerate tests/fixtures/review_run/ (a hand-sized 3-stop run used by the s5b/s5c tests).

Everything in it is invented. Run: python tests/make_review_run.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures" / "review_run"
SHA = "0" * 64
D = "2030-01-14T"


def _meta(stage: str, **extra) -> dict:
    return {"stage": stage, "site": "Testville", "generated_at": "2030-01-14T12:00:00Z",
            "config_sha256": SHA, "input_counts": {}, "output_counts": {}, **extra}


def _cluster(cid, seq, ids, photos, audios, texts, gps, gps_mid, inherited, ts0, ts1, reason):
    c = {"id": cid, "seq": seq, "start_ts": D + ts0, "end_ts": D + ts1, "boundary_reason": reason,
         "gps": gps, "gps_message_id": gps_mid, "gps_inherited": inherited,
         "message_ids": ids,
         "photos": [{"filename": f, "message_id": m, "ts": D + t, "exists": True} for f, m, t in photos],
         "audios": [{"filename": f, "message_id": m, "ts": D + t, "exists": True} for f, m, t in audios],
         "texts": [{"message_id": m, "ts": D + t, "sender": "Inspector A", "text": x}
                   for m, t, x in texts],
         "omitted_count": 0, "senders": ["Inspector A"]}
    if gps_mid and not inherited:
        c["gps_points"] = [{"lat": gps["lat"], "lng": gps["lng"], "message_id": gps_mid, "ts": D + ts0}]
    return c


def _item(f, mid, ts, en):
    return {"file": f, "message_id": mid, "ts": D + ts, "text_raw": en, "text_clean": en, "en_text": en,
            "unclear_spans": ["[unclear]"] if "[unclear]" in en else [], "domain_terms": [],
            "quantities_mentioned": [], "status": "ok", "attempts": 1, "error": None,
            "language": "en", "translation": "passthrough"}


def build() -> tuple[dict, dict]:
    pin = {"lat": 0.001, "lng": 0.002}
    clusters = [
        _cluster("C001", 1, ["M00001", "M00002", "M00003", "M00004"],
                 [("IMG-1.jpg", "M00002", "10:51:00"), ("IMG-2.jpg", "M00003", "10:52:00")],
                 [("PTT-1.opus", "M00004", "10:53:00")],
                 [("M00001", "10:50:00", "Arrived at the north block.")],
                 pin, "M00001", False, "10:50:00", "10:53:00", "first"),
        _cluster("C002", 2, ["M00005", "M00006", "M00007", "M00008"],
                 [("IMG-3.jpg", "M00005", "10:56:00"), ("IMG-4.jpg", "M00007", "10:58:00")],
                 [("PTT-2.opus", "M00006", "10:57:00"), ("PTT-3.opus", "M00008", "10:59:00")],
                 [], pin, "M00001", True, "10:56:00", "10:59:00", "gap"),
        _cluster("C003", 3, ["M00009"], [("IMG-5.jpg", "M00009", "11:10:00")], [], [],
                 None, None, False, "11:10:00", "11:10:00", "gap"),
    ]
    cl = {"status": "ok", "errors": [],
          "meta": _meta("s5_cluster", gap_threshold_sec=180, rule="test fixture", boundary_mode="pin_gap"),
          "clusters": clusters,
          "counts": {"clusters": 3, "messages_in": 9, "messages_assigned": 9, "messages_unassigned": 0,
                     "clusters_with_gps": 2, "clusters_without_gps": 1}}
    items = [_item("PTT-1.opus", "M00004", "10:53:00", "Two stair treads are cracked."),
             _item("PTT-2.opus", "M00006", "10:57:00", "This is the east stair and [unclear]."),
             _item("PTT-3.opus", "M00008", "10:59:00", "The gate is missing a hinge.")]
    tr = {"status": "ok", "errors": [],
          "meta": _meta("s4_translate", model="offline", prompt_version="test", schema_retries=0),
          "items": items,
          "counts": {"input_items": 3, "ok": 3, "partial": 0, "failed": 0, "skipped": 0}}
    return cl, tr


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    cl, tr = build()
    (OUT / "clusters.json").write_text(json.dumps(cl, indent=2) + "\n", encoding="utf-8")
    (OUT / "transcripts_en.json").write_text(json.dumps(tr, indent=2) + "\n", encoding="utf-8")
