"""Word content controls for the reviewer sheet — dropdowns the reviewer clicks.

python-docx has no API for content controls, so these build the `w:sdt` elements
directly. Two kinds are used:

  dropdown  — a picker with fixed choices ("Yes — as recorded", a severity, …)
  text      — a plain-text box for the rare free-text correction

Every control carries `w:tag` = the confirmation-sheet item key. Answers are read
back by tag, so the reviewer can sort, reorder or delete rows and ingestion still
maps each answer to the right asset.

Word desktop and Word Online both render these as click-to-choose controls; no
document protection and no macros are involved. If a reviewer types over them
anyway, `read_controls` still returns whatever text ended up inside.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _stable_id(tag: str) -> str:
    """Word wants a unique 31-bit id per control; derive it from the tag so a
    rebuilt sheet keeps the same ids."""
    return str(int(hashlib.sha256(tag.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31 - 1))


def _sdt_shell(tag: str, alias: str) -> tuple:
    sdt = OxmlElement("w:sdt")
    pr = OxmlElement("w:sdtPr")
    alias_el = OxmlElement("w:alias")
    alias_el.set(qn("w:val"), alias)
    tag_el = OxmlElement("w:tag")
    tag_el.set(qn("w:val"), tag)
    id_el = OxmlElement("w:id")
    id_el.set(qn("w:val"), _stable_id(tag))
    pr.append(alias_el)
    pr.append(tag_el)
    pr.append(id_el)
    sdt.append(pr)
    content = OxmlElement("w:sdtContent")
    sdt.append(content)
    return sdt, pr, content


def _content_paragraph(text: str, *, bold: bool = False, size_pt: int = 12,
                       font: str = "Times New Roman"):
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rfonts = OxmlElement("w:rFonts")
    for slot in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(slot), font)
    rpr.append(rfonts)
    if bold:
        rpr.append(OxmlElement("w:b"))
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(size_pt * 2))
    rpr.append(sz)
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    p.append(r)
    return p


def _clear_cell(cell) -> None:
    for child in list(cell._tc):
        if child.tag in (qn("w:p"), qn("w:tbl")):
            cell._tc.remove(child)


def _place(cell, sdt) -> None:
    """A table cell must end with a paragraph — a control as the last child makes
    Word offer to repair the file, so a bare paragraph always follows it."""
    _clear_cell(cell)
    cell._tc.append(sdt)
    cell._tc.append(OxmlElement("w:p"))
    cell._tc.append(OxmlElement("w:p"))


def insert_dropdown(cell, tag: str, options: list[str], *, alias: str = "Answer",
                    selected: str | None = None) -> None:
    """Replace the cell's content with a dropdown content control."""
    shown = selected or options[0]
    sdt, pr, content = _sdt_shell(tag, alias)
    dd = OxmlElement("w:dropDownList")
    dd.set(qn("w:lastValue"), shown)
    for opt in options:
        item = OxmlElement("w:listItem")
        item.set(qn("w:displayText"), opt)
        item.set(qn("w:value"), opt)
        dd.append(item)
    pr.append(dd)
    content.append(_content_paragraph(shown, bold=True))
    _place(cell, sdt)


def insert_text_box(cell, tag: str, placeholder: str = "", *, alias: str = "Correction") -> None:
    """Replace the cell's content with a plain-text content control."""
    sdt, pr, content = _sdt_shell(tag, alias)
    text_el = OxmlElement("w:text")
    pr.append(text_el)
    content.append(_content_paragraph(placeholder))
    _place(cell, sdt)


def read_controls(doc) -> dict[str, str]:
    """tag -> text currently inside the control. Accepts a Document or a path."""
    if isinstance(doc, (str, Path)):
        from docx import Document
        doc = Document(str(doc))
    values: dict[str, str] = {}
    for sdt in doc.element.body.iter(qn("w:sdt")):
        pr = sdt.find(qn("w:sdtPr"))
        if pr is None:
            continue
        tag_el = pr.find(qn("w:tag"))
        if tag_el is None:
            continue
        tag = tag_el.get(qn("w:val"))
        content = sdt.find(qn("w:sdtContent"))
        text = "".join(t.text or "" for t in content.iter(qn("w:t"))) if content is not None else ""
        values[tag] = text.strip()
    return values
