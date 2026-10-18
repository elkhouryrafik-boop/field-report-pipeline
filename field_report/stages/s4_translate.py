#!/usr/bin/env python
"""s4_translate.py - stage 4: transcripts.json -> transcripts_en.json.

Cleans each transcript (whitespace only - the text is never rewritten), obtains the English
text through a translate backend, extracts the quantities that were actually spoken, and
reads the stop number the inspector opened the note with.

    python -m field_report.stages.s4_translate --config <config> [--translate whisper|none]
                                               [--asr ... --asr-model ... --device ...] [--offline]

`whisper` translates only notes whose detected language is not English (Whisper's
task="translate", or the sample's own `text_en` with the sidecar backend). `none` passes the text
through and marks a non-English note `partial`, so it is never passed off as translated.
Exit codes: 0 ok, 2 bad invocation/backend unavailable, 3 partial, 4 failed, 5 missing upstream.
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable
from field_report.backends.asr import get_asr
from field_report.backends.translate import get_translator
from field_report.lib import common, profiles, quantities, stage_args

STAGE = "s4_translate"
UNCLEAR = "[unclear]"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="stage 4: clean + translate transcripts")
    stage_args.add_common(p)
    stage_args.add_offline(p)
    stage_args.add_asr(p)
    stage_args.add_translate(p)
    return p.parse_args(argv)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def to_quantity(q: dict) -> dict:
    return {"unit": q["unit"], "value": q["value"], "value_text": q.get("value_text"),
            "confirmed": bool(q["confirmed"]), "verbatim": q.get("verbatim"), "note": q.get("note")}


def translate_item(src: dict, translator, stop_regex: str) -> tuple[dict, list[dict]]:
    """One transcripts.json item -> (transcripts_en item, item-level errors)."""
    base = {"file": src["file"], "message_id": src["message_id"], "ts": src["ts"],
            "text_raw": src["text"], "text_clean": "", "en_text": "", "unclear_spans": [],
            "domain_terms": [], "quantities_mentioned": [], "sector_declared": None,
            "language": src.get("language"), "status": "ok", "attempts": 1, "error": None}
    errs: list[dict] = []
    if src["status"] != "ok":
        base.update(status="skipped", error={"code": "SOURCE_NOT_OK",
                                             "message": f"transcript status {src['status']}"})
        return base, errs

    text = clean_text(src["text"])
    try:
        tr = translator.translate(text, src.get("language"), Path(src["abs_path"]))
    except BackendError as e:
        base.update(text_clean=text, status="failed", attempts=2,
                    error={"code": e.code, "message": e.message, "retryable": e.retryable})
        errs.append(common.err(src["file"], e.code, e.message, retryable=e.retryable, attempts=2))
        return base, errs

    en = clean_text(tr.en_text)
    quantities_found = [to_quantity(q) for q in quantities.extract_quantities(en)]
    base.update(text_clean=text, en_text=en, translation=tr.method,
                unclear_spans=[UNCLEAR] * en.count(UNCLEAR),
                quantities_mentioned=quantities_found,
                sector_declared=common.declared_sector(text, stop_regex)
                or common.declared_sector(en, stop_regex))
    if not tr.translated:
        msg = f"note is in {src.get('language')!r} and no translation backend ran; en_text is the source text"
        base.update(status="partial", error={"code": "NOT_TRANSLATED", "message": msg})
        errs.append(common.err(src["file"], "NOT_TRANSLATED", msg))
    return base, errs


def _run(cfg: dict, run_dir: Path, log, args) -> int:
    transcripts = common.read_stage(run_dir, "transcripts", "transcripts")
    prof = profiles.profile(cfg)
    settings = {"translate": args.translate, "asr": args.asr,
                "asr_model": args.asr_model if args.asr == "faster-whisper" else None}
    out_path = run_dir / "transcripts_en.json"
    if not args.force and common.up_to_date(out_path, cfg, [run_dir / "transcripts.json"], settings):
        print(f"SKIP {STAGE}: up to date")
        return 0

    asr = get_asr(args.asr, model=args.asr_model, device=args.device)
    translator = get_translator(args.translate, asr)

    items, item_errors = [], []
    for src in transcripts["items"]:
        item, errs = translate_item(src, translator, prof["stop_regex"])
        items.append(item)
        item_errors.extend(errs)
    items.sort(key=lambda i: (i["ts"], i["file"]))

    counts = {"input_items": len(items)}
    for st in ("ok", "partial", "failed", "skipped"):
        counts[st] = sum(1 for i in items if i["status"] == st)
    payload = common.envelope(STAGE, cfg["site"], cfg, {"transcripts": len(items)}, dict(counts))
    payload["meta"].update(model=args.translate, prompt_version="n/a", schema_retries=0, settings=settings)
    payload["items"] = items
    payload["counts"] = counts
    usable = counts["ok"] + counts["partial"]
    code = common.finalize(payload, len(items), usable, item_errors)
    try:
        common.write_stage(run_dir, "transcripts_en", payload, "transcripts_en")
    except ValueError as e:
        log.error("output schema invalid: %s", e)
        return 4
    log.info("transcripts_en: %s -> exit %d", counts, code)
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
