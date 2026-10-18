#!/usr/bin/env python
"""s2_scope.py (deterministic).

Filter messages.json to the site visit window and inventory its media.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from field_report.lib import common

STAGE = "s2_scope"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="scope messages.json to the visit window")
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--hash-media", action="store_true",
                    help="fill sha256 per media item (slow; default off)")
    return p.parse_args(argv)


def parse_window_bound(date_s: str, end_of_day: bool) -> datetime:
    """YYYY-MM-DD (bare) expands to whole-day bounds; YYYY-MM-DDTHH:MM is exact."""
    y, mo, d = int(date_s[0:4]), int(date_s[5:7]), int(date_s[8:10])
    if len(date_s) == 10:
        return datetime(y, mo, d, 23, 59, 59) if end_of_day else datetime(y, mo, d, 0, 0, 0)
    h, mi = int(date_s[11:13]), int(date_s[14:16])
    return datetime(y, mo, d, h, mi, 0)


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")


def media_kind_for_ext(ext: str | None) -> str:
    if ext in common.PHOTO_EXT:
        return "photo"
    if ext in common.AUDIO_EXT:
        return "audio"
    if ext in common.DOC_EXT:
        return "document"
    if ext in common.CONTACT_EXT:
        return "contact"
    if ext in common.VIDEO_EXT:
        return "video"
    return "other"


def build_disk_index(media_dir: Path) -> dict[str, str]:
    """lowercased filename -> actual on-disk filename (case-insensitive lookup)."""
    index = {}
    for entry in os.listdir(media_dir):
        p = media_dir / entry
        if p.is_file():
            index[entry.lower()] = entry
    return index


def build_media_manifest(window_messages: list[dict], media_dir: Path, hash_media: bool,
                          logger) -> tuple[dict, list[dict]]:
    disk_index = build_disk_index(media_dir)
    items = []
    counts = {"total": 0, "present": 0, "missing": 0, "photo": 0, "audio": 0,
              "document": 0, "contact": 0, "video": 0, "other": 0}
    for m in window_messages:
        if m["kind"] != "media":
            continue
        filename = m["media_filename"]
        ext = m.get("media_ext")
        disk_name = disk_index.get(filename.lower()) if filename else None
        exists = disk_name is not None
        abs_path = media_dir / (disk_name if exists else filename)
        mk = media_kind_for_ext(ext)
        size_bytes = None
        sha256 = None
        if exists:
            try:
                size_bytes = abs_path.stat().st_size
            except OSError:
                size_bytes = None
            if hash_media:
                try:
                    sha256 = common.sha256_file(abs_path)
                except OSError:
                    sha256 = None
        items.append({
            "message_id": m["id"],
            "filename": filename,
            "ext": ext,
            "media_kind": mk,
            "abs_path": str(abs_path),
            "exists": exists,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "ts": m["ts"],
            "sender": m["sender"],
            "line_no": m["line_no"],
        })
        counts["total"] += 1
        counts["present" if exists else "missing"] += 1
        counts[mk] = counts.get(mk, 0) + 1
    return {"items": items, "counts": counts}, items


def build_gaps(window_messages: list[dict], manifest_items: list[dict]) -> dict:
    gaps = []
    for m in window_messages:
        if m["kind"] == "media_omitted":
            note = None
            text = m.get("text", "")
            marker = "<Media omitted>"
            if text.startswith(marker):
                rest = text[len(marker):].lstrip("\n")
                note = rest if rest else None
            gaps.append({
                "message_id": m["id"], "ts": m["ts"], "sender": m["sender"],
                "line_no": m["line_no"], "reason": "media_omitted",
                "filename": None, "note": note,
            })
    for it in manifest_items:
        if not it["exists"]:
            gaps.append({
                "message_id": it["message_id"], "ts": it["ts"], "sender": it["sender"],
                "line_no": it["line_no"], "reason": "file_missing_on_disk",
                "filename": it["filename"], "note": None,
            })
    for m in window_messages:
        if m["kind"] == "media" and m.get("media_ext") is None:
            gaps.append({
                "message_id": m["id"], "ts": m["ts"], "sender": m["sender"],
                "line_no": m["line_no"], "reason": "unparsable_filename",
                "filename": m.get("media_filename"), "note": None,
            })
    counts = {
        "total": len(gaps),
        "media_omitted": sum(1 for g in gaps if g["reason"] == "media_omitted"),
        "file_missing_on_disk": sum(1 for g in gaps if g["reason"] == "file_missing_on_disk"),
        "unparsable_filename": sum(1 for g in gaps if g["reason"] == "unparsable_filename"),
        "unreadable_file": 0,
    }
    return {"gaps": gaps, "counts": counts}


def _is_up_to_date(out_paths: list[Path], cfg: dict, upstream_paths: list[Path]) -> bool:
    for out_path in out_paths:
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
    media_dir = Path(cfg["media_dir"])
    if not media_dir.is_dir():
        logger.error("config.media_dir does not exist: %s", media_dir)
        print(f"CONFIG ERROR: media_dir does not exist: {media_dir}", file=sys.stderr)
        return 2

    scoped_path = run_dir / "scoped.json"
    manifest_path = run_dir / "media_manifest.json"
    gaps_path = run_dir / "gaps.json"
    messages_path = run_dir / "messages.json"

    if not args.force and _is_up_to_date([scoped_path, manifest_path, gaps_path], cfg,
                                          [messages_path]):
        print(f"SKIP {STAGE}: up to date")
        return 0

    upstream = common.read_stage(run_dir, "messages", "messages")
    all_messages = upstream["messages"]
    n_in = len(all_messages)

    from_dt = parse_window_bound(cfg["date_from"], end_of_day=False)
    to_dt = parse_window_bound(cfg["date_to"], end_of_day=True)

    window_messages = [m for m in all_messages if from_dt <= parse_ts(m["ts"]) <= to_dt]
    n_out = len(window_messages)

    inspectors = set(cfg.get("inspectors", []))
    for m in window_messages:
        if m["sender"] and m["sender"] not in inspectors:
            logger.warning("sender not in config.inspectors: %s (message %s)", m["sender"], m["id"])

    counts = {
        "total": n_out,
        "text": sum(1 for m in window_messages if m["kind"] == "text"),
        "media": sum(1 for m in window_messages if m["kind"] == "media"),
        "location": sum(1 for m in window_messages if m["kind"] == "location"),
        "media_omitted": sum(1 for m in window_messages if m["kind"] == "media_omitted"),
        "system": sum(1 for m in window_messages if m["kind"] == "system"),
        "photos": sum(1 for m in window_messages
                       if m["kind"] == "media" and m.get("media_ext") in common.PHOTO_EXT),
        "audios": sum(1 for m in window_messages
                       if m["kind"] == "media" and m.get("media_ext") in common.AUDIO_EXT),
        "by_sender": {},
    }
    by_sender: dict[str, int] = {}
    for m in window_messages:
        if m["sender"]:
            by_sender[m["sender"]] = by_sender.get(m["sender"], 0) + 1
    counts["by_sender"] = by_sender

    scoped_config = {
        "site": cfg["site"],
        "site_slug": cfg["site_slug"],
        "profile": cfg["profile"],
        "date_from": cfg["date_from"],
        "date_to": cfg["date_to"],
        "inspectors": cfg.get("inspectors", []),
        "export_txt": cfg["export_txt"],
        "media_dir": cfg["media_dir"],
        "map_image": cfg.get("map_image"),
    }

    scoped_payload = common.envelope(STAGE, cfg["site"], cfg,
                                      input_counts={"messages_in": n_in},
                                      output_counts={"messages_out": n_out})
    scoped_payload["config"] = scoped_config
    scoped_payload["window"] = {
        "date_from": cfg["date_from"],
        "date_to": cfg["date_to"],
        "first_ts": window_messages[0]["ts"] if window_messages else None,
        "last_ts": window_messages[-1]["ts"] if window_messages else None,
    }
    scoped_payload["messages"] = window_messages
    scoped_payload["counts"] = counts

    scoped_code = common.finalize(scoped_payload, n_in=n_in, n_out=n_out, item_errors=[])
    if scoped_code == 4:
        # zero messages in window: still write nothing further, report failure
        try:
            common.write_stage(run_dir, "scoped", scoped_payload, "scoped")
        except ValueError as e:
            logger.error("scoped.json schema invalid: %s", e)
        logger.error("zero messages in window %s..%s -> EMPTY_RESULT_WITH_INPUTS",
                      cfg["date_from"], cfg["date_to"])
        return 4

    manifest_body, manifest_items = build_media_manifest(window_messages, media_dir,
                                                           args.hash_media, logger)
    manifest_n_in = counts["media"]
    manifest_n_out = len(manifest_body["items"])
    manifest_payload = common.envelope(STAGE, cfg["site"], cfg,
                                        input_counts={"media_messages": manifest_n_in},
                                        output_counts={"media_items": manifest_n_out})
    manifest_payload["media_dir"] = str(media_dir)
    manifest_payload["items"] = manifest_body["items"]
    manifest_payload["counts"] = manifest_body["counts"]
    manifest_item_errors = [
        common.err(it["message_id"], "FILE_MISSING", f"media file missing on disk: {it['filename']}")
        for it in manifest_items if not it["exists"]
    ]
    manifest_code = common.finalize(manifest_payload, n_in=manifest_n_in, n_out=manifest_n_out,
                                     item_errors=manifest_item_errors)

    gaps_body = build_gaps(window_messages, manifest_items)
    gaps_payload = common.envelope(STAGE, cfg["site"], cfg,
                                    input_counts={"messages_out": n_out},
                                    output_counts={"gaps": len(gaps_body["gaps"])})
    gaps_payload["gaps"] = gaps_body["gaps"]
    gaps_payload["counts"] = gaps_body["counts"]
    gaps_code = common.finalize(gaps_payload, n_in=n_out, n_out=len(gaps_body["gaps"]),
                                 item_errors=[], empty_ok=True)

    try:
        common.write_stage(run_dir, "scoped", scoped_payload, "scoped")
        common.write_stage(run_dir, "media_manifest", manifest_payload, "media_manifest")
        common.write_stage(run_dir, "gaps", gaps_payload, "gaps")
    except ValueError as e:
        logger.error("output schema invalid: %s", e)
        return 4

    logger.info("scoped %d/%d messages window=%s..%s; media_manifest %d items (%d missing); "
                "gaps %d -> scoped_code=%d manifest_code=%d gaps_code=%d",
                n_out, n_in, cfg["date_from"], cfg["date_to"], manifest_n_out,
                manifest_body["counts"]["missing"], len(gaps_body["gaps"]),
                scoped_code, manifest_code, gaps_code)

    return max(scoped_code, manifest_code, gaps_code)


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
