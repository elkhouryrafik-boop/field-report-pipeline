#!/usr/bin/env python
"""Run driver: chains the stages for one site and records what happened.

    python -m field_report run --config examples/sample/config.json --offline --auto
    python -m field_report run --config <config> --asr faster-whisper --asr-model tiny --auto
    python -m field_report run --config <config> --status
    python -m field_report run --config <config> --only s3,s4

Stages, in order:  s1 parse -> s2 scope -> s3 ASR -> s4 translate -> s5 cluster -> s5a captions
                   -> [gate s5b review] -> s5c apply -> s6 classify -> s7 report -> s8 quantities
                   -> s9 review sheet -> s10 readiness gate

`--offline` selects the deterministic backends (--asr sidecar --captions sidecar --classify keyword)
for any you did not name: no model, no network. A backend named explicitly always wins. `--auto` clears the one human gate (s5b) by accepting every stop and its
pre-labels; each is marked "auto-accepted, unreviewed" and the report says so. Without `--auto` the
driver stops at the gate (exit 0, with instructions) until review.json has no pending stop.

Every stage is idempotent: re-running skips a stage whose output is current for this config and
these backend settings (`--force` redoes everything).

Exit codes: 0 reached the end (or the review gate) cleanly, 2 bad invocation / config / backend
unavailable, 3 a stage reported item-level problems (the run continues; the code is the worst seen),
4 a stage failed, 5 a stage found its upstream artifact missing.

The driver never inspects prose to decide anything: stage exit codes and artifact status fields
are the only control signals. The record of the run is `run-manifest.json` in the run directory.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from field_report import __version__
from field_report.lib import common, profiles, stage_args

MANIFEST_NAME = "run-manifest.json"

# (name, module, takes: which option groups the stage understands)
STAGES = [
    ("s1", "s1_parse_chat", {"force"}),
    ("s2", "s2_scope", {"force"}),
    ("s3", "s3_transcribe", {"force", "offline", "asr"}),
    ("s4", "s4_translate", {"force", "offline", "asr", "translate"}),
    ("s5", "s5_cluster", {"force"}),
    ("s5a", "s5a_pre_label", {"force", "offline", "captions"}),
    ("gate:s5b", None, set()),
    ("s5c", "s5c_apply_review", set()),
    ("s6", "s6_classify", {"force", "offline", "classify"}),
    ("s7", "s7_build_report", {"force"}),
    ("s8", "s8_build_quantities", {"force"}),        # writes nothing unless the profile asks for a quantities sheet
    ("s9", "s9_build_confirmation_sheet", {"force"}),
    ("s10", "s10_verify_build", set()),
]
LAST_STAGE = "s10"
# Stages that exist only to serve the review gate: waiving it waives them too.
GATE_STAGES = {"s5c": "s5b"}
STAGE_NAMES = [n for n, _, _ in STAGES if not n.startswith("gate:")]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_manifest(run_dir: Path) -> dict:
    p = run_dir / MANIFEST_NAME
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"stages": {}, "gates": {}, "history": []}


def save_manifest(run_dir: Path, manifest: dict) -> None:
    p = run_dir / MANIFEST_NAME
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def record(run_dir: Path, manifest: dict, stage: str, code: int, seconds: float, note: str = "") -> None:
    status = {0: "ok", 3: "partial"}.get(code, "failed")
    manifest["stages"][stage] = {"status": status, "exit_code": code, "at": now(),
                                 "seconds": round(seconds, 2), "note": note}
    manifest["history"].append({"stage": stage, "status": status, "at": now()})
    save_manifest(run_dir, manifest)


def record_skip(run_dir: Path, manifest: dict, stage: str, note: str) -> None:
    manifest["stages"][stage] = {"status": "skipped", "at": now(), "note": note}
    manifest["history"].append({"stage": stage, "status": "skipped", "at": now()})
    save_manifest(run_dir, manifest)


def gate_skipped(manifest: dict, gate: str) -> bool:
    state = manifest.get("gates", {}).get(gate)
    return isinstance(state, dict) and bool(state.get("skipped"))


def review_complete(run_dir: Path) -> bool:
    """True when every stop in review.json carries a verdict (counts.pending == 0)."""
    try:
        review = json.loads((run_dir / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    counts = review.get("counts")
    return isinstance(counts, dict) and counts.get("pending") == 0


def call_stage(module: str, argv: list[str]) -> int:
    """Run one stage in-process and return its exit code (a crash is code 4)."""
    mod = importlib.import_module(f"field_report.stages.{module}")
    try:
        code = mod.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 4)
    except Exception as e:  # noqa: BLE001 - a stage crash is a failed stage, not a crashed driver
        print(f"stage crashed: {type(e).__name__}: {e}", file=sys.stderr)
        code = 4
    return int(code or 0)


def stage_argv(takes: set[str], args, config: str, run_dir: Path) -> list[str]:
    argv = ["--config", config, "--run-dir", str(run_dir)]
    if "force" in takes and args.force:
        argv.append("--force")
    if "offline" in takes and args.offline:
        argv.append("--offline")
    if "asr" in takes:
        argv += ["--asr", args.asr, "--asr-model", args.asr_model, "--device", args.device]
    if "translate" in takes:
        argv += ["--translate", args.translate]
    if "captions" in takes:
        argv += ["--captions", args.captions]
    if "classify" in takes:
        argv += ["--classify", args.classify]
    return argv


def print_gate(config: str) -> None:
    print("\nGATE s5b - human review of every stop (transcripts, captions, transcript->photo bindings).")
    print(f"  1. python -m field_report review --config {config}")
    print("     (serves the review app on 127.0.0.1 only; verdicts autosave to review.json)")
    print(f"  2. re-run: python -m field_report run --config {config}")
    print("  Or, with no human in the loop (every finding is then marked unreviewed):")
    print(f"     python -m field_report run --config {config} --auto")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m field_report run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", default=None, help="where artifacts go (default out/runs/<site_slug>)")
    ap.add_argument("--only", default=None, help="comma-separated stages to run: " + ",".join(STAGE_NAMES))
    ap.add_argument("--until", default=LAST_STAGE, choices=STAGE_NAMES, help="last stage to run")
    ap.add_argument("--status", action="store_true", help="print run-manifest.json and exit")
    ap.add_argument("--auto", action="store_true",
                    help="clear the review gate automatically (stops are marked auto-accepted, unreviewed)")
    ap.add_argument("--skip-gate", action="append", default=[], choices=["s5b"], metavar="GATE",
                    help="waive the review gate; recorded in the manifest, evidence ships unverified")
    stage_args.add_offline(ap)
    stage_args.add_asr(ap)
    stage_args.add_translate(ap)
    stage_args.add_captions(ap)
    stage_args.add_classify(ap)
    ap.add_argument("--force", action="store_true", help="redo every stage even if its output is current")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stage_args.apply_offline(args)

    try:
        cfg = common.load_config(args.config)
    except (common.ConfigError, profiles.ProfileError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    run_dir = common.resolve_run_dir(cfg, args.run_dir).resolve()
    manifest = load_manifest(run_dir)
    manifest.update(tool="field-report-pipeline", version=__version__, site=cfg["site"],
                    profile=cfg["profile"], auto=bool(args.auto), offline=bool(args.offline),
                    backends={"asr": args.asr, "asr_model": args.asr_model if args.asr == "faster-whisper" else None,
                              "device": args.device if args.asr == "faster-whisper" else None,
                              "translate": args.translate, "captions": args.captions,
                              "classify": args.classify})

    if args.status:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    bad = sorted((only or set()) - set(STAGE_NAMES))
    if bad:
        print(f"--only: unknown stage(s) {bad}; choose from {STAGE_NAMES}", file=sys.stderr)
        return 2

    for gate in args.skip_gate:
        manifest["gates"][gate] = {"skipped": True, "at": now()}
        print(f"gate {gate}: SKIPPED by request - recorded; evidence that would have passed it ships unverified.")
    save_manifest(run_dir, manifest)

    worst = 0
    last_index = STAGE_NAMES.index(args.until)
    for name, module, takes in STAGES:
        if name.startswith("gate:"):
            gate = name.split(":", 1)[1]
            if only is not None:
                continue
            if gate_skipped(manifest, gate):
                continue
            if args.auto:
                code = call_stage("s5b_review", stage_args_for_auto(args.config, run_dir))
                record(run_dir, manifest, "s5b", code, 0.0, "auto-accepted, unreviewed")
                if code != 0:
                    print(f"\nSTOP: stage s5b exited {code}", file=sys.stderr)
                    return code
                manifest["gates"][gate] = "auto"
                save_manifest(run_dir, manifest)
                continue
            if not review_complete(run_dir):
                manifest["gates"][gate] = "open"
                save_manifest(run_dir, manifest)
                print_gate(args.config)
                return worst
            manifest["gates"][gate] = "done"
            save_manifest(run_dir, manifest)
            continue

        if STAGE_NAMES.index(name) > last_index:
            break
        if only is not None and name not in only:
            continue
        owner = GATE_STAGES.get(name)
        if owner and only is None and gate_skipped(manifest, owner):
            print(f"\n=== {name} === skipped: gate {owner} was waived")
            record_skip(run_dir, manifest, name, f"gate {owner} was waived")
            continue

        print(f"\n=== {name} ===")
        t0 = time.monotonic()
        code = call_stage(module, stage_argv(takes, args, args.config, run_dir))
        record(run_dir, manifest, name, code, time.monotonic() - t0)
        if code in (0, 3):
            worst = max(worst, code)
            continue
        print(f"\nSTOP: stage {name} exited {code} - see {run_dir / 'logs'}", file=sys.stderr)
        return code

    manifest["completed_through"] = args.until if only is None else sorted(only)
    manifest["finished_at"] = now()
    save_manifest(run_dir, manifest)
    print(f"\nRun complete through {args.until} for {cfg['site']}: {run_dir}")
    return worst


def stage_args_for_auto(config: str, run_dir: Path) -> list[str]:
    return ["--config", config, "--run-dir", str(run_dir), "--auto"]


if __name__ == "__main__":
    sys.exit(main())
