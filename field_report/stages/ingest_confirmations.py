#!/usr/bin/env python
"""Ingest reviewer answers from the review sheet back into findings.json.

Two input routes, same effect:
  --from-docx <filled review-sheet.docx>   read the dropdown controls out of the Word file
  (default)                                read "answer" fields already set in confirmation_sheet.json

What is patched automatically (deterministic, no judgement):
  * quantity rows   - an affirmative answer confirms the recorded figure; a figure in the answer
                      replaces it. Either way confirmed:true and the reviewer is recorded as the
                      evidence source, so the highlight in quantities.xlsx disappears.
  * rating rows     - the answer must be exactly one value of the profile's scale; the category and
                      overall ratings are recomputed with the same rules s6 uses.
  * empty categories - "Yes - as recorded" acknowledges that nothing was found there.

What is never patched automatically (recorded in confirmation_patches.json -> needs_authoring):
  * an answer that qualifies a figure ("about 300, but check") rather than settling it
  * a "No" with the correction cell left empty
  * a missing photo or missing media file - the media has to go through s2 again, not be pasted in.

Outputs: patched findings.json (backed up first) and <run-dir>/confirmation_patches.json =
{applied[], needs_authoring[], unanswered[]}. Re-run s7-s10 afterwards to rebuild the deliverables.

    python -m field_report ingest-review --config <config> --from-docx review-sheet.docx [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


from field_report.lib import common  # noqa: E402
from field_report.lib import profiles  # noqa: E402
from field_report.stages import s6_classify  # noqa: E402  (shared rating rollup)
from field_report.lib import docx_controls  # noqa: E402

MARKER_RE = re.compile(r"\s*\[to be (?:confirmed|verified)[^\]]*\]")
AFFIRM_RE = re.compile(
    r"^\s*(ok|okay|yes|correct|confirmed|as stated|agreed|no change|verified)\b[\s.!]*$", re.I)
DECLINE_RE = re.compile(r"^\s*(unknown|tbd|pending|n/?a|not known|no answer)\b[\s.!]*$", re.I)
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
RANGE_RE = re.compile(r"\d[\d,.]*\s*(?:to|-|–)\s*\d[\d,.]*")


UNSET = ("— choose —", "- choose -", "")
# The dropdown's own affirmative, and nothing else. s9 writes "Yes — as recorded".
YES_ANSWER_RE = re.compile(r"^\s*confirmed\s*$", re.I)
YES_OPTION_RE = re.compile(r"^\s*yes\s*(?:[—–-]\s*as\s+recorded)?\s*[.!]?\s*$", re.I)


def read_answers_from_docx(path: Path) -> dict[str, str]:
    """key -> answer, read from the sheet's content controls.

    The reviewer picks from a dropdown ("Yes — as recorded" / "No — see correction" /
    "Don't know yet", or a severity), and types only when correcting a figure. Each
    control is tagged with the item key, so row order does not matter. A sheet whose
    controls were stripped (converted to plain text, pasted into another editor) is
    still read via the Answer column."""
    from docx import Document

    doc = Document(str(path))
    raw = docx_controls.read_controls(doc)
    answers: dict[str, str] = {}
    for tag, value in raw.items():
        if tag.endswith("#correction") or value in UNSET:
            continue
        if YES_OPTION_RE.match(value):
            answers[tag] = "confirmed"
        elif value.lower().startswith("yes"):
            # "Yes but only 3 of the 5 poles" is a correction typed over the
            # dropdown, not the dropdown's own "Yes — as recorded". Keeping it
            # verbatim sends it down the normal path, which will not invent a
            # confirmation out of it.
            answers[tag] = value
        elif value.lower().startswith("no"):
            correction = raw.get(f"{tag}#correction", "").strip()
            if correction:
                answers[tag] = correction
            else:
                answers[tag] = "__NO_CORRECTION_GIVEN__"
        elif value.lower().startswith(("don't know", "dont know", "keep ")):
            continue  # explicitly left open / severity unchanged
        else:
            answers[tag] = value  # a severity, or free text typed over the control
    if answers:
        return answers

    # fallback: a sheet whose controls are gone — read the Answer column by row number
    by_number: dict[int, str] = {}
    for table in doc.tables:
        header = [c.text.strip().lower() for c in table.rows[0].cells]
        if "answer" not in header:
            continue
        a_col = header.index("answer")
        for row in table.rows[1:]:
            try:
                item_no = int(row.cells[0].text.strip())
            except ValueError:
                continue
            text = row.cells[a_col].text.strip()
            if text and text not in UNSET:
                by_number[item_no] = text
    return {f"#{n}": v for n, v in by_number.items()}


# Words that turn a figure into an estimate. A reviewer who hedges has not
# confirmed anything, whatever else the sentence contains.
HEDGE_RE = re.compile(r"\b(about|approx\w*|around|roughly|circa|near(?:ly)?|est\w*|~)\b|~", re.I)
# Wording that makes the figure conditional, disputed or still open. A plain
# note of provenance ("1,150 m after re-measurement") is fine; a caveat
# ("1,150 but check on site") is not something to record as a measurement.
CAVEAT_RE = re.compile(
    r"\?|\b(but|however|though|although|if|unless|pending|tbc|to be confirmed|check|"
    r"double[- ]?check|verify|confirm with|await|awaiting|not sure|unsure|maybe|"
    r"possibly|either|versus|vs)\b", re.I)


def parse_quantity_answer(answer: str, q: dict) -> tuple[dict | None, str]:
    """Return (patch to apply to the quantity, description) or (None, reason).

    A `None` patch means the answer needs a human: it is never turned into a
    confirmed figure. This used to end in a catch-all that wrote the reviewer's
    whole sentence into `value_text` with `confirmed: true`, so "roughly 300, but
    check on site" printed in the Quantity column as a confirmed quantity,
    with the "to be confirmed" remark and its highlight removed.
    """
    if AFFIRM_RE.match(answer):
        return {"confirmed": True}, f"confirmed as recorded ({q.get('value', q.get('value_text'))})"

    if CAVEAT_RE.search(answer):
        return None, ("the answer qualifies the figure rather than settling it — read it and "
                      "apply what it means")

    hedged = bool(HEDGE_RE.search(answer))
    span = RANGE_RE.search(answer)
    if span:
        return ({"confirmed": not hedged, "value": None, "value_text": span.group(0)},
                f"value_text set to '{span.group(0)}'"
                + ("; left open — the reviewer gave it as an estimate" if hedged else ""))

    numbers = NUMBER_RE.findall(answer)
    if len(numbers) == 1:
        value = float(numbers[0].replace(",", ""))
        value = int(value) if value.is_integer() else value
        if hedged:
            # "about 300" is an estimate. Recording it as a confirmed 300 turns
            # the reviewer's approximation into a measurement.
            return ({"confirmed": False, "value": None, "value_text": answer.strip()},
                    f"recorded as the reviewer's estimate ({answer.strip()}), still open")
        return ({"confirmed": True, "value": value, "value_text": None},
                f"value set to {value}")
    if not numbers:
        return None, "no figure in the answer — read it and apply what it means"
    return None, f"{len(numbers)} figures in one answer — apply the right one by hand"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest confirmation-sheet answers into findings.json")
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--from-docx", default=None)
    ap.add_argument("--revision", default=None, help="revision label recorded on the evidence ref")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = common.load_config(args.config)
    run_dir = common.resolve_run_dir(cfg, args.run_dir)

    sheet_path = run_dir / "confirmation_sheet.json"
    if not sheet_path.is_file():
        print(f"missing {sheet_path} — run s9 first", file=sys.stderr)
        return 5
    sheet = json.loads(sheet_path.read_text(encoding="utf-8"))
    revision = args.revision or sheet.get("revision", "Rev1")

    if args.from_docx:
        answers = read_answers_from_docx(Path(args.from_docx))
        for item in sheet["items"]:
            value = answers.get(item["key"], answers.get(f"#{item['item_no']}"))
            if value:
                item["answer"] = value

    findings_path = run_dir / "findings.json"
    findings = json.loads(findings_path.read_text(encoding="utf-8"))

    prof = profiles.load(findings.get("profile") or profiles.DEFAULT_PROFILE)
    rank = profiles.severity_rank(prof)
    scale = profiles.scale_values(prof)
    assets = {a["id"]: a for s in findings["sectors"] for a in s.get("assets", [])}
    evidence_ref = {"type": "reviewer", "ref": f"reviewer-confirmation {revision}",
                    "cluster_id": None, "quote": "confirmed by the reviewer"}

    applied: list[dict] = []
    needs_authoring: list[dict] = []
    unanswered: list[dict] = []

    for item in sheet["items"]:
        answer = (item.get("answer") or "").strip()
        if not answer or DECLINE_RE.match(answer):
            unanswered.append({"item_no": item["item_no"], "key": item["key"]})
            continue
        if answer == "__NO_CORRECTION_GIVEN__":
            needs_authoring.append({
                "key": item["key"], "asset_id": item.get("asset_id"), "answer": None,
                "reason": "marked as not correct, but the correction cell was left empty — "
                          "ask the reviewer for the right value"})
            continue
        kind, key = item["kind"], item["key"]

        if kind == "quantity":
            aid, idx = key.split("/qty/")
            asset = assets.get(aid)
            if not asset or int(idx) >= len(asset.get("quantities", [])):
                needs_authoring.append({"key": key, "answer": answer,
                                        "reason": "quantity no longer exists at that index"})
                continue
            q = asset["quantities"][int(idx)]
            patch, described = parse_quantity_answer(answer, q)
            if patch is None:
                needs_authoring.append({"key": key, "asset_id": aid, "answer": answer,
                                        "current": {"value": q.get("value"),
                                                    "value_text": q.get("value_text"),
                                                    "unit": q.get("unit")},
                                        "reason": described})
                continue
            q.update(patch)
            # Stamped once, and honestly: re-ingesting the same sheet appended
            # "; confirmed by reviewer (Rev3)" again every run, into a note that
            # renders in the quantities sheet.
            stamp = (f"confirmed by reviewer ({revision})" if patch.get("confirmed")
                     else f"reviewer estimate ({revision}) — still to be confirmed")
            note = (q.get("note") or "").strip()
            if stamp not in note:
                q["note"] = (note + "; " if note else "") + stamp
            if evidence_ref not in (asset.get("source_evidence") or []):
                asset.setdefault("source_evidence", []).append(dict(evidence_ref))
            applied.append({"key": key, "asset_id": aid, "change": described})

        elif kind == "finding_marker":
            aid = key.split("/marker/")[0]
            asset = assets.get(aid)
            if not asset:
                needs_authoring.append({"key": key, "answer": answer, "reason": "asset not found"})
                continue
            if AFFIRM_RE.match(answer):
                before = asset["finding"]
                # Only the marker this row asked about. `MARKER_RE.sub` is global,
                # so answering one open item cleared every other open item on the
                # same asset — while the sheet still said "to be confirmed".
                spans = list(MARKER_RE.finditer(before))
                offset = key.split("/marker/")[-1]
                # s9 keys the item on the position of the "[", while MARKER_RE
                # here also swallows the space in front of it. Compare brackets.
                target = next((m for m in spans
                               if str(before.index("[", m.start())) == offset), None)
                if target is None and len(spans) == 1:
                    target = spans[0]     # the prose moved; there is only one to clear
                if target is None:
                    needs_authoring.append({
                        "key": key, "asset_id": aid, "answer": answer,
                        "current_finding": before,
                        "reason": f"the finding now carries {len(spans)} open-item markers and none "
                                  f"is at the recorded position — clear the right one by hand"})
                    continue
                asset["finding"] = (before[:target.start()] + before[target.end():])
                asset["finding"] = re.sub(r"\s{2,}", " ", asset["finding"]).strip()
                if evidence_ref not in (asset.get("source_evidence") or []):
                    asset.setdefault("source_evidence", []).append(dict(evidence_ref))
                applied.append({"key": key, "asset_id": aid,
                                "change": "open-item marker removed from the finding"})
            else:
                needs_authoring.append({
                    "key": key, "asset_id": aid, "answer": answer,
                    "current_finding": asset["finding"],
                    "reason": "reviewer supplied new substance — rewrite the finding paragraph, "
                              "then remove the marker"})

        elif kind == "severity":
            aid = key.split("/severity")[0]
            asset = assets.get(aid)
            if not asset:
                needs_authoring.append({"key": key, "answer": answer, "reason": "asset not found"})
                continue
            if AFFIRM_RE.match(answer):
                asset["human_verified"] = True      # a person looked at the rating and kept it
                applied.append({"key": key, "asset_id": aid,
                                "change": f"severity kept at {asset.get('severity')}"})
                continue
            # Exact match on the dropdown's own values. A substring search read
            # "not severe, more like Minor" as Severe — inverting the reviewer,
            # and then re-rating the whole sector off that inversion.
            cleaned = re.sub(r"^\s*(keep|change to|set to|rate as)\b[\s:]*", "", answer,
                             flags=re.I).strip(" .;")
            wanted = next((s for s in scale if s.lower() == cleaned.lower()), None)
            if not wanted:
                needs_authoring.append({
                    "key": key, "asset_id": aid, "answer": answer,
                    "reason": f"answer is not exactly one of {scale} — set the rating "
                              f"by hand rather than guessing which word was meant"})
                continue
            before = asset.get("severity")
            asset["severity"] = wanted
            asset["human_verified"] = True
            if evidence_ref not in (asset.get("source_evidence") or []):
                asset.setdefault("source_evidence", []).append(dict(evidence_ref))
            applied.append({"key": key, "asset_id": aid,
                            "change": f"severity {before} -> {wanted}"})

        elif kind == "no_photo":
            needs_authoring.append({
                "key": key, "asset_id": key.split("/")[0], "answer": answer,
                "reason": "if photographs were supplied, copy them into the media dir, re-run s2, "
                          "then assign them to the asset; do not paste them into findings"})

        elif kind == "sector_absence":
            if YES_ANSWER_RE.match(answer) or AFFIRM_RE.match(answer):
                applied.append({"key": key, "change": "reviewer confirmed nothing to report in this category"})
            else:
                needs_authoring.append({"key": key, "answer": answer,
                                        "reason": "the reviewer says something was found here - add the evidence "
                                                  "to the chat export and re-run from s1"})

        elif kind == "gap":
            needs_authoring.append({"key": key, "answer": answer,
                                    "reason": "supply the missing file in the export folder, then re-run from s2"})

        else:  # no mechanical target for this kind
            needs_authoring.append({"key": key, "answer": answer,
                                    "reason": "authored item — apply by hand where it belongs"})

    # A changed asset rating re-rates its sector, and a changed sector rating can
    # move the overall rating. Both use s6_classify's rules so a reviewer's answer lands on
    # exactly the rating the build would have produced — this used to keep its own
    # worst-asset copy, which drifted the moment the rollup rule changed.
    rerated: list[dict] = []
    if any(p["key"].endswith("/severity") for p in applied):
        for sector in findings["sectors"]:
            if not sector.get("present", True):
                continue
            new_sev = s6_classify.worst([a["severity"] for a in sector.get("assets", [])], rank)
            if new_sev != sector.get("sector_severity"):
                rerated.append({"sector_id": sector["sector_id"],
                                "from": sector.get("sector_severity"), "to": new_sev})
                sector["sector_severity"] = new_sev
        if rerated and findings.get("overall"):
            new_overall = s6_classify.overall_level([x["sector_severity"] for x in findings["sectors"] if x.get("present", True)], rank)
            override = findings["overall"].get("override")
            if override:
                # A human set this level against the rollup and recorded why.
                # Re-deriving it here would undo that decision at the reviewer
                # round-trip, silently and without anyone reading a line about
                # it. Track what the rule now gives so the departure stays
                # visible, and leave the level alone.
                override["derived"] = new_overall
            elif new_overall != findings["overall"].get("damage_level"):
                rerated.append({"sector_id": None,
                                "from": findings["overall"].get("damage_level"),
                                "to": new_overall})
                findings["overall"]["damage_level"] = new_overall

    payload = {
        "status": "ok",
        "errors": [],
        "sectors_rerated": rerated,
        "meta": {
            "stage": "ingest_confirmations",
            "site": cfg["site"],
            "revision": revision,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": args.from_docx or str(sheet_path),
            "dry_run": bool(args.dry_run),
        },
        "applied": applied,
        "needs_authoring": needs_authoring,
        "unanswered": unanswered,
    }

    print(f"applied {len(applied)} | needs authoring {len(needs_authoring)} | "
          f"unanswered {len(unanswered)}")
    for row in rerated:
        print(f"  SECTOR {row['sector_id']} re-rated {row['from']} -> {row['to']}")
    for row in needs_authoring:
        print(f"  AUTHOR {row['key']}: {row['reason']}")

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    # Timestamped, so a second ingest of the same revision cannot overwrite the
    # pre-answer state with the already-patched one.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(findings_path,
                 findings_path.with_name(f"findings_before_{revision}_answers_{stamp}.json"))
    findings["meta"]["reviewer_answers_ingested"] = revision
    # Through write_stage: schema-validated and atomically replaced. A raw
    # write_text here could leave findings.json malformed on an interrupt, and
    # nothing downstream except s7 revalidates it.
    try:
        common.write_stage(run_dir, "findings", findings, "findings")
    except ValueError as e:
        print(f"refusing to write findings.json: the patched document is schema-invalid: {e}",
              file=sys.stderr)
        return 4
    sheet_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")

    out = run_dir / "confirmation_patches.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, out)
    return 3 if needs_authoring else 0


if __name__ == "__main__":
    sys.exit(main())
