# Profiles

A profile decides what a run assesses: the categories a stop can fall into, the rating scale, the words the deliverables use, and the regex that picks a stop number out of a voice note. Profiles are JSON files in `field_report/profiles/`, validated against `field_report/schemas/profile.schema.json` when loaded.

| Profile | Categories | Scale (most severe first) | Extras |
|---|---|---|---|
| `site-inspection` (default) | Structure, Building envelope, MEP, Fire and life safety, Access and circulation, Finishes | High, Medium, Low | |
| `damage-assessment` | Structure, Roof, Openings, Services, Access, External works | Destroyed, Severe, Moderate, Minor, None | writes `quantities.xlsx` |
| `event-log` | one catch-all, Events | none | `classify: false`; stops listed chronologically with transcript and photos |

## Use one

Set `"profile": "<name>"` in the run's `config.json`, or pass `--profile <name>` to `demo`.

## Write one

Copy the closest built-in file and edit it. Either drop it into `field_report/profiles/`, or keep it anywhere and point `FRP_PROFILE_DIR` at that folder.

```json
{
  "name": "roof-survey",
  "title": "Roof survey",
  "classify": true,
  "quantities_sheet": false,
  "font": { "name": "Calibri", "size_pt": 11 },
  "stop_regex": "(?i)\\b(?:roof|bay)\\s*(\\d{1,2})\\b",
  "categories": [
    { "id": 1, "name": "Membrane", "short": "Membrane",
      "keywords": { "blister": 3, "ponding": 3, "membrane": 2 } },
    { "id": 2, "name": "Drainage", "short": "Drainage",
      "keywords": { "gutter": 3, "outlet": 3, "downpipe": 2 } }
  ],
  "scale": {
    "label": "Priority",
    "values": ["High", "Medium", "Low"],
    "default": "Medium",
    "explicit_regex": "(?i)\\bpriority\\s*[:\\-]?\\s*(high|medium|low)\\b",
    "keywords": { "High": ["leak", "hazard"], "Low": ["cosmetic"] }
  },
  "report": {
    "title": "Roof survey report",
    "unit": "Bay", "unit_plural": "bays",
    "summary_heading": "Summary",
    "findings_heading": "Findings by area",
    "priorities_heading": "Priorities"
  }
}
```

Fields:

- `name`: lower-case letters, digits and hyphens. Must match the file name.
- `classify`: `false` skips classification; every stop becomes one entry in the single category.
- `quantities_sheet`: `true` makes s8 write `quantities.xlsx`.
- `font`: body font and size for `report.docx` (default Calibri 11).
- `stop_regex`: one capture group, the stop number (digits or an English number word up to twelve) that a voice note opens with. A number spoken in a note outranks anything inferred from a photo.
- `categories[].keywords`: term to weight, 1 to 5, matched as whole words or phrases, case-insensitive.
- `scale.values`: most severe first. `scale.explicit_regex` catches a rating the speaker stated outright ("priority high"). `scale.keywords` maps a value to phrases that imply it. Anything else gets `scale.default`.
- `report`: the headings and unit words used in the report and sheet. `absent_finding` and `absent_action` set the text for a category with nothing recorded.

Check that it loads:

```bash
python -c "from field_report.lib import profiles; print(profiles.load('roof-survey')['title'])"
```

The experimental `--classify anthropic` backend reads the same profile: it may answer only with a category from the file or `unclear`, and a rating from `scale.values`.
