"""Fill the dropdowns of a review-sheet.docx programmatically (a test and demo helper).

    python -m field_report.tools.fill_review_sheet review-sheet.docx filled.docx --yes-all
    python -m field_report.tools.fill_review_sheet review-sheet.docx filled.docx \
        --answer "S1-1/qty/0=No — see correction" --answer "S1-1/qty/0#correction=3 m"

Answers are matched to content controls by tag (the item key), exactly as `ingest-review` reads
them back. `--yes-all` answers "Yes — as recorded" on every row that has a Yes option and leaves
rating rows on "Keep current rating". A real reviewer does the same thing by clicking in Word.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from field_report.lib import docx_common as dc
from field_report.lib.open_items import ANSWER_OPTIONS

YES = ANSWER_OPTIONS[1]


def fill(src: Path, dst: Path, answers: dict[str, str] | None = None, yes_all: bool = False) -> int:
    """Write `dst` = `src` with the given answers set. Returns how many controls were changed."""
    answers = dict(answers or {})
    doc = Document(str(src))
    changed = 0
    for sdt in doc.element.body.iter(qn("w:sdt")):
        pr = sdt.find(qn("w:sdtPr"))
        tag = pr.find(qn("w:tag")).get(qn("w:val"))
        value = answers.get(tag)
        dropdown = pr.find(qn("w:dropDownList"))
        if value is None and yes_all and dropdown is not None:
            options = [i.get(qn("w:value")) for i in dropdown.findall(qn("w:listItem"))]
            value = YES if YES in options else None
        if value is None:
            continue
        if dropdown is not None:
            options = [i.get(qn("w:value")) for i in dropdown.findall(qn("w:listItem"))]
            if value not in options:
                raise ValueError(f"{tag}: {value!r} is not one of {options}")
            dropdown.set(qn("w:lastValue"), value)
        texts = list(sdt.find(qn("w:sdtContent")).iter(qn("w:t")))
        texts[0].text = value
        for extra in texts[1:]:
            extra.text = ""
        changed += 1
    dc.save_docx(doc, dst)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--answer", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--yes-all", action="store_true")
    args = ap.parse_args(argv)
    answers = dict(a.split("=", 1) for a in args.answer)
    n = fill(Path(args.src), Path(args.dst), answers, args.yes_all)
    print(f"{n} control(s) filled -> {args.dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
