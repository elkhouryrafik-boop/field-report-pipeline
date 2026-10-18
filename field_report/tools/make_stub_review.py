#!/usr/bin/env python
"""make_stub_review.py — emit the identity review for a run.

Reads clusters.json and writes a review.json that approves every stop and
changes nothing: no exclusions, no caption or text overrides, no bindings, no
splits, merges or discards. Applying it with s5c_apply_review.py must leave
clusters_verified.json structurally identical to clusters.json — that is the
regression check for the applier, and the way to exercise the whole verified
path before the s5b review UI exists.

    python -m field_report.tools.make_stub_review --config runs/<slug>/config.json

Exit codes: 0 ok, 2 bad invocation/config, 5 missing upstream.
"""

from __future__ import annotations

import argparse
import sys

from field_report.lib import common

STAGE = "s5b_review"


def stub_review(clusters: dict, cfg: dict, reviewer: str) -> dict:
    """Build the all-approved, no-edits decision log for these clusters."""
    env = common.envelope(STAGE, cfg["site"], cfg,
                          {"clusters": len(clusters["clusters"])},
                          {"stops": len(clusters["clusters"])})
    stops = [{
        "cluster_id": c["id"], "verdict": "approved",
        "reviewed_at": env["meta"]["generated_at"],
        "human_note": None, "gps_override": None, "split_at": None, "merge_with": None,
        "photos": [{"filename": p["filename"], "caption_override": None,
                    "excluded": False, "move_to": None} for p in c["photos"]],
        "audios": [{"file": a["filename"], "text_override": None, "en_override": None,
                    "binding": [], "binding_source": "none",
                    "dropped": False, "drop_reason": None} for a in c["audios"]],
    } for c in clusters["clusters"]]
    return {
        **env,
        "meta": {**env["meta"], "reviewer": reviewer,
                 "clusters_total": len(stops), "prelabels_sha256": None},
        "stops": stops,
        "counts": {"approved": len(stops), "discarded": 0, "pending": 0,
                   "bindings_accepted": 0, "bindings_corrected": 0, "bindings_manual": 0,
                   "photos_excluded": 0, "photos_moved": 0, "texts_edited": 0},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="emit an all-approved, no-edits review.json")
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--reviewer", default="stub")
    args = ap.parse_args(argv)

    cfg = common.load_config(args.config)
    run_dir = common.resolve_run_dir(cfg, args.run_dir)
    clusters = common.read_stage(run_dir, "clusters", "clusters")
    review = stub_review(clusters, cfg, args.reviewer)
    common.write_stage(run_dir, "review", review, "review")
    print(f"review.json: {len(review['stops'])} stops, all approved, no edits")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    except common.MissingUpstream as e:
        print(f"missing upstream: {e}", file=sys.stderr)
        sys.exit(5)
