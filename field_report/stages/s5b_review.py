#!/usr/bin/env python
"""s5b_review.py (human stage).

Serve the local review app: one card per site stop, joining clusters.json,
transcripts_en.json and (optionally) prelabels.json, and autosaving every
verdict to review.json — the gate artifact.

    python -m field_report.stages.s5b_review --config runs/<slug>/config.json

The server binds to 127.0.0.1 only. This machine may hold unpublished field
damage assessments; 0.0.0.0 would put them on the local network.

Media is served **only** for filenames that exact-match an entry in
media_manifest.json, and only from that entry's own abs_path. No user input is
ever joined onto a directory — that is what makes GET /media/../../secrets a
404 rather than a leak.

Exit codes: 0 ok, 2 bad invocation/config, 5 missing upstream.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from field_report.lib import common

STAGE = "s5b_review"
DEFAULT_PORT = 8770
APP_DIR = Path(__file__).resolve().parents[1] / "review_app"
MAX_BODY_BYTES = 8 * 1024 * 1024

# Static assets are served by explicit allowlist, never by resolving a path the
# client supplied. Adding a file to review_app/ does not make it reachable
# until its basename is listed here.
STATIC_ALLOWLIST = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

NO_APP_MESSAGE = (
    "The review app frontend (field_report/review_app/) is not present yet.\n"
    "The API is live and serving data:\n"
    "  GET  /api/stops\n"
    "  GET  /media/<filename>\n"
    "  POST /api/stop/<cluster_id>\n"
    "  POST /api/finish\n"
)


class UnknownStop(ValueError):
    """POSTed cluster_id is not in review.json — the caller answers 400."""


# --------------------------------------------------------------------------
# pure functions — everything below the server is independently testable
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prelabels_sha256(prelabels: dict | None) -> str | None:
    if not prelabels:
        return None
    canon = json.dumps(prelabels, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _bindings_index(prelabels: dict | None) -> dict[tuple[str, str], dict]:
    """(cluster_id, audio filename) -> proposed binding."""
    if not prelabels:
        return {}
    return {(b["cluster_id"], b["audio"]): b for b in prelabels.get("bindings", [])}


def _captions_index(prelabels: dict | None) -> dict[str, dict]:
    """photo filename -> prelabel entry."""
    if not prelabels:
        return {}
    return {p["filename"]: p for p in prelabels.get("photos", [])}


def _blank_photo(filename: str) -> dict:
    return {"filename": filename, "caption_override": None,
            "excluded": False, "exclude_reason": None, "move_to": None}


def _blank_audio(filename: str, proposed: dict | None) -> dict:
    photos = list(proposed["photos"]) if proposed and proposed.get("photos") else []
    return {"file": filename,
            "text_override": None, "en_override": None,
            "binding": photos,
            "binding_source": "accepted" if photos else "none",
            "dropped": False, "drop_reason": None}


def initial_review(clusters: dict, transcripts_en: dict, prelabels: dict | None,
                   cfg: dict, reviewer: str) -> dict:
    """A fresh decision log: every stop pending, bindings seeded from prelabels."""
    proposed = _bindings_index(prelabels)
    stops = []
    for c in clusters["clusters"]:
        stops.append({
            "cluster_id": c["id"],
            # The verdict belongs to these messages, not to the label s5 gave the
            # stop. Recording them is what lets s5c re-key a verdict after an
            # s5 --force — including for a pin-only or text-only stop, which has
            # no media to match on.
            "message_ids": list(c.get("message_ids", [])),
            "verdict": "pending",
            "reviewed_at": None,
            "human_note": None,
            "gps_override": None,
            "split_at": None,
            "merge_with": None,
            "photos": [_blank_photo(p["filename"]) for p in c.get("photos", [])],
            "audios": [_blank_audio(a["filename"], proposed.get((c["id"], a["filename"])))
                       for a in c.get("audios", [])],
        })

    review = common.envelope(
        STAGE, cfg["site"], cfg,
        input_counts={"clusters": len(clusters["clusters"]),
                      "transcripts": len(transcripts_en.get("items", []))},
        output_counts={"stops": len(stops)},
    )
    review["meta"]["reviewer"] = reviewer
    review["meta"]["clusters_total"] = len(clusters["clusters"])
    review["meta"]["prelabels_sha256"] = _prelabels_sha256(prelabels)
    review["stops"] = stops
    review["counts"] = recount(review)
    return review


def recount(review: dict) -> dict:
    """Derive counts from stops. Pure — the caller assigns the result."""
    counts = {"approved": 0, "discarded": 0, "pending": 0,
              "bindings_accepted": 0, "bindings_corrected": 0, "bindings_manual": 0,
              "photos_excluded": 0, "photos_moved": 0, "texts_edited": 0}
    for stop in review.get("stops", []):
        verdict = stop.get("verdict", "pending")
        if verdict in counts:
            counts[verdict] += 1
        for ph in stop.get("photos", []):
            if ph.get("excluded"):
                counts["photos_excluded"] += 1
            if ph.get("move_to"):
                counts["photos_moved"] += 1
        for au in stop.get("audios", []):
            source = au.get("binding_source", "none")
            key = f"bindings_{source}"
            if key in counts:
                counts[key] += 1
            if au.get("text_override") or au.get("en_override"):
                counts["texts_edited"] += 1
    return counts


def save_review(run_dir: Path, review: dict) -> None:
    """Validate against review.schema.json, then atomic-write. Invalid input
    raises before anything touches review.json, so a malformed POST can never
    corrupt an in-progress review."""
    common.write_stage(run_dir, "review", review, "review")


AUTO_NOTE = "auto-accepted, unreviewed"


def auto_accept(review: dict) -> dict:
    """Approve every pending stop without a human. Pure; the caller saves.

    The decision is never disguised: each auto-accepted stop carries AUTO_NOTE as
    its note (a human note is never overwritten), and meta.auto_accepted is set so
    the report and the gate can say the evidence is unreviewed.
    """
    out = dict(review)
    out["meta"] = dict(review["meta"], auto_accepted=True)
    stamp = _now()
    stops = []
    for stop in review["stops"]:
        stop = dict(stop)
        if stop["verdict"] == "pending":
            stop["verdict"] = "approved"
            stop["reviewed_at"] = stamp
            stop["human_note"] = stop.get("human_note") or AUTO_NOTE
        stops.append(stop)
    out["stops"] = stops
    out["counts"] = recount(out)
    return out


def load_review(run_dir: Path) -> dict | None:
    p = run_dir / "review.json"
    if not p.is_file():
        return None
    existing = json.loads(p.read_text(encoding="utf-8"))
    problems = common.validate(existing, "review")
    if problems:
        # Never clobber a human decision log we cannot parse — stop loudly and
        # let the reviewer decide.
        raise ValueError(f"review.json is schema-invalid, refusing to overwrite: {problems[:5]}")
    return existing


def load_or_init(run_dir: Path, clusters: dict, transcripts_en: dict,
                 prelabels: dict | None, cfg: dict, reviewer: str) -> dict:
    """Reload an existing review and reconcile it against the current clusters.

    Existing verdicts are never discarded: a cluster with no stop is appended as
    pending, a stop whose cluster has vanished is retained and reported, and
    media that appeared or vanished inside a surviving cluster is reconciled the
    same way.
    """
    existing = load_review(run_dir)
    if existing is None:
        return initial_review(clusters, transcripts_en, prelabels, cfg, reviewer)

    fresh = initial_review(clusters, transcripts_en, prelabels, cfg, reviewer)
    fresh_by_id = {s["cluster_id"]: s for s in fresh["stops"]}
    existing_by_id = {s["cluster_id"]: s for s in existing["stops"]}

    stops: list[dict] = []
    orphaned: list[str] = []
    for stop in existing["stops"]:
        template = fresh_by_id.get(stop["cluster_id"])
        if template is None:
            orphaned.append(stop["cluster_id"])
            stops.append(stop)
            continue
        stops.append(_reconcile_stop(stop, template))

    added = [s["cluster_id"] for s in fresh["stops"] if s["cluster_id"] not in existing_by_id]
    stops.extend(fresh_by_id[cid] for cid in added)

    review = dict(existing)
    review["stops"] = stops
    review["meta"] = dict(existing["meta"])
    review["meta"]["clusters_total"] = len(clusters["clusters"])
    review["meta"]["prelabels_sha256"] = _prelabels_sha256(prelabels)
    review["counts"] = recount(review)

    if existing["meta"].get("reviewer") != reviewer:
        print(f"note: review.json was started by "
              f"{existing['meta'].get('reviewer')!r}; continuing under that name")
    if added:
        print(f"note: {len(added)} new cluster(s) since the last review — "
              f"appended as pending: {', '.join(added)}")
    if orphaned:
        print(f"note: {len(orphaned)} stop(s) have no matching cluster any more; "
              f"verdicts retained: {', '.join(orphaned)}")
    return review


def _reconcile_stop(stop: dict, template: dict) -> dict:
    """Keep every recorded decision; add entries for media that appeared."""
    out = dict(stop)
    # Backfill for a review.json written before stops carried their messages, and
    # refresh after a re-cluster: the ids are the applier's re-keying handle.
    out["message_ids"] = list(template.get("message_ids") or stop.get("message_ids") or [])

    have_photos = {p["filename"] for p in stop.get("photos", [])}
    out["photos"] = list(stop.get("photos", [])) + [
        p for p in template["photos"] if p["filename"] not in have_photos
    ]

    have_audios = {a["file"] for a in stop.get("audios", [])}
    out["audios"] = list(stop.get("audios", [])) + [
        a for a in template["audios"] if a["file"] not in have_audios
    ]
    return out


def save_stop(run_dir: Path, stop: dict) -> dict:
    """Merge one stop into review.json by cluster_id, recount, save. Returns counts."""
    review = load_review(run_dir)
    if review is None:
        raise common.MissingUpstream(f"review.json not found in {run_dir}")

    cluster_id = stop.get("cluster_id")
    for i, existing in enumerate(review["stops"]):
        if existing["cluster_id"] == cluster_id:
            merged = {**existing, **stop}
            if merged.get("verdict") in ("approved", "discarded") and not merged.get("reviewed_at"):
                merged["reviewed_at"] = _now()
            review["stops"][i] = merged
            break
    else:
        raise UnknownStop(f"no stop with cluster_id {cluster_id!r} in review.json")

    review["counts"] = recount(review)
    save_review(run_dir, review)
    return review["counts"]


def first_pending(review: dict) -> str | None:
    for stop in review.get("stops", []):
        if stop.get("verdict") == "pending":
            return stop["cluster_id"]
    return None


def resolve_media(manifest: dict, filename: str) -> str | None:
    """Exact-match the requested string against the manifest and return the
    manifest's own abs_path. Never joins user input onto a directory — that is
    the whole path-safety story."""
    for item in manifest["items"]:
        if item["filename"] == filename and item.get("exists"):
            return item["abs_path"]
    return None


def build_stops_payload(clusters: dict, transcripts_en: dict,
                        prelabels: dict | None, review: dict) -> list[dict]:
    """Join clusters + transcripts + prelabels + current verdicts into the card
    dataset the app renders. One entry per cluster, in cluster order."""
    captions = _captions_index(prelabels)
    proposed = _bindings_index(prelabels)
    verdicts = {s["cluster_id"]: s for s in review.get("stops", [])}
    texts = {t["file"]: t for t in transcripts_en.get("items", [])}

    payload: list[dict] = []
    total = len(clusters["clusters"])
    for idx, c in enumerate(clusters["clusters"]):
        stop = verdicts.get(c["id"], {})
        photo_decisions = {p["filename"]: p for p in stop.get("photos", [])}
        audio_decisions = {a["file"]: a for a in stop.get("audios", [])}

        photos = []
        for ph in c.get("photos", []):
            d = photo_decisions.get(ph["filename"], _blank_photo(ph["filename"]))
            pre = captions.get(ph["filename"], {})
            photos.append({
                "filename": ph["filename"],
                "message_id": ph["message_id"],
                "ts": ph["ts"],
                "exists": ph["exists"],
                "url": "/media/" + ph["filename"],
                "caption": pre.get("caption"),
                "sector_guess": pre.get("sector_guess"),
                "sector_confidence": pre.get("sector_confidence"),
                "visible_damage": pre.get("visible_damage"),
                "countable": pre.get("countable"),
                "caption_override": d.get("caption_override"),
                "excluded": bool(d.get("excluded")),
                "exclude_reason": d.get("exclude_reason"),
                "move_to": d.get("move_to"),
            })

        audios = []
        for au in c.get("audios", []):
            d = audio_decisions.get(au["filename"])
            pre = proposed.get((c["id"], au["filename"]))
            if d is None:
                d = _blank_audio(au["filename"], pre)
            t = texts.get(au["filename"], {})
            audios.append({
                "file": au["filename"],
                "message_id": au["message_id"],
                "ts": au["ts"],
                "exists": au["exists"],
                "url": "/media/" + au["filename"],
                "text": t.get("text_clean"),
                "en_text": t.get("en_text"),
                "unclear_spans": t.get("unclear_spans", []),
                "confidence": t.get("confidence"),
                "transcript_status": t.get("status"),
                "proposed_binding": list(pre["photos"]) if pre else [],
                "proposed_confidence": pre.get("confidence") if pre else None,
                "basis": pre.get("basis") if pre else None,
                "text_override": d.get("text_override"),
                "en_override": d.get("en_override"),
                "binding": list(d.get("binding", [])),
                "binding_source": d.get("binding_source", "none"),
                "dropped": bool(d.get("dropped")),
                "drop_reason": d.get("drop_reason"),
            })

        bound = {f for a in audios for f in a["binding"]}
        warnings = []
        if c.get("gps_inherited"):
            warnings.append(f"pin inherited from {c.get('gps_message_id') or 'an earlier stop'} "
                            "(no pin at this stop)")
        if c.get("gps") is None:
            warnings.append("no GPS pin for this stop")
        if not photos:
            warnings.append("no photos at this stop")
        if not audios:
            warnings.append("no voice notes at this stop")
        elif photos and not bound:
            warnings.append("no voice note is bound to a photo")

        payload.append({
            "cluster_id": c["id"],
            "seq": c["seq"],
            "index": idx,
            "total": total,
            "start_ts": c["start_ts"],
            "end_ts": c["end_ts"],
            "boundary_reason": c["boundary_reason"],
            "gps": c.get("gps"),
            "gps_message_id": c.get("gps_message_id"),
            "gps_inherited": bool(c.get("gps_inherited")),
            "message_ids": c.get("message_ids", []),
            "texts": c.get("texts", []),
            "omitted_count": c.get("omitted_count", 0),
            "senders": c.get("senders", []),
            "warnings": warnings,
            "unbound_photos": [p["filename"] for p in photos if p["filename"] not in bound],
            "verdict": stop.get("verdict", "pending"),
            "reviewed_at": stop.get("reviewed_at"),
            "human_note": stop.get("human_note"),
            "gps_override": stop.get("gps_override"),
            "split_at": stop.get("split_at"),
            "merge_with": stop.get("merge_with"),
            "photos": photos,
            "audios": audios,
        })
    return payload


def build_orphans(clusters: dict, manifest: dict, review: dict) -> list[dict]:
    """Photos in no stop, or in a stop the reviewer discarded."""
    in_a_cluster = {p["filename"] for c in clusters["clusters"] for p in c.get("photos", [])}
    discarded = {s["cluster_id"] for s in review.get("stops", []) if s.get("verdict") == "discarded"}

    orphans = [{"filename": it["filename"], "url": "/media/" + it["filename"],
                "ts": it["ts"], "from_stop": None, "reason": "in no stop"}
               for it in manifest["items"]
               if it["media_kind"] == "photo" and it["filename"] not in in_a_cluster]

    for c in clusters["clusters"]:
        if c["id"] not in discarded:
            continue
        for p in c.get("photos", []):
            orphans.append({"filename": p["filename"], "url": "/media/" + p["filename"],
                            "ts": p["ts"], "from_stop": c["id"],
                            "reason": f"stop {c['id']} was discarded"})
    return orphans


def finish_problems(review: dict) -> list[str]:
    """Everything that would make s5c unhappy, reported before the reviewer leaves."""
    problems = list(common.validate(review, "review"))
    known = {s["cluster_id"] for s in review.get("stops", [])}
    discarded = {s["cluster_id"] for s in review.get("stops", [])
                 if s.get("verdict") == "discarded"}
    for stop in review.get("stops", []):
        cid = stop["cluster_id"]
        if stop.get("verdict") == "pending":
            problems.append(f"{cid}: still pending")
        if stop.get("split_at") and stop.get("merge_with"):
            problems.append(f"CONFLICT {cid}: split_at and merge_with are both set")
        target = stop.get("merge_with")
        if target:
            if target not in known:
                problems.append(f"CONFLICT {cid}: merge_with {target} is not a known stop")
            elif target in discarded:
                problems.append(f"CONFLICT {cid}: merge_with {target} was discarded")
            elif target == cid:
                problems.append(f"CONFLICT {cid}: cannot merge a stop with itself")
        excluded = {p["filename"] for p in stop.get("photos", []) if p.get("excluded")}
        moved = {p["filename"] for p in stop.get("photos", []) if p.get("move_to")}
        for ph in stop.get("photos", []):
            dest = ph.get("move_to")
            if not dest:
                continue
            if dest not in known:
                problems.append(f"CONFLICT {cid}: {ph['filename']} moves to "
                                f"{dest}, which is not a known stop")
            elif dest in discarded:
                problems.append(f"CONFLICT {cid}: {ph['filename']} moves to "
                                f"{dest}, which was discarded")
            elif dest == cid:
                problems.append(f"CONFLICT {cid}: {ph['filename']} moves to its own stop")
        for au in stop.get("audios", []):
            if au.get("dropped"):
                continue
            bad = [f for f in au.get("binding", []) if f in excluded or f in moved]
            if bad:
                problems.append(f"CONFLICT {cid}: {au['file']} is bound to "
                                f"{', '.join(bad)}, which the review excluded or moved away")
    return problems


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


def make_handler(run_dir: Path, clusters: dict, transcripts_en: dict,
                 manifest: dict, prelabels: dict | None, lock: threading.Lock,
                 logger=None):
    """Build the request handler class bound to this run's data."""

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "s5b_review"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        _raw = b""

        # -- plumbing ----------------------------------------------------
        def log_message(self, fmt, *args):  # noqa: A003 — stdlib hook name
            if logger:
                logger.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _text(self, code: int, message: str):
            self._send(code, message.encode("utf-8"), "text/plain; charset=utf-8")

        # -- GET ---------------------------------------------------------
        def do_GET(self):  # noqa: N802 — stdlib hook name
            try:
                path = unquote(urlparse(self.path).path)
                if path == "/" or path == "/index.html":
                    return self._static("index.html")
                if path in ("/app.js", "/style.css"):
                    return self._static(path.lstrip("/"))
                if path == "/api/stops":
                    return self._api_stops()
                if path == "/api/orphans":
                    return self._api_orphans()
                if path.startswith("/media/"):
                    return self._media(path[len("/media/"):])
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                return self._text(404, f"no route for {path}")
            except Exception:
                if logger:
                    logger.error("GET %s failed:\n%s", self.path, traceback.format_exc())
                return self._text(500, "internal error — see logs/s5b_review.log")

        def _static(self, name: str):
            content_type = STATIC_ALLOWLIST.get(name)
            if content_type is None:                       # unreachable by route
                return self._text(404, f"not served: {name}")
            path = APP_DIR / name                          # name comes from the allowlist
            if not path.is_file():
                return self._text(503, NO_APP_MESSAGE)
            return self._send(200, path.read_bytes(), content_type)

        def _api_stops(self):
            # Reads take the write lock too: on Windows os.replace fails if
            # another thread has review.json open, so an unlocked GET could
            # make a concurrent autosave lose a verdict.
            with lock:
                review = load_review(run_dir)
            if review is None:
                return self._text(500, "review.json is missing — restart the server")
            return self._json({
                "site": review["meta"].get("site"),
                "reviewer": review["meta"].get("reviewer"),
                "total": len(review["stops"]),
                "counts": review["counts"],
                "resume_at": first_pending(review),
                "has_prelabels": prelabels is not None,
                "stops": build_stops_payload(clusters, transcripts_en, prelabels, review),
            })

        def _api_orphans(self):
            with lock:
                review = load_review(run_dir)
            if review is None:
                return self._text(500, "review.json is missing — restart the server")
            return self._json({"orphans": build_orphans(clusters, manifest, review)})

        def _media(self, requested: str):
            abs_path = resolve_media(manifest, requested)
            if abs_path is None or not os.path.isfile(abs_path):
                return self._text(404, "not found")
            ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
            data = Path(abs_path).read_bytes()

            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                try:
                    start_s, _, end_s = rng[len("bytes="):].partition("-")
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else len(data) - 1
                    end = min(end, len(data) - 1)
                    if 0 <= start <= end:
                        chunk = data[start:end + 1]
                        return self._send(
                            206, chunk, ctype,
                            {"Content-Range": f"bytes {start}-{end}/{len(data)}",
                             "Accept-Ranges": "bytes"})
                except ValueError:
                    pass
            return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

        # -- POST --------------------------------------------------------
        def do_POST(self):  # noqa: N802 — stdlib hook name
            try:
                # Always consume the request body before answering. Replying
                # while bytes are still queued makes Windows send an RST, which
                # the browser surfaces as a failed save on a request that in
                # fact succeeded.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._text(400, "bad Content-Length")
                if length > MAX_BODY_BYTES:
                    self.close_connection = True
                    return self._text(413, "request body too large")
                self._raw = self.rfile.read(length) if length > 0 else b""

                path = unquote(urlparse(self.path).path)
                if path.startswith("/api/stop/"):
                    return self._post_stop(path[len("/api/stop/"):])
                if path == "/api/finish":
                    return self._post_finish()
                return self._text(404, f"no route for {path}")
            except Exception:
                if logger:
                    logger.error("POST %s failed:\n%s", self.path, traceback.format_exc())
                return self._text(500, "internal error — see logs/s5b_review.log")

        def _body(self) -> dict:
            return json.loads(self._raw.decode("utf-8")) if self._raw else {}

        def _post_stop(self, cluster_id: str):
            try:
                stop = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._json({"ok": False, "error": f"body is not valid JSON: {e}"}, 400)
            if not isinstance(stop, dict):
                return self._json({"ok": False, "error": "body must be a stop object"}, 400)
            stop.setdefault("cluster_id", cluster_id)
            if stop["cluster_id"] != cluster_id:
                return self._json({"ok": False,
                                   "error": f"body cluster_id {stop['cluster_id']!r} does not "
                                            f"match the URL {cluster_id!r}"}, 400)
            with lock:
                try:
                    counts = save_stop(run_dir, stop)
                except UnknownStop as e:
                    return self._json({"ok": False, "error": str(e)}, 400)
                except ValueError as e:      # schema-invalid — nothing was written
                    return self._json({"ok": False, "error": str(e)}, 400)
                review = load_review(run_dir)
            if logger:
                logger.info("saved %s verdict=%s", cluster_id, stop.get("verdict"))
            return self._json({"ok": True, "cluster_id": cluster_id, "counts": counts,
                               "resume_at": first_pending(review), "saved_at": _now()})

        def _post_finish(self):
            with lock:
                review = load_review(run_dir)
            if review is None:
                return self._text(500, "review.json is missing — restart the server")
            problems = finish_problems(review)
            return self._json({"ok": not problems,
                               "pending": review["counts"]["pending"],
                               "counts": review["counts"],
                               "problems": problems})

    return ReviewHandler


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="local review server for the verification gate")
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--reviewer", default=os.environ.get("USERNAME") or "reviewer")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--auto", action="store_true",
                   help="no human: accept every stop and its pre-labels, mark each one "
                        "'unreviewed' in the note, and write review.json (used by the demo)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    # This stage's own output carries em dashes and the reviewer's name; a
    # redirected stdout falls back to cp1252 on Windows and would raise
    # UnicodeEncodeError before the server ever starts.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    args = parse_args(argv)
    cfg = common.load_config(args.config)
    run_dir = common.resolve_run_dir(cfg, args.run_dir)
    logger = common.get_logger(run_dir, STAGE, args.log_level)

    clusters = common.read_stage(run_dir, "clusters", "clusters")
    transcripts = common.read_stage(run_dir, "transcripts_en", "transcripts_en")
    manifest = common.read_stage(run_dir, "media_manifest", "media_manifest")

    prelabels = None
    if (run_dir / "prelabels.json").is_file():
        prelabels = common.read_stage(run_dir, "prelabels", "prelabels")
    else:
        print("no prelabels.json — cards will show photos without captions "
              "and no proposed bindings. Run s5a first for the assisted experience.")

    review = load_or_init(run_dir, clusters, transcripts, prelabels, cfg, args.reviewer)
    if args.auto:
        if (run_dir / "review.json").is_file() and review["counts"]["pending"] == 0:
            print("SKIP s5b_review --auto: review.json has no pending stop")
            return 0
        review = auto_accept(review)
        save_review(run_dir, review)
        print(f"auto-accepted {review['counts']['approved']} stops (unreviewed) -> "
              f"{run_dir / 'review.json'}")
        return 0
    save_review(run_dir, review)

    handler = make_handler(run_dir, clusters, transcripts, manifest, prelabels,
                           threading.Lock(), logger)
    # 127.0.0.1 only, never 0.0.0.0 — this machine may hold unpublished field
    # damage assessments and must not answer on the local network.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"serving {len(review['stops'])} stops at {url}")
    print(f"  {review['counts']['pending']} pending, "
          f"resuming at {first_pending(review) or '—'}")
    print(f"  autosave -> {run_dir / 'review.json'}")
    print(f"  Ctrl-C when done, then: python -m field_report.stages.s5c_apply_review "
          f"--config {args.config}")
    if not APP_DIR.is_dir():
        print(f"  note: {APP_DIR} does not exist yet — the API serves data, "
              f"but there is no UI to open")
    # serve_forever() never returns, so a block-buffered (piped) stdout would
    # hide the banner — and the banner is where the reviewer reads the URL.
    sys.stdout.flush()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped. Verdicts are saved; re-run to resume.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    except common.MissingUpstream as e:
        print(f"missing upstream: {e}", file=sys.stderr)
        sys.exit(5)
