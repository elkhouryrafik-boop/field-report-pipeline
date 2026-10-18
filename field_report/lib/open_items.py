"""Open items: the single definition of "what is still unconfirmed" in a run.

The report (s7) brackets each one inline, the reviewer sheet (s9) lists each one with a
dropdown, and the gate (s10) recomputes the list and checks that the three agree. One
function, three readers - so they cannot drift.

Kinds (the confirmation_sheet schema enum):
  quantity        a spoken figure nobody has confirmed          -> "[to be confirmed: quantity]"
  no_photo        a finding with no photograph                  -> "[to be confirmed: no photo]"
  severity        a rating that no human has looked at          (sheet only; the report says "unreviewed")
  sector_absence  a category nothing was recorded in            (sheet only)
  gap             a media file the export lacks                 (sheet only; also listed in the report)
"""
from __future__ import annotations

KEEP_OPTION = "Keep current rating"
CHOOSE = "— choose —"
ANSWER_OPTIONS = [CHOOSE, "Yes — as recorded", "No — see correction", "Don't know yet"]

QUANTITY_MARKER = "[to be confirmed: quantity]"
NO_PHOTO_MARKER = "[to be confirmed: no photo]"


def quantity_text(q: dict) -> str:
    """The figure as the report prints it: value + unit (or the stated text)."""
    unit = "" if q.get("unit") in (None, "other") else (" no." if q["unit"] == "no" else f" {q['unit']}")
    if q.get("value") is not None:
        v = q["value"]
        v = int(v) if isinstance(v, float) and v.is_integer() else v
        return f"{v}{unit}"
    return f"{q.get('value_text') or '?'}{unit}"


def _transcript_source(asset: dict) -> str:
    refs = [e["ref"] for e in asset.get("source_evidence", []) if e.get("type") == "transcript"]
    return ("Voice note " + ", ".join(refs)) if refs else "Field team"


def _gap_what(gap: dict) -> str:
    when = (gap.get("ts") or "")[11:16] or "an unknown time"
    reason = {"media_omitted": "a media file was left out of the export",
              "file_missing_on_disk": f"the file {gap.get('filename')} is missing from the export folder",
              "unreadable_file": f"the file {gap.get('filename')} could not be read",
              "unparsable_filename": f"the file {gap.get('filename')} has an unrecognised name"}
    return f"At {when}, {reason.get(gap.get('reason'), 'a media file is missing')}."


def collect(findings: dict, gaps: dict | None, prof: dict) -> list[dict]:
    """Open items in a stable order (category, asset, kind); item_no is assigned last."""
    label = prof["scale"]["label"].lower()
    items: list[dict] = []
    for sector in findings["sectors"]:
        sid, sname = sector["sector_id"], sector["sector_name"]
        if not sector.get("present", True):
            if prof["classify"]:
                items.append({
                    "key": f"S{sid}/absence", "kind": "sector_absence", "scope": "report",
                    "sector_id": sid, "sector_name": sname, "asset_id": None, "asset_name": None,
                    "what": f"Nothing was recorded for {sname}.",
                    "why": "No voice note or photo of the walk was placed in this category.",
                    "question": f"Is it right that there is nothing to report for {sname}?",
                    "source": "Field team", "current_value": None, "unit": None})
            continue
        for a in sector["assets"]:
            base = {"scope": "asset", "sector_id": sid, "sector_name": sname,
                    "asset_id": a["id"], "asset_name": a["name"]}
            src = _transcript_source(a)
            for i, q in enumerate(a.get("quantities", [])):
                if q.get("confirmed"):
                    continue
                shown = quantity_text(q)
                items.append({**base, "key": f"{a['id']}/qty/{i}", "kind": "quantity",
                              "what": f"Quantity: {shown}" + (f" (said: \"{q['verbatim']}\")" if q.get("verbatim") else ""),
                              "why": q.get("note") or "Spoken in a voice note and not measured.",
                              "question": f"Is {shown} the right figure for {a['name']}?",
                              "source": src, "current_value": shown, "unit": q.get("unit")})
            if not a.get("photos"):
                items.append({**base, "key": f"{a['id']}/photo", "kind": "no_photo",
                              "what": f"No photograph is attached to {a['name']}.",
                              "why": "The finding comes from a voice note alone.",
                              "question": f"Is there a photograph of {a['name']} to add?",
                              "source": src, "current_value": None, "unit": None})
            if prof["classify"] and not a.get("human_verified"):
                items.append({**base, "key": f"{a['id']}/severity", "kind": "severity",
                              "what": f"{prof['scale']['label']}: {a['severity']}",
                              "why": a.get("basis") or "Assigned by keyword rules; nobody has reviewed it.",
                              "question": f"Is {a['severity']} the right {label} for {a['name']}?",
                              "source": src, "current_value": a["severity"], "unit": None})
    for g in (gaps or {}).get("gaps", []):
        items.append({"key": f"gap/{g.get('message_id') or g.get('filename')}", "kind": "gap", "scope": "report",
                      "sector_id": None, "sector_name": None, "asset_id": None, "asset_name": None,
                      "what": _gap_what(g),
                      "why": "The chat shows the file was sent, but it is not in the export.",
                      "question": "Can the original be supplied, or should it be left out?",
                      "source": "Chat export", "current_value": None, "unit": None})
    for n, it in enumerate(items, 1):
        it["item_no"] = n
        it["answer"] = None
    return items


def rating_options(prof: dict) -> list[str]:
    return [KEEP_OPTION] + list(prof["scale"]["values"])


def options_for(item: dict, prof: dict) -> list[str]:
    return rating_options(prof) if item["kind"] == "severity" else ANSWER_OPTIONS


def report_markers(findings: dict) -> int:
    """How many inline [to be confirmed ...] markers s7 prints for this findings.json."""
    n = 0
    for sector in findings["sectors"]:
        if not sector.get("present", True):
            continue
        for a in sector["assets"]:
            n += sum(1 for q in a.get("quantities", []) if not q.get("confirmed"))
            n += 0 if a.get("photos") else 1
    return n


__all__ = ["collect", "options_for", "rating_options", "report_markers", "quantity_text",
           "QUANTITY_MARKER", "NO_PHOTO_MARKER"]
