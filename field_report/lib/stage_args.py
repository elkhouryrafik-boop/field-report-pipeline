"""Command-line options shared by the backend-driven stages (s3-s6) and the run driver."""
from __future__ import annotations

import argparse
import os

from field_report.backends import ASR_BACKENDS, CAPTION_BACKENDS, CLASSIFY_BACKENDS, TRANSLATE_BACKENDS


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--force", action="store_true", help="redo the stage even if its output is current")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])


def add_offline(p: argparse.ArgumentParser) -> None:
    p.add_argument("--offline", action="store_true",
                   help="deterministic offline backends for any not named: --asr sidecar --captions sidecar --classify keyword")


def add_asr(p: argparse.ArgumentParser) -> None:
    p.add_argument("--asr", choices=ASR_BACKENDS, default=None,
                   help="speech-to-text backend (default faster-whisper, or sidecar with --offline; "
                        "sidecar reads ground-truth <name>.transcript.json files)")
    p.add_argument("--asr-model", default=os.environ.get("FRP_ASR_MODEL", "tiny"),
                   help="faster-whisper model: tiny|base|small|medium|large-v3 (default tiny)")
    p.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto",
                   help="faster-whisper device; auto picks CUDA if present, else CPU int8")


def add_translate(p: argparse.ArgumentParser) -> None:
    p.add_argument("--translate", choices=TRANSLATE_BACKENDS, default="whisper",
                   help="whisper: translate notes whose language is not English; none: passthrough")


def add_captions(p: argparse.ArgumentParser) -> None:
    p.add_argument("--captions", choices=CAPTION_BACKENDS, default=None,
                   help="photo caption backend (default: sidecar if the export ships *.caption.json files, else none; anthropic is experimental)")


def add_classify(p: argparse.ArgumentParser) -> None:
    p.add_argument("--classify", choices=CLASSIFY_BACKENDS, default=None,
                   help="classification backend (default keyword; anthropic is experimental)")


def apply_offline(args: argparse.Namespace) -> None:
    """Fill in the backends the caller did not choose.

    `--offline` selects the deterministic offline implementation of every backend left unset
    (--asr sidecar --captions sidecar --classify keyword); a backend named explicitly always wins,
    so `--offline --asr faster-whisper` means "no network, real ASR from the cached model".
    Without `--offline`, ASR defaults to faster-whisper (or $FRP_ASR).
    """
    offline = bool(getattr(args, "offline", False))
    if hasattr(args, "asr") and args.asr is None:
        args.asr = "sidecar" if offline else os.environ.get("FRP_ASR", "faster-whisper")
    if hasattr(args, "captions") and args.captions is None:
        args.captions = "sidecar" if offline else "auto"
    if hasattr(args, "classify") and args.classify is None:
        args.classify = "keyword"
