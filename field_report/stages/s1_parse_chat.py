#!/usr/bin/env python
"""s1_parse_chat.py (deterministic).

WhatsApp export txt -> messages.json. Whole file, no site filtering.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from field_report.lib import common

STAGE = "s1_parse_chat"
NAME = "messages"

FILE_ATTACHED_SUFFIX = " (file attached)"
LOCATION_PREFIX = "location: https://maps.google.com/" "?q="  # query string follows
MEDIA_OMITTED_MARKER = "<Media omitted>"
EDITED_SUFFIX = " <This message was edited>"
DELETED_TEXTS = {"This message was deleted", "You deleted this message"}
FSI = "⁨"  # FIRST STRONG ISOLATE — wraps @mentions in the export
PDI = "⁩"  # POP DIRECTIONAL ISOLATE

# --- iOS export grammar ------------------------------------------------------
# The iOS exporter differs from the android one in every surface detail: it
# brackets the timestamp, uses a 24-hour clock, names attachments inline as
# "<attached: NAME>" instead of the "(file attached)" suffix, capitalises the
# location prefix, prefixes bidi marks on machine-generated lines, and — unlike
# android — carries a sender name on system lines.
LRM = "‎"  # LEFT-TO-RIGHT MARK — prefixes attachment/system lines
RLM = "‏"  # RIGHT-TO-LEFT MARK
IOS_ATTACHED_RE = re.compile(r"<attached:\s*(?P<fn>[^>]+)>")
IOS_LOCATION_PREFIX = "location: https://maps.google.com/" "?q="  # query string follows
# iOS omits media as "<Media omitted>" or as "<type> omitted".
IOS_OMITTED_RE = re.compile(
    r"<Media omitted>|\b(?:image|audio|video|sticker|document|GIF|contact card) omitted\b",
    re.IGNORECASE,
)
# System notices carry a sender name on iOS, so they are recognised by wording.
IOS_SYSTEM_RES = [
    re.compile(r"^Messages and calls are end-to-end encrypted"),
    re.compile(r"\bcreated this group\b"),
    re.compile(r"\b(?:added you|added|removed|joined using this group's invite link|left)\b"),
    re.compile(r"\bchanged (?:the group name|this group's icon|the group description|their phone number)\b"),
    re.compile(r"\bYour security code with\b.*\bchanged\b"),
    re.compile(r"\bturned on disappearing messages\b|\bturned off disappearing messages\b"),
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="parse WhatsApp export -> messages.json")
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def classify_initial(text0: str):
    """Classify kind/media_filename/media_ext/gps from the message's FIRST
    physical line (the header line's body after 'Sender: '), BEFORE any
    continuation lines are appended.

    Real-data verified: classifying off the fully-joined (post-continuation)
    text undercounts media/location/media_omitted, because ~12 messages in
    the real export repeat the filename (or, for media_omitted, name a file)
    on a following continuation line. chat-parse rule 5 ("body ends '(file
    attached)'") means the single header line's body, not the final text.

    Returns (kind, media_filename, media_ext, gps, loc_error_code_or_None).
    """
    if text0.endswith(FILE_ATTACHED_SUFFIX):
        fn = text0[: -len(FILE_ATTACHED_SUFFIX)].strip()
        if "." in fn:
            ext = fn.rsplit(".", 1)[1].lower()
            media_ext = ext or None  # trailing dot, no extension -> None
        else:
            media_ext = None
        return "media", fn, media_ext, None, None
    if text0.startswith(LOCATION_PREFIX):
        query = text0[len(LOCATION_PREFIX):]
        parts = query.split(",")
        if len(parts) == 2:
            try:
                lat, lng = float(parts[0]), float(parts[1])
                return "location", None, None, {"lat": lat, "lng": lng}, None
            except ValueError:
                pass
        return "location", None, None, None, "UNPARSABLE_LINE"
    if MEDIA_OMITTED_MARKER in text0:
        return "media_omitted", None, None, None, None
    return "text", None, None, None, None


def classify_initial_ios(text0: str):
    """iOS counterpart of classify_initial. Same return contract.

    Bidi marks are stripped before matching: the exporter sprinkles U+200E
    around attachment and system bodies, which would otherwise defeat every
    prefix test below.
    """
    probe = text0.replace(LRM, "").replace(RLM, "").strip()
    m = IOS_ATTACHED_RE.search(probe)
    if m:
        fn = m.group("fn").strip()
        if "." in fn:
            ext = fn.rsplit(".", 1)[1].lower()
            media_ext = ext or None  # trailing dot, no extension -> None
        else:
            media_ext = None
        return "media", fn, media_ext, None, None
    if probe.lower().startswith(IOS_LOCATION_PREFIX):
        query = probe[len(IOS_LOCATION_PREFIX):]
        parts = query.split(",")
        if len(parts) == 2:
            try:
                lat, lng = float(parts[0]), float(parts[1])
                return "location", None, None, {"lat": lat, "lng": lng}, None
            except ValueError:
                pass
        return "location", None, None, None, "UNPARSABLE_LINE"
    if IOS_OMITTED_RE.search(probe):
        return "media_omitted", None, None, None, None
    return "text", None, None, None, None


def _ios_is_system(text0: str) -> bool:
    probe = text0.replace(LRM, "").replace(RLM, "").strip()
    return any(r.search(probe) for r in IOS_SYSTEM_RES)


def parse_export(export_txt: Path):
    """Returns (messages: list[dict], source: dict, item_errors: list[dict])."""
    content = export_txt.read_text(encoding="utf-8-sig")
    raw_lines = content.split("\n")
    if raw_lines and raw_lines[-1] == "":
        # trailing newline in the file produces one empty trailing split
        # artifact that is not itself a real line.
        raw_lines = raw_lines[:-1]
    total_lines = len(raw_lines)
    fmt = common.detect_export_format(raw_lines)
    header_re = common.WA_HEADER_RE_IOS if fmt == "ios" else common.WA_HEADER_RE

    messages: list[dict] = []
    item_errors: list[dict] = []
    blank_lines = 0
    continuation_lines = 0
    cur: dict | None = None

    def finalize_current():
        if cur is None:
            return
        text = "\n".join(cur["text_parts"])
        text = text.replace(FSI, "").replace(PDI, "")
        if fmt == "ios":
            # The iOS exporter injects bidi marks around attachment and system
            # bodies; they are invisible but survive into report prose.
            text = text.replace(LRM, "").replace(RLM, "").strip()
        edited = False
        if text.endswith(EDITED_SUFFIX):
            edited = True
            text = text[: -len(EDITED_SUFFIX)]
        deleted = text in DELETED_TEXTS
        messages.append({
            "id": f"M{len(messages) + 1:05d}",
            "line_no": cur["line_no"],
            "ts": cur["ts"].strftime("%Y-%m-%dT%H:%M:%S"),
            "ts_raw": cur["ts_raw"],
            "sender": cur["sender"],
            "kind": cur["kind"],
            "text": text,
            "media_filename": cur["media_filename"],
            "media_ext": cur["media_ext"],
            "gps": cur["gps"],
            "edited": edited,
            "deleted": deleted,
            "raw": "\n".join(cur["raw_lines"]),
        })

    for i, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.rstrip("\r")
        m = header_re.match(line)
        if m:
            finalize_current()
            d, h, mi = m.group("d"), m.group("h"), m.group("mi")
            body = m.group("body")
            try:
                if fmt == "ios":
                    ts = common.parse_wa_ts_ios(d, h, mi, m.group("s"))
                else:
                    ts = common.parse_wa_ts(d, h, mi, m.group("ap"))
            except ValueError as e:
                item_errors.append(common.err(f"line{i}", "UNPARSABLE_LINE",
                                               f"unparsable timestamp at line {i}: {e}"))
                cur = None
                continue
            if fmt == "ios":
                # "[14/01/2030, 10:37:54]" — drop the trailing space only.
                ts_raw = line[: m.start("body") - 1].lstrip(LRM + RLM)
            else:
                ts_raw = line[: m.start("body") - 3]  # strip trailing " - "
            if ": " in body:
                sender, text0 = body.split(": ", 1)
            else:
                sender, text0 = None, body
            if fmt == "ios":
                # iOS names a sender even on system notices, so system lines are
                # recognised by wording rather than by a missing "Sender: ".
                if sender is not None:
                    sender = sender.lstrip(LRM + RLM).lstrip("~").strip() or None
                if sender is None or _ios_is_system(text0):
                    kind, media_filename, media_ext, gps = "system", None, None, None
                    sender = None
                else:
                    kind, media_filename, media_ext, gps, loc_err = classify_initial_ios(text0)
                    if loc_err:
                        item_errors.append(common.err(f"line{i}", loc_err,
                                                       f"unparsable location query at line {i}"))
            elif sender is None:
                kind, media_filename, media_ext, gps = "system", None, None, None
            else:
                kind, media_filename, media_ext, gps, loc_err = classify_initial(text0)
                if loc_err:
                    item_errors.append(common.err(f"line{i}", loc_err,
                                                   f"unparsable location query at line {i}"))
            cur = {
                "line_no": i,
                "ts": ts,
                "ts_raw": ts_raw,
                "sender": sender,
                "kind": kind,
                "media_filename": media_filename,
                "media_ext": media_ext,
                "gps": gps,
                "text_parts": [text0],
                "raw_lines": [line],
            }
        else:
            if cur is None:
                item_errors.append(common.err(f"line{i}", "UNPARSABLE_LINE",
                                               f"continuation before first header at line {i}"))
                continue
            if line == "":
                blank_lines += 1
            else:
                continuation_lines += 1
            cur["text_parts"].append(line)
            cur["raw_lines"].append(line)

    finalize_current()

    source = {
        "export_txt": str(export_txt),
        "format": fmt,
        "sha256": common.sha256_file(export_txt),
        "total_lines": total_lines,
        "parsed_messages": len(messages),
        "continuation_lines": continuation_lines,
        "blank_lines": blank_lines,
    }
    return messages, source, item_errors


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


def _run(cfg: dict, run_dir, logger, args) -> int:
    export_txt = Path(cfg["export_txt"])
    out_path = run_dir / f"{NAME}.json"

    if not args.force and _is_up_to_date(out_path, cfg, [export_txt]):
        print(f"SKIP {STAGE}: up to date")
        return 0

    messages, source, item_errors = parse_export(export_txt)

    payload = common.envelope(
        STAGE, cfg["site"], cfg,
        input_counts={"total_lines": source["total_lines"]},
        output_counts={"messages": len(messages)},
    )
    payload["source"] = source
    payload["messages"] = messages

    code = common.finalize(payload, n_in=source["total_lines"], n_out=len(messages),
                            item_errors=item_errors)

    try:
        common.write_stage(run_dir, NAME, payload, NAME)
    except ValueError as e:
        logger.error("output schema invalid: %s", e)
        return 4

    for e in item_errors:
        logger.warning("%s %s: %s", e["item"], e["code"], e["message"])
    logger.info("parsed %d messages from %d lines (%d continuation, %d blank) -> exit %d",
                len(messages), source["total_lines"], source["continuation_lines"],
                source["blank_lines"], code)
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
