#!/usr/bin/env python
"""s5_cluster.py (deterministic).

Group scoped messages into site clusters (a site = GPS pin + the photos /
PTTs / texts around it). Greedy single pass over scoped.json ordered by
(ts, line_no).
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from field_report.lib import common

STAGE = "s5_cluster"
DEFAULT_GAP_SECONDS = 180


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="cluster scoped messages by GPS pin + time gap")
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS,
                    help="pin/gap mode only: silence that ends a stop")
    p.add_argument("--dot-gap-seconds", type=int, default=common.DOT_MODE_GAP_SECONDS,
                    help="dot mode only: silence that ends an asset when the inspector "
                         "forgot the closing dot")
    return p.parse_args(argv)


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")


def cluster_messages(messages: list[dict], manifest_by_msgid: dict, gap_seconds: int,
                      dot_mode: bool | None = None,
                      dot_gap_seconds: int | None = None) -> list[dict]:
    """Greedy single pass. Returns the list of cluster dicts (schema shape).

    Two boundary rules, chosen by the export itself:

    * **dot mode** — the export carries at least one lone "." separator.
      Everything between two dots is one asset, pins included, so neither a pin
      nor the clock opens a stop. Pins inside the block are that asset's
      locations (`gps_points`) and each photo remembers the pin it was taken at.
      `dot_gap_seconds` is a safety net against a forgotten dot, nothing more.
    * **pin/gap mode** — no dot anywhere (an export without the separator convention):
      the original rule, unchanged.
    """
    ordered = sorted(messages, key=lambda m: (parse_ts(m["ts"]), m["line_no"]))
    if dot_mode is None:
        dot_mode = common.uses_dot_convention(ordered)
    if dot_gap_seconds is None:
        dot_gap_seconds = common.DOT_MODE_GAP_SECONDS
    boundary_gap = dot_gap_seconds if dot_mode else gap_seconds

    clusters: list[dict] = []
    cur: dict | None = None
    prev_ts: datetime | None = None
    last_pin_gps: dict | None = None
    last_pin_message_id: str | None = None

    def open_cluster(reason: str, gps, gps_inherited: bool, gps_message_id: str | None):
        nonlocal cur
        cur = {
            "seq": len(clusters) + 1,
            "boundary_reason": reason,
            "gps": gps,
            "gps_inherited": gps_inherited,
            "gps_message_id": gps_message_id,
            "gps_points": [],
            "message_ids": [],
            "photos": [],
            "audios": [],
            "texts": [],
            "omitted_count": 0,
            "senders": [],
            "start_ts": None,
            "end_ts": None,
        }
        clusters.append(cur)

    pending_separator = False

    for m in ordered:
        ts_dt = parse_ts(m["ts"])
        is_location = m["kind"] == "location"
        separator = common.is_separator(m)

        # A pin normally carries coordinates. It does not when s1 could not parse
        # the ?q= query: the message is still kind="location" (it still bounds a
        # stop) but gps is null. Such a pin must not become the inherited pin, or
        # every stop after it loses its coordinates until the next good one.
        pin_gps = m["gps"] if is_location else None
        if pin_gps:
            opening_gps, opening_inherited, opening_msg = pin_gps, False, m["id"]
        else:
            opening_gps = last_pin_gps
            opening_inherited = last_pin_gps is not None
            opening_msg = last_pin_message_id

        if cur is None:
            # The window may open ON a pin — a inspector who drops it on arrival,
            # or a date_from tuned to the first pin. Taking the coordinates here
            # is what starts inheritance at all.
            open_cluster("first", opening_gps, opening_inherited, opening_msg)
        elif is_location and not dot_mode:
            open_cluster("new_pin", opening_gps, opening_inherited, opening_msg)
        elif pending_separator:
            # The inspector said this asset is finished. That is a stronger signal
            # than either the clock or the pin, so it takes precedence over the
            # gap rule below.
            open_cluster("separator", last_pin_gps, last_pin_gps is not None, last_pin_message_id)
        elif prev_ts is not None and (ts_dt - prev_ts).total_seconds() > boundary_gap:
            open_cluster("gap", last_pin_gps, last_pin_gps is not None, last_pin_message_id)

        if pin_gps:
            last_pin_gps, last_pin_message_id = pin_gps, m["id"]

        pending_separator = False
        cur["message_ids"].append(m["id"])
        if cur["start_ts"] is None:
            cur["start_ts"] = m["ts"]
        cur["end_ts"] = m["ts"]
        if m["sender"] and m["sender"] not in cur["senders"]:
            cur["senders"].append(m["sender"])

        if pin_gps:
            # One asset can be pinned more than once — a road, a network, a
            # building with a detached annex. Every pin is kept; the first real one
            # leads and displaces anything inherited from an earlier stop.
            cur["gps_points"].append({"lat": pin_gps["lat"], "lng": pin_gps["lng"],
                                       "message_id": m["id"], "ts": m["ts"]})
            if cur["gps"] is None or cur["gps_inherited"]:
                cur["gps"] = {"lat": pin_gps["lat"], "lng": pin_gps["lng"]}
                cur["gps_message_id"] = m["id"]
                cur["gps_inherited"] = False

        if m["kind"] == "media":
            mi = manifest_by_msgid.get(m["id"])
            media_kind = mi["media_kind"] if mi else None
            entry = {
                "filename": m.get("media_filename") or (mi["filename"] if mi else None),
                "message_id": m["id"],
                "ts": m["ts"],
                "exists": bool(mi["exists"]) if mi else False,
            }
            if media_kind == "photo":
                # Which pin this photo was taken at. The report caption prints
                # this one rather than the asset's primary pin — dropping a
                # second pin mid-asset is exactly how an inspector says "these next photos
                # are somewhere else".
                entry["pin_message_id"] = (cur["gps_points"][-1]["message_id"]
                                            if cur["gps_points"] else None)
                cur["photos"].append(entry)
            elif media_kind == "audio":
                cur["audios"].append(entry)
            # other media kinds (document/contact/video/other) are tracked
            # only via message_ids, matching how system messages are handled
        elif m["kind"] == "text":
            # The separator is bookkeeping, not report context: it stays in
            # message_ids so no message is lost, but never reaches the card or
            # the classifier as chat text.
            if not separator:
                cur["texts"].append({
                    "message_id": m["id"], "ts": m["ts"], "sender": m["sender"], "text": m["text"],
                })
        elif m["kind"] == "media_omitted":
            cur["omitted_count"] += 1

        if separator:
            pending_separator = True
        prev_ts = ts_dt

    for i, c in enumerate(clusters, start=1):
        c["id"] = f"C{i:03d}"
        c["seq"] = i

    return clusters


def _is_up_to_date(out_path: Path, cfg: dict, upstream_paths: list[Path]) -> bool:
    if not out_path.is_file():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if existing.get("status") != "ok":
        return False
    if existing.get("meta", {}).get("config_sha256") != common.config_sha256(cfg):
        return False
    out_mtime = out_path.stat().st_mtime
    for p in upstream_paths:
        if not p.exists() or p.stat().st_mtime > out_mtime:
            return False
    return True


def _run(cfg: dict, run_dir: Path, logger, args) -> int:
    out_path = run_dir / "clusters.json"
    scoped_path = run_dir / "scoped.json"
    manifest_path = run_dir / "media_manifest.json"

    if not args.force and _is_up_to_date(out_path, cfg, [scoped_path, manifest_path]):
        print(f"SKIP {STAGE}: up to date")
        return 0

    scoped = common.read_stage(run_dir, "scoped", "scoped")
    manifest = common.read_stage(run_dir, "media_manifest", "media_manifest")
    messages = scoped["messages"]
    manifest_by_msgid = {it["message_id"]: it for it in manifest["items"]}

    n_in = len(messages)
    # Decided once, here, so the artefact's meta can never disagree with the
    # rule that actually ran.
    dot_mode = common.uses_dot_convention(messages)
    clusters = cluster_messages(messages, manifest_by_msgid, args.gap_seconds,
                                 dot_mode=dot_mode, dot_gap_seconds=args.dot_gap_seconds)

    messages_assigned = sum(len(c["message_ids"]) for c in clusters)
    messages_unassigned = n_in - messages_assigned

    payload = common.envelope(STAGE, cfg["site"], cfg,
                               input_counts={"messages_in": n_in},
                               output_counts={"clusters": len(clusters)})
    payload["meta"]["boundary_mode"] = "dot" if dot_mode else "pin_gap"
    payload["meta"]["gap_threshold_sec"] = args.dot_gap_seconds if dot_mode else args.gap_seconds
    payload["meta"]["rule"] = (
        ("the export uses the separator convention: a new cluster starts only after a lone "
         "'.', or after a gap > gap_threshold_sec (a forgotten dot). Everything between two "
         "dots is one asset; pins inside it are that asset's locations, listed in gps_points, "
         "and each photo records the pin it was taken at. Order by ts then line_no.")
        if dot_mode else
        ("new cluster when the previous message was a separator dot, OR a "
         "location message arrives, OR gap > gap_threshold_sec; order by ts "
         "then line_no. A pin without parsable coordinates bounds a stop but "
         "does not replace the inherited pin."))
    payload["clusters"] = clusters
    payload["counts"] = {
        "clusters": len(clusters),
        "messages_in": n_in,
        "messages_assigned": messages_assigned,
        "messages_unassigned": messages_unassigned,
        "clusters_with_gps": sum(1 for c in clusters if c["gps"] is not None),
        "clusters_without_gps": sum(1 for c in clusters if c["gps"] is None),
        "clusters_multi_pin": sum(1 for c in clusters if len(c["gps_points"]) > 1),
        "pins_total": sum(len(c["gps_points"]) for c in clusters),
    }

    if messages_unassigned != 0:
        payload["status"] = "failed"
        payload["errors"].append(common.err("<stage>", "EMPTY_RESULT_WITH_INPUTS",
                                             f"{messages_unassigned} messages unassigned to any cluster"))
        logger.error("messages_unassigned=%d (must be 0)", messages_unassigned)
        try:
            common.write_stage(run_dir, "clusters", payload, "clusters")
        except ValueError as e:
            logger.error("clusters.json schema invalid: %s", e)
        return 4

    code = common.finalize(payload, n_in=n_in, n_out=len(clusters), item_errors=[])

    try:
        common.write_stage(run_dir, "clusters", payload, "clusters")
    except ValueError as e:
        logger.error("output schema invalid: %s", e)
        return 4

    logger.info("clustered %d messages into %d clusters (%s mode, gap=%ds, %d pins, "
                "%d stops pinned more than once) -> exit %d",
                n_in, len(clusters), payload["meta"]["boundary_mode"],
                payload["meta"]["gap_threshold_sec"], payload["counts"]["pins_total"],
                payload["counts"]["clusters_multi_pin"], code)
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = common.load_config(args.config)
    except common.ConfigError as e:
        sys.stderr.write(f"CONFIG ERROR: {e}\n")
        return 2

    run_dir = common.resolve_run_dir(cfg, args.run_dir)
    logger = common.get_logger(run_dir, STAGE, args.log_level)
    try:
        return _run(cfg, run_dir, logger, args)
    except common.MissingUpstream as e:
        logger.error("missing upstream: %s", e)
        return 5
    except Exception:
        tb = traceback.format_exc()
        (run_dir / "logs" / f"{STAGE}.err").write_text(tb, encoding="utf-8")
        logger.error("unhandled exception:\n%s", tb)
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)
