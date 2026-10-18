"""Shared pipeline helper — shared helper. Every stage imports this; no stage re-implements
envelope/IO/retry. The schema is the contract (field_report/schemas/*.schema.json)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

PACKAGE_DIR = Path(__file__).resolve().parents[1]
REPO = PACKAGE_DIR.parent
SCHEMA_DIR = PACKAGE_DIR / "schemas"

WA_HEADER_RE = re.compile(
    r"^(?P<d>\d{2}/\d{2}/\d{4}), (?P<h>\d{1,2}):(?P<mi>\d{2})[\s ](?P<ap>am|pm) - (?P<body>.*)$"
)

# iOS-style export: "[14/01/2030, 10:37:54] Alex Demo: <attached: file.jpg>".
# Attachment/system lines are prefixed with U+200E (LRM), so the leading mark is
# optional. 24-hour clock with seconds; no am/pm token.
WA_HEADER_RE_IOS = re.compile(
    "^[\u200e\u200f]?"
    r"\[(?P<d>\d{2}/\d{2}/\d{4}), "
    r"(?P<h>\d{1,2}):(?P<mi>\d{2}):(?P<s>\d{2})\] (?P<body>.*)$"
)

# --------------------------------------------------------------------------
# field conventions
#
# The inspector sends a lone "." once everything for one stop has been sent, and
# may open each voice note with the stop number ("Stop two, east stair ..."). Both
# are deliberate human signals: the dot bounds a stop without depending on when
# the GPS pin happened to be dropped, and the spoken number outranks anything a
# classifier infers from a photograph.
#
# Everything between two dots is ONE stop - pins included. A stop may be pinned
# more than once, so where the export uses dots the pin is a location *within* the
# stop, never a boundary. The clock is not a boundary either; DOT_MODE_GAP_SECONDS
# exists only to stop a forgotten dot from collapsing a whole afternoon into one stop.
# --------------------------------------------------------------------------

SEPARATOR_TEXTS = {".", "。", "·"}
DOT_MODE_GAP_SECONDS = 1800

# "stop 3", "sector one": the spoken/written stop number. A profile supplies its own
# regex (profile["stop_regex"]); this is the English default.
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
DEFAULT_STOP_REGEX = (r"(?i)\b(?:stop|sector)\s*[-:]?\s*"
                      r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b")
_DEFAULT_STOP_RE = re.compile(DEFAULT_STOP_REGEX)


def is_separator(message: dict) -> bool:
    """True for the lone-dot message that closes a site stop."""
    if message.get("kind") != "text":
        return False
    return (message.get("text") or "").strip() in SEPARATOR_TEXTS


def uses_dot_convention(messages) -> bool:
    """True when this export carries the separator convention at all.

    One dot is enough: a surveyor who dots does it deliberately. An export
    without any dot (sample, sample) keeps the old pin-and-clock clustering.
    """
    return any(is_separator(m) for m in messages)


def declared_sector(text: str | None, stop_regex: str | re.Pattern | None = None) -> int | None:
    """The stop number the speaker declared, or None. First match wins.

    `stop_regex` is the profile's pattern (one capture group: digits or a number
    word). Kept under its historical name; "sector" and "stop" are the same thing.
    """
    if not text:
        return None
    rx = _DEFAULT_STOP_RE if stop_regex is None else re.compile(stop_regex)
    for match in rx.finditer(text):
        token = match.group(1)
        n = int(token) if token.isdigit() else _NUM_WORDS.get(token.lower())
        if n and 1 <= n <= 99:
            return n
    return None


PHOTO_EXT = {"jpg", "jpeg", "png", "webp"}
AUDIO_EXT = {"opus", "m4a", "mp3", "ogg", "wav"}
DOC_EXT = {"pdf", "docx", "xlsx", "csv", "tif", "txt"}
CONTACT_EXT = {"vcf"}
VIDEO_EXT = {"mp4", "3gp"}

_CONFIG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["site", "date_from", "date_to", "export_txt", "media_dir"],
    "properties": {
        "site": {"type": "string", "minLength": 1},
        "site_slug": {"type": "string", "minLength": 1},
        "date_from": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$"},
        "date_to": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$"},
        "inspectors": {"type": "array", "items": {"type": "string"}},
        "export_txt": {"type": "string"},
        "media_dir": {"type": "string"},
        "map_image": {"type": "string"},
        # which profile to use: a file in field_report/profiles/ (see lib/profiles.py)
        "profile": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
    },
}


class ConfigError(Exception):
    """Invalid config - caller exits 2."""


class MissingUpstream(Exception):
    """Required upstream artifact missing or failed - caller exits 5."""


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "site"


def _resolve(base: Path, value: str) -> str:
    """Config paths are relative to the config file, falling back to the cwd."""
    p = Path(value)
    if p.is_absolute():
        return str(p)
    for root in (base, Path.cwd()):
        if (root / p).exists():
            return str((root / p).resolve())
    return str((base / p).resolve())


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config not found: {path}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config is not valid JSON: {e}") from e
    try:
        jsonschema.validate(cfg, _CONFIG_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ConfigError(f"config schema invalid: {e.message}") from e
    cfg["_hash_source"] = {k: v for k, v in cfg.items()}  # the file as written, for config_sha256
    base = p.resolve().parent
    cfg["_dir"] = str(base)   # not hashed (leading underscore)
    for key in ("export_txt", "media_dir", "map_image"):
        if key in cfg:
            cfg[key] = _resolve(base, cfg[key])
    for key in ("export_txt", "media_dir"):
        if not Path(cfg[key]).exists():
            raise ConfigError(f"config.{key} path does not exist: {cfg[key]}")
    if "map_image" in cfg and not Path(cfg["map_image"]).is_file():
        raise ConfigError(f"config.map_image path does not exist: {cfg['map_image']}")
    cfg.setdefault("site_slug", slugify(cfg["site"]))
    cfg.setdefault("inspectors", [])
    cfg.setdefault("profile", "site-inspection")
    # imported here: profiles imports nothing from this module, but keep the edge lazy
    from field_report.lib import profiles
    try:
        profiles.load(cfg["profile"])
    except profiles.ProfileError as e:
        raise ConfigError(str(e)) from e
    return cfg


# Config keys no deterministic stage reads. `profile` only changes classification and
# wording from s6 onwards, and hashing it would invalidate s1-s5 on a run that is
# already transcribed. The stages that do read it record `meta.profile`.
UNHASHED_CONFIG_KEYS = ("profile",)


def config_sha256(cfg: dict) -> str:
    """Hash of the config as written (relative paths), so moving the folder is not a change."""
    cfg = cfg.get("_hash_source", cfg)
    cfg = {k: v for k, v in cfg.items() if k not in UNHASHED_CONFIG_KEYS and not k.startswith("_")}
    canon = json.dumps(cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def resolve_run_dir(cfg: dict, override: str | None) -> Path:
    run_dir = Path(override) if override else Path.cwd() / "out" / "runs" / cfg["site_slug"]
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    return run_dir


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_wa_ts(date_s: str, h: str, mi: str, ampm: str) -> datetime:
    day, month, year = (int(x) for x in date_s.split("/"))
    hour = int(h) % 12
    if ampm == "pm":
        hour += 12
    return datetime(year, month, day, hour, int(mi))


def parse_wa_ts_ios(date_s: str, h: str, mi: str, s: str) -> datetime:
    """iOS exports use a 24-hour clock with seconds and no am/pm token."""
    day, month, year = (int(x) for x in date_s.split("/"))
    return datetime(year, month, day, int(h), int(mi), int(s))


def detect_export_format(lines: list[str], sample: int = 400) -> str:
    """Return 'android' or 'ios' by counting header matches over a sample of lines.

    WhatsApp exports the same conversation in two mutually exclusive header
    grammars depending on the exporting phone. Choosing the wrong one parses
    zero messages, so the format is detected rather than assumed.
    """
    android = ios = 0
    for line in lines[:sample]:
        line = line.rstrip("\r")
        if WA_HEADER_RE.match(line):
            android += 1
        elif WA_HEADER_RE_IOS.match(line):
            ios += 1
    if android == 0 and ios == 0:
        raise ValueError(
            "no WhatsApp header line matched either the android or the ios grammar "
            f"in the first {sample} lines — is this a WhatsApp export?"
        )
    return "ios" if ios > android else "android"


def _schema(name: str) -> dict:
    p = SCHEMA_DIR / f"{name}.schema.json"
    return json.loads(p.read_text(encoding="utf-8"))


def validate(payload: dict, schema: str) -> list[str]:
    v = jsonschema.Draft202012Validator(_schema(schema))
    return [f"{'/'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
            for e in v.iter_errors(payload)]


def read_stage(run_dir: Path, name: str, schema: str | None = None) -> dict:
    p = run_dir / f"{name}.json"
    if not p.is_file():
        raise MissingUpstream(f"required upstream artifact missing: {p}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    if payload.get("status") == "failed":
        raise MissingUpstream(f"upstream {name}.json has status=failed")
    if schema:
        problems = validate(payload, schema)
        if problems:
            raise MissingUpstream(f"upstream {name}.json schema-invalid: {problems[:3]}")
    return payload


def write_stage(run_dir: Path, name: str, payload: dict, schema: str) -> None:
    problems = validate(payload, schema)
    if problems:
        raise ValueError(f"{name}.json schema-invalid: {problems[:5]}")
    out = run_dir / f"{name}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, out)


def envelope(stage: str, site: str, cfg: dict, input_counts: dict, output_counts: dict) -> dict:
    return {
        "status": "ok",
        "errors": [],
        "meta": {
            "stage": stage,
            "site": site,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config_sha256": config_sha256(cfg),
            "profile": cfg.get("profile") or "site",
            "input_counts": input_counts,
            "output_counts": output_counts,
        },
    }


def finalize(payload: dict, n_in: int, n_out: int, item_errors: list[dict],
             empty_ok: bool = False) -> int:
    """Apply the status rules; set payload['status']; return the exit code.
    empty_ok=True only for gaps.json (the sole exemption from EMPTY_RESULT_WITH_INPUTS)."""
    payload.setdefault("errors", []).extend(item_errors)
    if n_in > 0 and n_out == 0 and not empty_ok:
        payload["status"] = "failed"
        payload["errors"].append(err("<stage>", "EMPTY_RESULT_WITH_INPUTS",
                                     f"{n_in} inputs produced 0 payload items"))
        return 4
    payload["status"] = "partial" if item_errors else "ok"
    return 3 if item_errors else 0


def err(item: str, code: str, message: str, retryable: bool = False, attempts: int = 1) -> dict:
    return {"item": item, "code": code, "message": message,
            "retryable": retryable, "attempts": attempts}


def retry(fn, item_id: str, cap: int = 2):
    """1 initial attempt + up to `cap` retries (cap+1 calls total).
    Returns (result, None) on success or (None, error_dict) after exhaustion."""
    last = None
    for attempt in range(1, cap + 2):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 — item-level isolation is the point
            last = e
    return None, err(item_id, "RETRY_EXHAUSTED", str(last), retryable=False, attempts=cap + 1)


def get_logger(run_dir: Path, stage: str, level: str) -> logging.Logger:
    logger = logging.getLogger(f"pipeline.{stage}")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(run_dir / "logs" / f"{stage}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def up_to_date(out_path: Path, cfg: dict, upstream: list[Path], settings: dict | None = None) -> bool:
    """True when `out_path` is a finished artifact for this config and these backend
    settings and is newer than every upstream file - the idempotence check for stages."""
    if not out_path.is_file():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    meta = existing.get("meta", {})
    if existing.get("status") not in ("ok", "partial"):
        return False
    if meta.get("config_sha256") != config_sha256(cfg):
        return False
    if settings is not None and meta.get("settings") != settings:
        return False
    mtime = out_path.stat().st_mtime
    return all(p.exists() and p.stat().st_mtime <= mtime for p in upstream)
