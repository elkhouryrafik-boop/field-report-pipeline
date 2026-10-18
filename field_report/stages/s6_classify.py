#!/usr/bin/env python
"""s6_classify.py - stage 6: stops -> findings.json (categories, rated assets, priorities).

    python -m field_report.stages.s6_classify --config <config> [--classify keyword|anthropic] [--offline]

Reads the verified stops (clusters_verified.json, else clusters.json), the English transcripts,
the captions/bindings from s5a and the profile. Each stop is split into *units* - a voice note
together with the photos it refers to, or a photo on its own - and each unit is classified by
the chosen backend: `keyword` applies the profile's keyword rules (deterministic, offline);
`anthropic` is the experimental Claude backend. Units of one stop that land in the same
category become one asset. Every asset cites its transcript/photo evidence, every photo is used
once, and categories nothing was recorded in stay as explicit N/A checklist rows.
A unit no rule can place is listed under `unclassified` - never guessed - and the stage exits 3.

A profile with `classify: false` (event-log) skips classification: each stop becomes one asset
in the single catch-all category, rated N/A.

Exit codes: 0 ok, 2 bad invocation/backend unavailable, 3 partial, 4 failed, 5 missing upstream.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from field_report.backends import BackendUnavailable
from field_report.backends.classify import Unit, get_classifier
from field_report.lib import common, profiles, stage_args

STAGE = "s6_classify"
PROMPT_VERSION = "1.0.0"
_STOP_PREFIX_RE = re.compile(r"^\s*(?:stop|sector)\s*[-:]?\s*(?:\d{1,2}|[a-z]+)\s*[,:.\-]?\s*", re.IGNORECASE)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="stage 6: classify stops into findings")
    stage_args.add_common(p)
    stage_args.add_offline(p)
    stage_args.add_classify(p)
    return p.parse_args(argv)


# --------------------------------------------------------------------------- helpers

def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def location_name(en_text: str) -> str | None:
    """'Stop two, east stair. Handrail ...' -> 'East stair' (the clause naming the place)."""
    first = sentences(en_text)[:1]
    if not first:
        return None
    head = _STOP_PREFIX_RE.sub("", first[0]).strip(" .,:;")
    if head and head != first[0].strip(" .,:;"):
        return head[:1].upper() + head[1:]
    return None


def caption_text(caption: str) -> str:
    """A caption without its trailing parenthetical (generator / scene metadata)."""
    return re.sub(r"\s*\([^)]*\)(?=[.\s]*$)", "", caption or "").strip()


def finding_text(en_text: str, explicit_rx: re.Pattern | None) -> str:
    """The note without its opening place clause and without the stated-rating sentence."""
    sents = sentences(en_text)
    if sents and location_name(en_text):
        sents = sents[1:]
    if explicit_rx is not None:
        sents = [s for s in sents if not explicit_rx.search(s)]
    return " ".join(sents).strip()


def worst(values: list[str], rank: dict[str, int]) -> str:
    known = [v for v in values if v in rank and v != profiles.NOT_APPLICABLE]
    return min(known, key=rank.get) if known else profiles.NOT_APPLICABLE


def overall_level(sector_ratings: list[str], rank: dict[str, int]) -> str:
    """The predominant rating across categories that have one; ties go to the more severe.

    Taking the maximum would let a single bad item define the whole site, which costs the
    headline its power to discriminate.
    """
    rated = [r for r in sector_ratings if r in rank and r != profiles.NOT_APPLICABLE]
    if not rated:
        return profiles.NOT_APPLICABLE
    counts: dict[str, int] = {}
    for r in rated:
        counts[r] = counts.get(r, 0) + 1
    return max(counts, key=lambda r: (counts[r], -rank[r]))


def build_units(cluster: dict, transcripts: dict, prelabel_caps: dict, prelabel_bind: dict) -> list[dict]:
    """Voice notes with their photos, then photos on their own. Every photo lands in exactly one unit."""
    caps = {**prelabel_caps, **{k: v for k, v in (cluster.get("photo_captions") or {}).items() if v}}
    bindings = cluster.get("audio_bindings") or {}
    photos = [p["filename"] for p in cluster["photos"]]
    used: set[str] = set()
    units = []
    for au in cluster["audios"]:
        t = transcripts.get(au["filename"])
        if not t:
            continue
        bound = [f for f in (bindings.get(au["filename"]) or prelabel_bind.get((cluster["id"], au["filename"]), []))
                 if f in photos and f not in used]
        used.update(bound)
        units.append({"transcript": t, "photos": bound})
    for f in photos:
        if f not in used:
            units.append({"transcript": None, "photos": [f]})
    for u in units:
        u["captions"] = [caps[f] for f in u["photos"] if caps.get(f)]
    return units


def quantity_row(q: dict, name: str) -> dict:
    return {"unit": q["unit"], "value": q["value"], "value_text": q.get("value_text"),
            "confirmed": bool(q["confirmed"]), "description": name, "note": q.get("note"),
            "verbatim": q.get("verbatim")}


# --------------------------------------------------------------------------- core

def classify_clusters(cfg: dict, prof: dict, classifier, clusters: list[dict], transcripts: dict,
                      prelabels: dict | None) -> tuple[dict, list[dict]]:
    """Pure assembly of the findings body. Returns (findings fields, item errors)."""
    rank = profiles.severity_rank(prof)
    explicit_rx = re.compile(prof["scale"]["explicit_regex"]) if prof["scale"].get("explicit_regex") else None
    pre_caps = {p["filename"]: p["caption"] for p in (prelabels or {}).get("photos", []) if p["caption"]}
    pre_rating = {p["filename"]: p.get("rating_guess") for p in (prelabels or {}).get("photos", [])}
    pre_cat = {p["filename"]: p.get("sector_guess") for p in (prelabels or {}).get("photos", [])}
    pre_bind = {(b["cluster_id"], b["audio"]): b["photos"] for b in (prelabels or {}).get("bindings", [])}
    cat_by_id = {c["id"]: c for c in prof["categories"]}

    assets_by_cat: dict[int, list[dict]] = {cid: [] for cid in cat_by_id}
    stops, unclassified, unassigned, item_errors = [], [], [], []
    seen_photos: set[str] = set()

    for seq, c in enumerate(clusters, 1):
        units = build_units(c, transcripts, pre_caps, pre_bind)
        groups: dict[int, list[dict]] = {}
        if not prof["classify"] and units:
            groups[prof["categories"][0]["id"]] = [
                {**u, "result": None, "rating": profiles.NOT_APPLICABLE} for u in units]
        else:
            for u in units:
                t = u["transcript"]
                hints_c = [cat_by_id[pre_cat[f]]["name"] for f in u["photos"] if pre_cat.get(f) in cat_by_id]
                hints_r = [pre_rating[f] for f in u["photos"] if pre_rating.get(f)]
                res = classifier.classify(Unit(cluster_id=c["id"], text=(t or {}).get("en_text", ""),
                                               captions=u["captions"], category_hints=hints_c,
                                               rating_hints=hints_r))
                if res.category_id is None:
                    unclassified.append({"cluster_id": c["id"], "reason": res.basis, "photos": u["photos"],
                                         "text": (t or {}).get("en_text")})
                    item_errors.append(common.err(c["id"], "UNCLASSIFIED", res.basis))
                    continue
                groups.setdefault(res.category_id, []).append({**u, "result": res, "rating": res.rating})

        if not units:
            unassigned.append({"cluster_id": c["id"], "reason": "no usable voice note or photo in this stop"})

        stop_asset_ids, stop_photos, stop_files = [], [], []
        declared = None
        for cid, group in sorted(groups.items()):
            first = group[0]
            t0 = first["transcript"]
            name = (location_name(t0["en_text"]) if t0 else None) or (
                first["captions"][0].split(" (")[0] if first["captions"] else f"Stop {seq}")
            bits = [finding_text(g["transcript"]["en_text"], explicit_rx) for g in group if g["transcript"]]
            if not any(bits):  # nothing was said about it: fall back to what the captions show
                bits = [caption_text(g["captions"][0]) for g in group if g["captions"]]
            finding = " ".join(b for b in bits if b).strip() or f"Stop {seq}: nothing described"
            photos_here = []
            for g in group:
                for f in g["photos"]:
                    if f not in seen_photos:
                        seen_photos.add(f)
                        photos_here.append(f)
            evidence, quantities = [], []
            for g in group:
                t = g["transcript"]
                if t:
                    declared = declared or t.get("sector_declared")
                    stop_files.append(t["file"])
                    evidence.append({"type": "transcript", "ref": Path(t["file"]).stem, "cluster_id": c["id"],
                                     "quote": t["en_text"][:160]})
                    quantities += [quantity_row(q, name) for q in t["quantities_mentioned"]]
            for f in photos_here:
                evidence.append({"type": "photo", "ref": Path(f).stem, "cluster_id": c["id"],
                                 "quote": pre_caps.get(f)})
            gps = None
            if c.get("gps"):
                gps = {"lat": c["gps"]["lat"], "lng": c["gps"]["lng"], "source_cluster": c["id"],
                       "inherited": bool(c.get("gps_inherited"))}
            rating = worst([g["rating"] for g in group], rank) if prof["classify"] else profiles.NOT_APPLICABLE
            n = len(assets_by_cat[cid]) + 1
            asset = {"id": f"S{cid}-{n}", "name": name, "severity": rating, "finding": finding,
                     "quantities": quantities, "photos": photos_here, "gps": gps,
                     "source_evidence": evidence, "cluster_id": c["id"],
                     "human_verified": bool(c.get("human_verified")),
                     "basis": "; ".join(sorted({g["result"].basis for g in group if g["result"]})) or None,
                     "map_marker": f"{prof['report']['unit']} {seq}"}
            assets_by_cat[cid].append(asset)
            stop_asset_ids.append(asset["id"])
            stop_photos += photos_here
        spoken = next((location_name(u["transcript"]["en_text"]) for u in units if u["transcript"]
                       and location_name(u["transcript"]["en_text"])), None)
        stop_name = spoken or (assets_by_cat[min(groups)][-1]["name"] if groups else "no findings")
        stops.append({"cluster_id": c["id"], "seq": seq, "declared_number": declared,
                      "title": f"{prof['report']['unit']} {seq}: {stop_name}", "start_ts": c.get("start_ts"),
                      "human_verified": bool(c.get("human_verified")), "asset_ids": stop_asset_ids,
                      "photos": stop_photos, "transcripts": stop_files})

    sectors, priorities = [], []
    label = prof["scale"]["label"].lower()
    for cat in prof["categories"]:
        cid, assets = cat["id"], assets_by_cat[cat["id"]]
        if assets:
            sev = worst([a["severity"] for a in assets], rank)
            sectors.append({"sector_id": cid, "sector_name": cat["name"], "sector_severity": sev,
                            "present": True, "assets": assets})
            action = (f"{len(assets)} item(s) recorded; highest {label}: {sev}." if prof["classify"]
                      else f"{len(assets)} stop(s) logged; no rating applied.")
            priorities.append({"sector_id": cid, "sector_short": cat["short"],
                               "asset_ids": [a["id"] for a in assets], "action": action})
        else:
            absent = {"id": f"S{cid}-1", "name": "No finding recorded", "severity": profiles.NOT_APPLICABLE,
                      "finding": prof["report"].get("absent_finding", "Nothing recorded."), "quantities": [],
                      "photos": [], "gps": None, "source_evidence": [], "cluster_id": None,
                      "human_verified": False, "basis": None, "map_marker": None}
            sectors.append({"sector_id": cid, "sector_name": cat["name"], "sector_severity": profiles.NOT_APPLICABLE,
                            "present": False, "assets": [absent]})
            priorities.append({"sector_id": cid, "sector_short": cat["short"], "asset_ids": [absent["id"]],
                               "action": prof["report"].get("absent_action", "N/A: nothing recorded.")})

    n_assets = sum(len(v) for v in assets_by_cat.values())
    level = overall_level([s["sector_severity"] for s in sectors if s["present"]], rank) if prof["classify"] \
        else profiles.NOT_APPLICABLE
    present = sum(1 for s in sectors if s["present"])
    if prof["classify"]:
        summary = (f"{n_assets} item(s) recorded across {present} of {len(sectors)} categories at "
                   f"{len(stops)} stop(s). Overall {label}: {level} (the most common category rating; ties go to the more severe).")
    else:
        summary = f"{n_assets} stop(s) logged in chronological order; no classification or rating applied."
    body = {"site": cfg["site"], "profile": prof["name"], "sectors": sectors, "stops": stops,
            "priorities": priorities, "overall": {"damage_level": level, "summary": summary}}
    if unclassified:
        body["unclassified"] = unclassified
    if unassigned:
        body["unassigned_clusters"] = unassigned
    return body, item_errors


def _run(cfg: dict, run_dir: Path, log, args) -> int:
    prof = profiles.profile(cfg)
    verified = (run_dir / "clusters_verified.json").is_file()
    clusters = common.read_stage(run_dir, "clusters_verified" if verified else "clusters",
                                 "clusters_verified" if verified else "clusters")
    ten_name = "transcripts_en_verified" if (run_dir / "transcripts_en_verified.json").is_file() \
        else "transcripts_en"
    ten = common.read_stage(run_dir, ten_name, "transcripts_en")
    prelabels = common.read_stage(run_dir, "prelabels", "prelabels") if (run_dir / "prelabels.json").is_file() else None
    if not verified:
        log.warning("no clusters_verified.json - the s5b review gate was skipped; every finding is unreviewed")

    settings = {"classify": args.classify, "profile": prof["name"]}
    out_path = run_dir / "findings.json"
    ups = [run_dir / f"{n}.json" for n in ("clusters_verified" if verified else "clusters", ten_name)]
    if prelabels is not None:
        ups.append(run_dir / "prelabels.json")
    if not args.force and common.up_to_date(out_path, cfg, ups, settings):
        print(f"SKIP {STAGE}: up to date")
        return 0

    classifier = get_classifier(args.classify, prof)
    transcripts = {i["file"]: i for i in ten["items"] if i["status"] in ("ok", "partial")}
    body, item_errors = classify_clusters(cfg, prof, classifier, clusters["clusters"], transcripts, prelabels)

    n_clusters = len(clusters["clusters"])
    n_assets = sum(len(s["assets"]) for s in body["sectors"] if s["present"])
    payload = common.envelope(STAGE, cfg["site"], cfg, {"clusters": n_clusters},
                              {"assets": n_assets, "unclassified": len(body.get("unclassified", []))})
    info = classifier.info()
    payload["meta"].update(model=str(info["model"]), prompt_version=str(info.get("prompt_version", PROMPT_VERSION)),
                           clusters_total=n_clusters, clusters_processed=n_clusters, clusters_failed=0,
                           schema_retries=0, human_reviewed=bool(verified) and all(
                               c.get("human_verified") for c in clusters["clusters"]),
                           settings=settings)
    payload.update(body)
    code = common.finalize(payload, n_clusters, n_assets, item_errors)
    try:
        common.write_stage(run_dir, "findings", payload, "findings")
    except ValueError as e:
        log.error("output schema invalid: %s", e)
        return 4
    log.info("findings: %d stops, %d assets, %d unclassified -> exit %d", len(body["stops"]), n_assets,
             len(body.get("unclassified", [])), code)
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
