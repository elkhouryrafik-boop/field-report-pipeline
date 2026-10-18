#!/usr/bin/env python
"""s5c_apply_review.py (deterministic).

Apply the human decision log (review.json) to the machine's clusters and
transcripts, producing clusters_verified.json and transcripts_en_verified.json.

Pure function `apply_review` does the work; the CLI only does IO. The applier is
idempotent: running it twice on the same inputs produces byte-identical output.

    python -m field_report.stages.s5c_apply_review --config runs/<slug>/config.json

Exit codes: 0 ok, 3 partial (problems recorded), 4 conflicting instructions,
5 missing upstream, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import copy
import sys
import traceback

from field_report.lib import common
from field_report.stages.s5b_review import AUTO_NOTE

STAGE = "s5c_apply_review"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="apply review.json to clusters + transcripts")
    p.add_argument("--config", required=True)
    p.add_argument("--run-dir", default=None)
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--force", action="store_true", help="re-apply even if the outputs are current")
    return p.parse_args(argv)


def _remap_review(clusters: dict, review: dict, problems: list[str]) -> dict:
    """Re-key stop verdicts onto current cluster ids by message_id overlap.

    s5 --force can renumber every stop. A verdict belongs to the messages the
    reviewer looked at, not to the label the clusterer happened to assign, so a
    stop is matched to the current cluster sharing the most message ids.

    Message ids are the primary key and media filenames only the fallback: a
    pin-only, text-only or system-message stop carries no media at all, and
    matching those on media alone scored 0 against every cluster and dropped the
    verdict. `message_ids` is written by s5b; a review.json from before that is
    still matched on media.

    Runs BEFORE _validate_instructions — otherwise validation rejects the
    pre-recluster ids.
    """
    current = {c["id"]: set(c["message_ids"]) for c in clusters["clusters"]}
    if all(s["cluster_id"] in current for s in review["stops"]):
        return review  # ids unchanged — nothing to do

    media = {c["id"]: {p["filename"] for p in c["photos"]} | {a["filename"] for a in c["audios"]}
             for c in clusters["clusters"]}
    remapped = copy.deepcopy(review)
    taken: set[str] = set()
    for s in remapped["stops"]:
        if s["cluster_id"] in current:
            taken.add(s["cluster_id"])
            continue
        mine_msgs = set(s.get("message_ids") or [])
        mine_media = {p["filename"] for p in s["photos"]} | {a["file"] for a in s["audios"]}
        best, best_score = None, 0
        for cid in current:
            if cid in taken:
                continue
            score = len(mine_msgs & current[cid]) or len(mine_media & media[cid])
            if score > best_score:
                best, best_score = cid, score
        if best is None:
            problems.append(f"{s['cluster_id']}: orphaned verdict — no current stop shares its "
                            f"media; the review for this stop was dropped")
            s["cluster_id"] = "__ORPHAN__"
        else:
            problems.append(f"{s['cluster_id']}: remapped to {best} after re-cluster")
            s["cluster_id"] = best
            taken.add(best)
    remapped["stops"] = [s for s in remapped["stops"] if s["cluster_id"] != "__ORPHAN__"]
    return remapped


def _validate_instructions(clusters: dict, review: dict) -> list[str]:
    """Pass 1. Everything that must be rejected before any mutation."""
    problems: list[str] = []
    known = {c["id"] for c in clusters["clusters"]}
    stops = {s["cluster_id"]: s for s in review["stops"]}
    discarded = {cid for cid, s in stops.items() if s["verdict"] == "discarded"}
    msgs = {c["id"]: list(c["message_ids"]) for c in clusters["clusters"]}

    for s in review["stops"]:
        cid = s["cluster_id"]
        if cid not in known:
            problems.append(f"CONFLICT {cid}: review names a stop that does not exist in clusters.json")
            continue
        if s["split_at"] and s["merge_with"]:
            problems.append(f"CONFLICT {cid}: split_at and merge_with are both set — pick one")
        if s["split_at"] and s["split_at"] not in msgs[cid]:
            problems.append(f"CONFLICT {cid}: split_at {s['split_at']} is not a message in this stop")
        if s["split_at"] and msgs[cid] and s["split_at"] == msgs[cid][0]:
            problems.append(f"CONFLICT {cid}: split_at {s['split_at']} is the first message — "
                            f"the split would produce an empty stop")
        if s["merge_with"]:
            if s["merge_with"] not in known:
                problems.append(f"CONFLICT {cid}: merge_with {s['merge_with']} does not exist")
            elif s["merge_with"] in discarded:
                problems.append(f"CONFLICT {cid}: merge_with {s['merge_with']} was discarded")
            elif s["merge_with"] == cid:
                problems.append(f"CONFLICT {cid}: cannot merge a stop with itself")
            elif stops.get(s["merge_with"], {}).get("split_at"):
                # The target stops existing the moment it is split into a/b halves,
                # so this merge would silently find nothing to merge into.
                problems.append(f"CONFLICT {cid}: merge_with {s['merge_with']} is itself being "
                                f"split — merge into one of its halves, or drop the split")
        for p in s["photos"]:
            t = p.get("move_to")
            if not t:
                continue
            if t not in known:
                problems.append(f"CONFLICT {cid}: {p['filename']} moves to {t}, which does not exist")
            elif t in discarded:
                problems.append(f"CONFLICT {cid}: {p['filename']} moves to {t}, which was discarded")
            elif t == cid:
                problems.append(f"CONFLICT {cid}: {p['filename']} moves to its own stop")
    return problems


def _apply_photos(cluster: dict, stop: dict, problems: list[str]) -> tuple[list[dict], dict, set[str]]:
    """Return (kept_photos, captions, removed_filenames).

    The pre-filter list is stashed on the cluster as `_photos_before` so
    `_apply_moves` can still find a photo this pass has already removed. Every
    `_`-prefixed key is stripped before `apply_review` returns.
    """
    decisions = {p["filename"]: p for p in stop["photos"]}
    cluster["_photos_before"] = copy.deepcopy(cluster["photos"])
    kept, captions, removed = [], {}, set()
    for ph in cluster["photos"]:
        d = decisions.get(ph["filename"])
        if d is None:
            kept.append(ph)
            continue
        if d["excluded"] or d.get("move_to"):
            removed.add(ph["filename"])
            continue
        kept.append(ph)
        if d.get("caption_override"):
            captions[ph["filename"]] = d["caption_override"]
    return kept, captions, removed


def _apply_audios(cluster: dict, stop: dict, removed_photos: set[str],
                  problems: list[str]) -> tuple[list[dict], dict, set[str]]:
    """Return (kept_audios, bindings, dropped_filenames)."""
    decisions = {a["file"]: a for a in stop["audios"]}
    kept, bindings, dropped = [], {}, set()
    for au in cluster["audios"]:
        d = decisions.get(au["filename"])
        if d is None:
            kept.append(au)
            continue
        if d["dropped"]:
            dropped.add(au["filename"])
            continue
        kept.append(au)
        if d["binding"]:
            bad = [f for f in d["binding"] if f in removed_photos]
            if bad:
                problems.append(
                    f"CONFLICT {stop['cluster_id']}: {au['filename']} is bound to "
                    f"{', '.join(bad)}, which the review excluded or moved away")
            bindings[au["filename"]] = d["binding"]
    return kept, bindings, dropped


def _apply_moves(out: dict[str, dict], review: dict, problems: list[str]) -> int:
    """Pass 3. `out` maps cluster_id -> mutable verified cluster. Returns move count.

    Collect first, place second, so behaviour never depends on iteration order.
    """
    pending: list[tuple[str, dict, dict]] = []
    for s in review["stops"]:
        src = out.get(s["cluster_id"])
        if src is None:
            continue
        for d in s["photos"]:
            if not d.get("move_to"):
                continue
            orig = next((p for p in src.get("_photos_before", src["photos"])
                         if p["filename"] == d["filename"]), None)
            if orig is None:
                problems.append(f"CONFLICT {s['cluster_id']}: {d['filename']} is not in this stop")
                continue
            pending.append((d["move_to"], orig, src))
    for target_id, photo, src in pending:
        tgt = out.get(target_id)
        if tgt is None:
            problems.append(f"CONFLICT: {photo['filename']} moves to {target_id}, which is gone")
            continue
        if photo["filename"] not in [p["filename"] for p in tgt["photos"]]:
            tgt["photos"].append(photo)
            tgt["photos"].sort(key=lambda p: (p["ts"], p["filename"]))
            if photo["message_id"] not in tgt["message_ids"]:
                tgt["message_ids"].append(photo["message_id"])
                tgt["message_ids"].sort()
            # The message travels with its photo. A media message names exactly one
            # file, so leaving its id behind counts the same message in two stops
            # and quietly breaks the only completeness invariant this stage has.
            if photo["message_id"] in src["message_ids"] and \
                    photo["message_id"] not in [p["message_id"] for p in src["photos"]] and \
                    photo["message_id"] not in [a["message_id"] for a in src["audios"]]:
                src["message_ids"].remove(photo["message_id"])
            # The photo remembers the pin it was taken at. Moving it to another
            # asset does not move where it was taken, so the pin travels with it
            # and `pin_message_id` still resolves in the stop that now owns it.
            pin_id = photo.get("pin_message_id")
            if pin_id and pin_id not in {g["message_id"] for g in tgt.get("gps_points") or []}:
                pin = next((g for g in src.get("gps_points") or []
                            if g["message_id"] == pin_id), None)
                if pin:
                    tgt.setdefault("gps_points", []).append(copy.deepcopy(pin))
                    tgt["gps_points"].sort(key=lambda g: (g["ts"], g["message_id"]))
    return len(pending)


def _apply_discards(out: dict[str, dict], review: dict) -> tuple[list[dict], set[str]]:
    """Pass 4a. Drop discarded stops, recording the reviewer's reason.

    Returns the record and the message ids that left with it — those are the only
    messages allowed to go missing from the accounting.
    """
    discarded: list[dict] = []
    dropped_msgs: set[str] = set()
    for s in review["stops"]:
        if s["verdict"] != "discarded" or s["cluster_id"] not in out:
            continue
        dropped_msgs.update(out[s["cluster_id"]]["message_ids"])
        del out[s["cluster_id"]]
        discarded.append({"cluster_id": s["cluster_id"], "reason": s.get("human_note")})
    return discarded, dropped_msgs


def _apply_splits(out: dict[str, dict], review: dict) -> int:
    """Pass 4b. One stop becomes two at the named message."""
    n = 0
    for s in review["stops"]:
        cid = s["cluster_id"]
        if not s["split_at"] or s["merge_with"]:
            continue                      # conflicting instruction; already reported
        c = out.get(cid)
        if c is None:
            continue
        ids = c["message_ids"]
        if s["split_at"] not in ids or ids.index(s["split_at"]) == 0:
            continue                      # invalid split; already reported
        a, b = _split_cluster(c, s["split_at"])
        del out[cid]
        out[a["id"]], out[b["id"]] = a, b
        n += 1
    return n


def _apply_merges(out: dict[str, dict], review: dict, problems: list[str]) -> int:
    """Pass 4c. Stops the gap rule cut apart become one site.

    Merges are resolved as GROUPS, not as pairs. A reviewer splicing three
    over-cut stops back together writes two instructions — C002 → C001 and
    C003 → C001 — and pair-wise application consumed C001 on the first
    instruction, then found nothing to merge into on the second and dropped it
    silently at exit 0. Union-find puts every stop the reviewer connected into
    one group, so chains, shared targets and A↔B cycles all resolve to one site.

    The survivor is the earliest-starting member, and `origin` names every stop
    that went into it.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:          # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for s in review["stops"]:
        cid, target_id = s["cluster_id"], s["merge_with"]
        if not target_id or s["split_at"] or target_id == cid:
            continue                      # conflicting instruction; already reported
        if cid not in out or target_id not in out:
            problems.append(f"CONFLICT {cid}: merge_with {target_id} is no longer a stop "
                            f"(discarded or reshaped) — this merge was NOT applied")
            continue
        union(cid, target_id)

    groups: dict[str, list[str]] = {}
    for cid in parent:
        groups.setdefault(find(cid), []).append(cid)

    n = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda cid: (out[cid]["start_ts"], cid))
        merged = out[members[0]]
        for cid in members[1:]:
            merged = _merge_clusters(merged, out[cid])
            del out[cid]
        merged["id"] = members[0]
        merged["origin"] = "+".join(members)
        out[members[0]] = merged
        n += 1
    return n


def _split_cluster(c: dict, split_at: str) -> tuple[dict, dict]:
    ids = c["message_ids"]
    i = ids.index(split_at)
    a, b = copy.deepcopy(c), copy.deepcopy(c)
    first, second = set(ids[:i]), set(ids[i:])
    a["message_ids"], b["message_ids"] = ids[:i], ids[i:]
    for part, keep in ((a, first), (b, second)):
        # Each half keeps the pins dropped inside it. Copying the whole list to
        # both would give each half locations it was never at.
        part["gps_points"] = [g for g in c.get("gps_points") or [] if g["message_id"] in keep]
        part["photos"] = [p for p in c["photos"] if p["message_id"] in keep]
        part["audios"] = [x for x in c["audios"] if x["message_id"] in keep]
        part["texts"] = [t for t in c.get("texts", []) if t["message_id"] in keep]
        part["photo_captions"] = {k: v for k, v in c["photo_captions"].items()
                                  if k in [p["filename"] for p in part["photos"]]}
        part["audio_bindings"] = {k: v for k, v in c["audio_bindings"].items()
                                  if k in [x["filename"] for x in part["audios"]]}
    a["id"], b["id"] = c["id"] + "a", c["id"] + "b"
    a["origin"] = b["origin"] = c["id"]
    a["start_ts"], a["end_ts"] = _ts_range(a)
    b["start_ts"], b["end_ts"] = _ts_range(b)
    b["boundary_reason"] = "gap"
    # the second half inherits the pin unless it carried its own
    b["gps_inherited"] = True
    return a, b


def _merge_clusters(a: dict, b: dict) -> dict:
    m = copy.deepcopy(a)
    m["message_ids"] = sorted(set(a["message_ids"]) | set(b["message_ids"]))
    seen = {p["filename"] for p in m["photos"]}
    m["photos"] += [p for p in b["photos"] if p["filename"] not in seen]
    m["photos"].sort(key=lambda p: (p["ts"], p["filename"]))
    seen_a = {x["filename"] for x in m["audios"]}
    m["audios"] += [x for x in b["audios"] if x["filename"] not in seen_a]
    m["audios"].sort(key=lambda x: (x["ts"], x["filename"]))
    m["texts"] = (m.get("texts") or []) + (b.get("texts") or [])
    m["photo_captions"] = {**b["photo_captions"], **a["photo_captions"]}
    m["audio_bindings"] = {**b["audio_bindings"], **a["audio_bindings"]}
    m["gps"] = a["gps"] or b["gps"]
    m["gps_source"] = (a.get("gps_source") if a["gps"] else b.get("gps_source"))
    # One asset pinned more than once is the normal case under the dot convention,
    # and merging two stops means merging their pin lists — dropping b's would
    # lose the coordinate half of this asset's photographs were taken at.
    seen_pins = {g["message_id"] for g in m.get("gps_points") or []}
    m["gps_points"] = (m.get("gps_points") or []) + [
        g for g in b.get("gps_points") or [] if g["message_id"] not in seen_pins]
    m["gps_points"].sort(key=lambda g: (g["ts"], g["message_id"]))
    m["gps_inherited"] = a["gps_inherited"] and b.get("gps_inherited", True)
    m["origin"] = f"{a['id']}+{b['id']}"
    m["human_note"] = " · ".join(x for x in (a.get("human_note"), b.get("human_note")) if x) or None
    m["omitted_count"] = a.get("omitted_count", 0) + b.get("omitted_count", 0)
    m["senders"] = sorted(set(a.get("senders", [])) | set(b.get("senders", [])))
    m["start_ts"], m["end_ts"] = _ts_range(m)
    return m


def _ts_range(c: dict) -> tuple[str, str]:
    ts = [p["ts"] for p in c["photos"]] + [x["ts"] for x in c["audios"]] \
         + [t["ts"] for t in c.get("texts", [])]
    return (min(ts), max(ts)) if ts else (c["start_ts"], c["end_ts"])


def apply_review(clusters: dict, transcripts_en: dict, review: dict) -> tuple[dict, dict, list[str]]:
    """Return (clusters_verified, transcripts_en_verified, problems).

    problems is a list of human-readable strings. A non-empty list means the
    caller exits 3 (soft) or 4 (if any problem is a conflict — prefixed CONFLICT).

    Ordered passes; the order is load-bearing, each pass depends on the previous
    one having settled:

        pass 0  remap     — re-key verdicts onto current ids after an s5 --force
        pass 1  validate  — reject conflicting instructions before touching anything
        pass 2  per-stop  — exclusions, captions, bindings, text overrides, drops
        pass 3  moves     — relocate photos between stops
        pass 4  reshape   — discard, then split, then merge; renumber seq
    """
    problems: list[str] = []
    review = _remap_review(clusters, review, problems)
    problems.extend(_validate_instructions(clusters, review))
    stops = {s["cluster_id"]: s for s in review["stops"]}
    out: dict[str, dict] = {}
    dropped_audio_files: set[str] = set()
    text_overrides: dict[str, dict] = {}

    for c in clusters["clusters"]:
        stop = stops.get(c["id"])
        v = copy.deepcopy(c)
        v["origin"] = c["id"]
        v["photo_captions"] = {}
        v["audio_bindings"] = {}
        if stop is None:
            problems.append(f"{c['id']}: no verdict in review.json — carried through unverified")
            v["human_verified"] = False
            v["human_note"] = None
            out[c["id"]] = v
            continue
        if stop["verdict"] == "pending":
            problems.append(f"{c['id']}: still pending — not reviewed")
        # An auto-accepted stop (s5b --auto) is approved for the pipeline but no
        # person looked at it: it must not claim human verification, and its
        # placeholder note is not a human note that outranks the evidence.
        auto = stop.get("human_note") == AUTO_NOTE
        v["human_verified"] = stop["verdict"] == "approved" and not auto
        v["human_note"] = None if auto else stop.get("human_note")

        # A reviewer who re-pins a stop has looked at the building. That decision
        # is written by the review app and required by review.schema.json — and
        # was read by nothing here, so the machine's (often inherited) pin shipped
        # to the report anyway, under captions printing the wrong coordinates.
        override = stop.get("gps_override")
        if override:
            v["gps"] = {"lat": override["lat"], "lng": override["lng"]}
            v["gps_inherited"] = False
            v["gps_message_id"] = None
        v["gps_source"] = ("reviewer" if override
                           else None if v["gps"] is None
                           else "inherited" if v.get("gps_inherited") else "pin")

        kept_photos, captions, removed = _apply_photos(v, stop, problems)
        kept_audios, bindings, dropped = _apply_audios(v, stop, removed, problems)
        v["photos"] = kept_photos
        v["audios"] = kept_audios
        v["photo_captions"] = captions
        v["audio_bindings"] = bindings
        dropped_audio_files.update(dropped)
        text_overrides.update({
            a["file"]: a for a in stop["audios"]
            if a.get("text_override") or a.get("en_override")
        })
        out[c["id"]] = v

    _apply_moves(out, review, problems)

    discarded, discarded_msgs = _apply_discards(out, review)
    _apply_splits(out, review)
    _apply_merges(out, review, problems)

    out_clusters = sorted(out.values(), key=lambda c: (c["start_ts"], c["id"]))
    for i, c in enumerate(out_clusters, start=1):
        c["seq"] = i
        _strip_internal(c)

    cv = copy.deepcopy(clusters)
    cv["meta"] = dict(clusters["meta"])
    cv["meta"]["stage"] = STAGE
    cv["clusters"] = out_clusters
    cv["discarded"] = discarded
    cv["counts"] = _recount(out_clusters, clusters["counts"], discarded, discarded_msgs)
    if cv["counts"]["messages_unassigned"]:
        # Every message is in exactly one surviving stop, or it left with a stop
        # the reviewer discarded. Anything else means the applier lost or
        # duplicated evidence, which is not something to ship and re-derive later.
        problems.append(
            f"CONFLICT: message accounting does not balance — "
            f"{clusters['counts']['messages_in']} in, {cv['counts']['messages_assigned']} assigned, "
            f"{cv['counts']['messages_dropped']} discarded, "
            f"{cv['counts']['messages_unassigned']} unaccounted for")

    tv = copy.deepcopy(transcripts_en)
    tv["meta"] = dict(transcripts_en["meta"])
    tv["meta"]["stage"] = STAGE
    tv["items"] = []
    for item in transcripts_en["items"]:
        if item["file"] in dropped_audio_files:
            continue
        it = copy.deepcopy(item)
        ov = text_overrides.get(item["file"])
        if ov:
            it["human_edited"] = True
            it["text_machine"] = item["text_clean"]
            it["en_text_machine"] = item["en_text"]
            if ov.get("text_override"):
                it["text_clean"] = ov["text_override"]
            if ov.get("en_override"):
                it["en_text"] = ov["en_override"]
            # a human resolved the audio, so s6 must stop hedging about it
            it["unclear_spans"] = [] if ov.get("en_override") else it["unclear_spans"]
            # The corrected text is the authority on what was said, including the
            # spoken sector code — a reviewer may have supplied one the machine
            # transcript garbled, or removed one it hallucinated.
            it["sector_declared"] = (common.declared_sector(it["text_clean"])
                                     or common.declared_sector(it["en_text"]))
        else:
            it["human_edited"] = False
        tv["items"].append(it)
    tv["counts"] = {**transcripts_en["counts"], "input_items": len(transcripts_en["items"]),
                    "ok": sum(1 for i in tv["items"] if i["status"] == "ok")}

    return cv, tv, problems


def _strip_internal(cluster: dict) -> None:
    """Remove every `_`-prefixed working key — `additionalProperties: false`."""
    for k in [k for k in cluster if k.startswith("_")]:
        del cluster[k]


def _recount(out_clusters: list[dict], original_counts: dict, discarded: list[dict],
             discarded_msgs: set[str]) -> dict:
    """Recompute the counts block.

    `messages_unassigned` is the honest remainder, not a clamped one: it used to
    be `max(0, in - assigned)`, which silently absorbed a photo move that copied
    a message id into a second stop instead of moving it. Clamping the number
    disabled the only completeness check this stage has.
    """
    assigned = sum(len(c["message_ids"]) for c in out_clusters)
    return {
        "clusters": len(out_clusters),
        "messages_in": original_counts["messages_in"],
        "messages_assigned": assigned,
        "messages_dropped": len(discarded_msgs),
        "messages_unassigned": abs(original_counts["messages_in"] - assigned - len(discarded_msgs)),
        "clusters_with_gps": sum(1 for c in out_clusters if c["gps"]),
        "clusters_without_gps": sum(1 for c in out_clusters if not c["gps"]),
        "discarded": len(discarded),
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = common.load_config(args.config)
    run_dir = common.resolve_run_dir(cfg, args.run_dir)
    log = common.get_logger(run_dir, STAGE, args.log_level)

    clusters = common.read_stage(run_dir, "clusters", "clusters")
    transcripts_en = common.read_stage(run_dir, "transcripts_en", "transcripts_en")
    review = common.read_stage(run_dir, "review", "review")

    ups = [run_dir / f"{n}.json" for n in ("clusters", "transcripts_en", "review")]
    if not args.force and all(common.up_to_date(run_dir / f"{n}.json", cfg, ups)
                              for n in ("clusters_verified", "transcripts_en_verified")):
        print(f"SKIP {STAGE}: up to date")
        return 0

    cv, tv, problems = apply_review(clusters, transcripts_en, review)

    conflicts = [p for p in problems if p.startswith("CONFLICT")]
    for p in problems:
        log.warning(p)

    item_errors = [common.err(p.split(":")[0], "REVIEW_CONFLICT" if p.startswith("CONFLICT")
                              else "REVIEW_PROBLEM", p) for p in problems]

    cv_env = common.envelope(STAGE, cfg["site"], cfg,
                             {"clusters": len(clusters["clusters"])},
                             {"clusters": len(cv["clusters"])})
    cv["status"], cv["errors"], cv["meta"] = "ok", [], {**cv["meta"], **cv_env["meta"]}
    code = common.finalize(cv, len(clusters["clusters"]), len(cv["clusters"]), item_errors)

    tv["status"], tv["errors"] = "ok", []
    tv["meta"] = {**tv["meta"], **cv_env["meta"], "stage": STAGE,
                  "model": transcripts_en["meta"].get("model", "n/a"),
                  "prompt_version": transcripts_en["meta"].get("prompt_version", "n/a"),
                  "schema_retries": transcripts_en["meta"].get("schema_retries", 0)}

    common.write_stage(run_dir, "clusters_verified", cv, "clusters_verified")
    common.write_stage(run_dir, "transcripts_en_verified", tv, "transcripts_en")

    print(f"clusters_verified: {len(cv['clusters'])} stops, "
          f"{sum(1 for c in cv['clusters'] if c['human_verified'])} human-verified")
    if problems:
        print(f"problems: {len(problems)}")
        for p in problems[:10]:
            print(f"  - {p}")
    if conflicts:
        return 4
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except common.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    except common.MissingUpstream as e:
        print(f"missing upstream: {e}", file=sys.stderr)
        sys.exit(5)
    except Exception:
        traceback.print_exc()
        sys.exit(4)
