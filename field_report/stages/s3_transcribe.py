#!/usr/bin/env python
"""s3_transcribe.py - stage 3: voice notes -> transcripts.json via an ASR backend.

    python -m field_report.stages.s3_transcribe --config <config> [--asr faster-whisper|sidecar]
                                                [--asr-model tiny] [--device auto|cpu|cuda] [--offline]

faster-whisper runs on CPU (int8) by default - no GPU, no key; the first run downloads
the model into the usual cache. `--asr sidecar` (implied by --offline) reads the
ground-truth `<note>.transcript.json` files the synthetic sample ships.

One bad note never stops the batch: it is recorded in errors[] and the stage exits 3.
Exit codes: 0 ok, 2 bad invocation/config/backend unavailable, 3 partial, 4 failed, 5 missing upstream.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable
from field_report.backends.asr import get_asr
from field_report.lib import common, stage_args

STAGE = "s3_transcribe"
MAX_ATTEMPTS = 3


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="stage 3: transcribe voice notes")
    stage_args.add_common(p)
    stage_args.add_offline(p)
    stage_args.add_asr(p)
    return p.parse_args(argv)


def transcribe_item(asr, entry: dict) -> dict:
    """One manifest audio entry -> one transcripts.json item (never raises BackendError)."""
    item = {"file": entry["filename"], "abs_path": entry["abs_path"], "message_id": entry["message_id"],
            "ts": entry["ts"], "sender": entry["sender"], "duration_sec": None, "language": None,
            "language_probability": None, "text": "", "segments": [], "status": "skipped",
            "attempts": 1, "error": None}
    if not entry["exists"]:
        item["error"] = {"code": "FILE_MISSING", "message": "audio file is not on disk", "retryable": False}
        return item
    last: BackendError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        item["attempts"] = attempt
        try:
            res = asr.transcribe(Path(entry["abs_path"]))
        except BackendError as e:
            last = e
            if not e.retryable:
                break
            continue
        item.update(duration_sec=res.duration, language=res.language,
                    language_probability=res.language_probability, text=res.text,
                    segments=[s.as_dict() for s in res.segments], error=None,
                    status="ok" if res.text.strip() else "empty_audio")
        return item
    item["status"] = "failed"
    item["error"] = {"code": last.code, "message": last.message, "retryable": last.retryable}
    return item


def _run(cfg: dict, run_dir: Path, log, args) -> int:
    asr = get_asr(args.asr, model=args.asr_model, device=args.device)
    settings = {"asr": args.asr, "model": getattr(asr, "model", None),
                "device_requested": args.device if args.asr == "faster-whisper" else "none"}
    out_path = run_dir / "transcripts.json"
    manifest = common.read_stage(run_dir, "media_manifest", "media_manifest")
    if not args.force and common.up_to_date(out_path, cfg, [run_dir / "media_manifest.json"], settings):
        print(f"SKIP {STAGE}: up to date")
        return 0

    audio = sorted((m for m in manifest["items"] if m["media_kind"] == "audio"),
                   key=lambda m: (m["ts"], m["line_no"]))
    t0 = time.monotonic()
    items = [transcribe_item(asr, m) for m in audio]
    wall = round(time.monotonic() - t0, 3)

    counts = {"input_files": len(audio)}
    for st in ("ok", "empty_audio", "failed", "skipped"):
        counts[st] = sum(1 for i in items if i["status"] == st)
    item_errors = [common.err(i["file"], i["error"]["code"], i["error"]["message"],
                              retryable=bool(i["error"].get("retryable")), attempts=i["attempts"])
                   for i in items if i["error"]]
    item_errors += [common.err(i["file"], "EMPTY_AUDIO", "no speech detected")
                    for i in items if i["status"] == "empty_audio"]

    total_audio = round(sum(i["duration_sec"] or 0 for i in items), 3)
    info = asr.info()
    load_s = round(getattr(asr, "model_load_seconds", 0.0), 3)
    payload = common.envelope(STAGE, cfg["site"], cfg, {"audio_files": len(audio)},
                              {"ok": counts["ok"], "failed": counts["failed"]})
    payload["meta"].update(
        backend=args.asr, model=str(info["model"]), device=info["device"],
        compute_type=str(info["compute_type"]), language=str(info["language"]),
        vad_filter=bool(info["vad_filter"]), model_load_seconds=load_s,
        total_audio_seconds=total_audio, wall_seconds=wall,
        realtime_factor=round(total_audio / (wall - load_s), 2) if wall - load_s > 0 and total_audio else None,
        settings=settings)
    payload["items"] = items
    payload["counts"] = counts
    code = common.finalize(payload, len(audio), counts["ok"], item_errors)
    try:
        common.write_stage(run_dir, "transcripts", payload, "transcripts")
    except ValueError as e:
        log.error("output schema invalid: %s", e)
        return 4
    log.info("transcribed %d/%d notes with %s (%s, %s) in %.1fs -> exit %d", counts["ok"], len(audio),
             args.asr, info["model"], info["device"], wall, code)
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    stage_args.apply_offline(args)
    try:
        cfg = common.load_config(args.config)
    except common.ConfigError as e:
        sys.stderr.write(f"CONFIG ERROR: {e}\n")
        return 2
    run_dir = common.resolve_run_dir(cfg, args.run_dir)
    log = common.get_logger(run_dir, STAGE, args.log_level)
    try:
        return _run(cfg, run_dir, log, args)
    except common.MissingUpstream as e:
        log.error("missing upstream: %s", e)
        return 5
    except BackendUnavailable as e:
        sys.stderr.write(f"BACKEND UNAVAILABLE: {e}\n")
        return 2
    except Exception:  # noqa: BLE001 - a crash is exit 4 with a traceback in logs/*.err
        tb = traceback.format_exc()
        (run_dir / "logs" / f"{STAGE}.err").write_text(tb, encoding="utf-8")
        log.error("unhandled exception:\n%s", tb)
        return 4


if __name__ == "__main__":
    sys.exit(main())
