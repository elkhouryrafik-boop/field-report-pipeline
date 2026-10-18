#!/usr/bin/env python
"""s5a_pre_label.py - stage 5a: caption the photos and propose which photos each voice note is about.

    python -m field_report.stages.s5a_pre_label --config <config> [--captions sidecar|none|anthropic] [--offline]

Captions come from a vision backend (`sidecar` = the sample's ground truth, `anthropic` = the
experimental Claude backend). Bindings are deterministic: a voice note is bound to the photos
of its own stop whose caption shares the most words with what was said (ties bind to all of
them); a note that shares no words is bound to the nearest photo in time, with a low
confidence and a basis that says so. The output seeds the review gate (s5b) - a person, or
`s5b --auto`, accepts or corrects it.

Exit codes: 0 ok, 2 bad invocation/backend unavailable, 3 partial (some photos uncaptioned),
4 failed, 5 missing upstream.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from field_report.backends import BackendError, BackendUnavailable
from field_report.backends.vision import get_captioner
from field_report.lib import common, profiles, stage_args

STAGE = "s5a_pre_label"
PROMPT_VERSION = "1.0.0"

_STOP_WORDS = {
    "stop", "synthetic", "scene", "priority", "severity", "this", "that", "with", "from", "have",
    "there", "their", "about", "photo", "image", "picture", "below", "above", "near", "second",
    "angle", "view", "inside", "wider", "into", "onto", "very", "some", "does", "only",
}


def content_words(text: str) -> set[str]:
    words = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if len(w) > 3 and w not in _STOP_WORDS:
            words.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return words


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="stage 5a: caption photos, propose bindings")
    stage_args.add_common(p)
    stage_args.add_offline(p)
    stage_args.add_captions(p)
    return p.parse_args(argv)


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def propose_binding(audio: dict, transcript: dict, photos: list[dict], captions: dict[str, str | None]) -> dict | None:
    """Which photos of this stop does this voice note talk about? None if the stop has no photos."""
    if not photos:
        return None
    said = content_words(transcript["en_text"])
    scored = []
    for ph in photos:
        shared = sorted(said & content_words(captions.get(ph["filename"]) or ""))
        scored.append((len(shared), shared, ph))
    best = max(s[0] for s in scored)
    if best > 0:
        chosen = [s for s in scored if s[0] == best]
        words = sorted({w for s in chosen for w in s[1]})
        return {"audio": audio["filename"], "photos": [s[2]["filename"] for s in chosen],
                "confidence": round(min(0.95, 0.5 + 0.15 * best), 2),
                "basis": "shared words: " + ", ".join(words)}
    a_ts = _ts(audio["ts"])
    nearest = min(photos, key=lambda ph: abs((_ts(ph["ts"]) - a_ts).total_seconds()))
    return {"audio": audio["filename"], "photos": [nearest["filename"]], "confidence": 0.3,
            "basis": f"no caption words in common; nearest photo in time ({nearest['filename']})"}


def _run(cfg: dict, run_dir: Path, log, args) -> int:
    prof = profiles.profile(cfg)
    clusters = common.read_stage(run_dir, "clusters", "clusters")
    manifest = common.read_stage(run_dir, "media_manifest", "media_manifest")
    tr_en = common.read_stage(run_dir, "transcripts_en", "transcripts_en")
    if args.captions == "auto":  # sidecar captions if the export ships them, otherwise none
        has_sidecars = any(Path(cfg["media_dir"]).glob("*.caption.json"))
        args.captions = "sidecar" if has_sidecars else "none"
    settings = {"captions": args.captions, "profile": prof["name"]}
    out_path = run_dir / "prelabels.json"
    ups = [run_dir / "clusters.json", run_dir / "media_manifest.json", run_dir / "transcripts_en.json"]
    if not args.force and common.up_to_date(out_path, cfg, ups, settings):
        print(f"SKIP {STAGE}: up to date")
        return 0

    captioner = get_captioner(args.captions, profile=prof)
    media = {m["filename"]: m for m in manifest["items"]}
    names = {c["name"].lower(): c["id"] for c in prof["categories"]}
    names.update({c["short"].lower(): c["id"] for c in prof["categories"]})
    usable = {i["file"]: i for i in tr_en["items"] if i["status"] in ("ok", "partial")}

    rows, item_errors, captions = [], [], {}
    n_in = 0
    for c in clusters["clusters"]:
        for ph in c["photos"]:
            n_in += 1
            m = media.get(ph["filename"])
            row = {"filename": ph["filename"], "cluster_id": c["id"], "caption": None, "sector_guess": None,
                   "sector_confidence": None, "visible_damage": None, "countable": None, "rating_guess": None}
            if not (m and m.get("exists")):
                item_errors.append(common.err(ph["filename"], "PHOTO_MISSING", "photo is not on disk"))
            else:
                try:
                    cap = captioner.caption(Path(m["abs_path"]))
                except BackendError as e:
                    item_errors.append(common.err(ph["filename"], e.code, e.message, retryable=e.retryable))
                else:
                    row["caption"] = cap.caption
                    row["visible_damage"] = cap.visible_damage
                    row["countable"] = cap.countable
                    row["rating_guess"] = cap.rating
                    cid = names.get((cap.category or "").lower())
                    if cid is not None:
                        row["sector_guess"], row["sector_confidence"] = cid, 0.9
                    captions[ph["filename"]] = cap.caption
            rows.append(row)

    bindings, audios_in = [], 0
    for c in clusters["clusters"]:
        for au in c["audios"]:
            t = usable.get(au["filename"])
            if not t:
                continue
            audios_in += 1
            b = propose_binding(au, t, c["photos"], captions)
            if b:
                bindings.append({"cluster_id": c["id"], **b})
    bound = {(b["cluster_id"], b["audio"]) for b in bindings}
    counts = {"photos_in": len(rows), "photos_captioned": sum(1 for r in rows if r["caption"]),
              "photos_failed": sum(1 for r in rows if not r["caption"]), "audios_in": audios_in,
              "bindings_proposed": len(bindings), "audios_unbound": audios_in - len(bound)}
    payload = common.envelope(STAGE, cfg["site"], cfg, {"clusters": len(clusters["clusters"]), "photos": n_in},
                              dict(counts))
    info = captioner.info()
    payload["meta"].update(model=str(info["model"]), prompt_version=str(info.get("prompt_version", PROMPT_VERSION)),
                           schema_retries=0, settings=settings)
    payload["photos"] = rows
    payload["bindings"] = bindings
    payload["counts"] = counts
    # --captions none is a deliberate choice, not a failure: uncaptioned photos are not errors then.
    if args.captions == "none":
        item_errors = []
    code = common.finalize(payload, n_in, len(rows), item_errors)
    try:
        common.write_stage(run_dir, "prelabels", payload, "prelabels")
    except ValueError as e:
        log.error("output schema invalid: %s", e)
        return 4
    log.info("prelabels: %s -> exit %d", counts, code)
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
        if getattr(e, "error", None):
            sys.stderr.write(json.dumps({"error": e.error}) + "\n")
        return 2
    except Exception:  # noqa: BLE001 - a crash is exit 4 with a traceback in logs/*.err
        tb = traceback.format_exc()
        (run_dir / "logs" / f"{STAGE}.err").write_text(tb, encoding="utf-8")
        log.error("unhandled exception:\n%s", tb)
        return 4


if __name__ == "__main__":
    sys.exit(main())
