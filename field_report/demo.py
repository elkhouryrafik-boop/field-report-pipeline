"""`python -m field_report demo` - the whole pipeline on the synthetic sample, in one command.

    python -m field_report demo --offline                       # no model, no network, a few seconds
    python -m field_report demo --offline --profile damage-assessment
    python -m field_report demo --offline --profile event-log
    python -m field_report demo --asr-model tiny                # real faster-whisper on CPU (one-time ~75 MB download)

Steps: make the sample if it is missing -> s1 parse -> s2 scope -> s3 ASR -> s4 translate -> s5 cluster
-> s5a captions -> s5b review (--auto: every stop accepted and marked unreviewed) -> s5c apply
-> s6 classify -> s7 report -> s8 quantities (damage-assessment only) -> s9 review sheet -> s10 gate.

Writes to --out (default out/demo):
    report.docx  review-sheet.docx  [quantities.xlsx]  run-manifest.json
and keeps every intermediate artifact in <out>/work/.

`--offline` = `--asr sidecar --captions sidecar --classify keyword`: the transcripts and captions that
ship with the sample, no model, no network. Exit code = the worst stage exit code (0 ok, 3 item-level
problems, 4 the s10 gate failed, ...).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from field_report import __version__, run
from field_report.lib import common, profiles, stage_args

REPO = common.REPO
SAMPLE_FOR = {"site-inspection": "sample", "event-log": "sample", "damage-assessment": "sample-damage"}
DELIVERABLES = ("report.docx", "review-sheet.docx", "quantities.xlsx")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m field_report demo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="site-inspection", help="site-inspection | damage-assessment | event-log")
    ap.add_argument("--out", default="out/demo", help="output folder (default out/demo)")
    ap.add_argument("--sample", default=None, help="a sample folder with config.json (default: the bundled one for the profile)")
    ap.add_argument("--force", action="store_true", help="redo every stage")
    stage_args.add_offline(ap)
    stage_args.add_asr(ap)
    stage_args.add_translate(ap)
    stage_args.add_captions(ap)
    stage_args.add_classify(ap)
    return ap


def ensure_sample(profile: str, sample: str | None) -> Path:
    """Path of the sample's config.json; the sample is generated when it is not there."""
    folder = Path(sample) if sample else REPO / "examples" / SAMPLE_FOR.get(profile, "sample")
    cfg = folder / "config.json"
    if cfg.is_file():
        return cfg
    script = REPO / "examples" / "make_sample.py"
    if not script.is_file():
        raise SystemExit(f"sample not found at {folder} and examples/make_sample.py is not available")
    base = "damage-assessment" if profile == "damage-assessment" else "site-inspection"
    print(f"sample missing - generating {folder} ...")
    subprocess.run([sys.executable, str(script), "--out", str(folder), "--seed", "7", "--tts", "none",
                    "--profile", base], check=True)
    return cfg


def derive_config(src: Path, profile: str, work: Path) -> Path:
    """The sample config with the chosen profile and absolute paths, next to a copy of the sample manifest."""
    cfg = json.loads(src.read_text(encoding="utf-8"))
    cfg["profile"] = profile
    for key in ("export_txt", "media_dir", "map_image"):
        if key in cfg:
            cfg[key] = str((src.parent / cfg[key]).resolve())
    work.mkdir(parents=True, exist_ok=True)
    out = work / "config.json"
    out.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    manifest = src.parent / "sample_manifest.json"
    if manifest.is_file():
        shutil.copy2(manifest, work / "sample_manifest.json")     # marks the run as synthetic
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profiles.load(args.profile)
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    stage_args.apply_offline(args)
    out = Path(args.out).resolve()
    work = out / "work"
    t0 = time.monotonic()
    try:
        src = ensure_sample(args.profile, args.sample)
    except (SystemExit, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    cfg_path = derive_config(src, args.profile, work)

    argv_run = ["--config", str(cfg_path), "--run-dir", str(work), "--auto", "--until", "s10",
                "--asr", args.asr, "--asr-model", args.asr_model, "--device", args.device,
                "--translate", args.translate, "--captions", args.captions, "--classify", args.classify]
    if args.offline:
        argv_run.append("--offline")
    if args.force:
        argv_run.append("--force")
    code = run.main(argv_run)

    cfg = common.load_config(str(cfg_path))
    prof = profiles.profile(cfg)
    copied = []
    if code in (0, 3):
        wanted = ["report.docx", "review-sheet.docx"] + (["quantities.xlsx"] if prof.get("quantities_sheet") else [])
        for name in DELIVERABLES:
            stale = out / name
            if name not in wanted and stale.exists():
                stale.unlink()                      # a previous run of another profile
        for name in wanted:
            src_file = work / name
            if src_file.is_file():
                shutil.copy2(src_file, out / name)
                copied.append(name)
    manifest = run.load_manifest(work)
    manifest.update(command="demo", demo_exit_code=code, deliverables=copied,
                    seconds=round(time.monotonic() - t0, 1), tool_version=__version__)
    (out / run.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                                         encoding="utf-8")
    run.save_manifest(work, manifest)
    print(f"\ndemo ({args.profile}) exit {code} in {time.monotonic() - t0:.1f}s")
    for name in copied + [run.MANIFEST_NAME]:
        print(f"  {out / name}")
    return code


if __name__ == "__main__":
    sys.exit(main())
