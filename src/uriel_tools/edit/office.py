"""Word (.docx), LibreOffice (.odt) and plain-text edits. Each function returns new bytes."""

import io

import docx
from docx.text.paragraph import Paragraph
from odf import teletype
from odf.element import Text
from odf.namespaces import TEXTNS
from odf.opendocument import load as load_odf

from uriel_tools.edit.core import Item, Result
from uriel_tools.rag.extract import DOCX, ODT


def inspect(data: bytes, mime: str) -> dict:
    if mime == DOCX:
        doc = docx.Document(io.BytesIO(data))
        return {"type": "docx", "paragraphs": len(list(_paragraphs(doc))), "comments": True}
    if mime == ODT:
        doc = load_odf(io.BytesIO(data))
        return {"type": "odt", "paragraphs": len(list(_odf_paragraphs(doc.text))), "comments": False}
    return {"type": "text", "characters": len(data.decode("utf-8", errors="replace"))}


# ---- Word


def _paragraphs(doc):
    yield from doc.paragraphs
    for table in doc.tables:
        yield from _table_paragraphs(table)


def _table_paragraphs(table):
    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs
            for inner in cell.tables:
                yield from _table_paragraphs(inner)


def _span(paragraph: Paragraph, start: int, end: int) -> tuple[list, int, int]:
    """The runs covering characters [start, end) of the paragraph, and the offsets into the first and last."""
    runs, pos, covered = paragraph.runs, 0, []
    first_off = last_off = 0
    for run in runs:
        length = len(run.text)
        if pos + length > start and pos < end:
            if not covered:
                first_off = start - pos
            covered.append(run)
            last_off = end - pos
        pos += length
    return covered, first_off, last_off


def _replace_in_paragraph(paragraph: Paragraph, find: str, replace: str) -> int:
    """Replace every occurrence, even when Word split the text across runs. The replacement takes the
    formatting of the run where the match starts; the rest of the match is removed from the following runs."""
    count, start = 0, paragraph.text.find(find)
    while start >= 0:
        runs, first_off, last_off = _span(paragraph, start, start + len(find))
        if not runs:
            break
        first, last = runs[0], runs[-1]
        if first is last:
            first.text = first.text[:first_off] + replace + first.text[last_off:]
        else:
            first.text = first.text[:first_off] + replace
            for middle in runs[1:-1]:
                middle.text = ""
            last.text = last.text[last_off:]
        count += 1
        start = paragraph.text.find(find, start + len(replace))
    return count


def _docx_edit(data: bytes, changes: list[tuple[str, str]], notes: list[tuple[str, str]]) -> Result:
    doc = docx.Document(io.BytesIO(data))
    result = Result(b"")
    for find, replace in changes:
        n = sum(_replace_in_paragraph(p, find, replace) for p in _paragraphs(doc))
        if n:
            result.items.append(Item(find, replace))
        else:
            result.not_found.append(find)
    for anchor, text in notes:
        target = next((p for p in _paragraphs(doc) if anchor in p.text), None)
        if target is None:
            result.not_found.append(anchor)
            continue
        start = target.text.find(anchor)
        runs, _, _ = _span(target, start, start + len(anchor))
        doc.add_comment(runs, text=text, author="Uriel", initials="U")
        result.items.append(Item(anchor, text))
    out = io.BytesIO()
    doc.save(out)
    result.data = out.getvalue()
    return result


# ---- LibreOffice


def _odf_paragraphs(node):
    for child in getattr(node, "childNodes", []):
        if getattr(child, "qname", None) in ((TEXTNS, "p"), (TEXTNS, "h")):
            yield child
        else:
            yield from _odf_paragraphs(child)


def _text_nodes(node):
    for child in getattr(node, "childNodes", []):
        if isinstance(child, Text):
            yield child
        else:
            yield from _text_nodes(child)


def _odt_edit(data: bytes, changes: list[tuple[str, str]], notes: list[tuple[str, str]]) -> Result:
    doc = load_odf(io.BytesIO(data))
    result = Result(b"")
    paragraphs = list(_odf_paragraphs(doc.text))
    for find, replace in changes:
        n = 0
        for node in (t for p in paragraphs for t in _text_nodes(p)):
            if find in node.data:
                n += node.data.count(find)
                node.data = node.data.replace(find, replace)
        if n:
            result.items.append(Item(find, replace))
        elif any(find in teletype.extractText(p) for p in paragraphs):
            # The text is there but split across differently formatted pieces; changing it loses formatting.
            result.not_found.append(find)
            result.warnings.append(f"{find!r} spans differently formatted text; change it in ONLYOFFICE")
        else:
            result.not_found.append(find)
    if notes:
        result.warnings.append("comments are supported in Word files only; none were added")
        result.not_found.extend(anchor for anchor, _ in notes)
    out = io.BytesIO()
    doc.write(out)
    result.data = out.getvalue()
    return result


# ---- plain text


def _text_edit(data: bytes, changes: list[tuple[str, str]], notes: list[tuple[str, str]]) -> Result:
    text = data.decode("utf-8", errors="replace")
    result = Result(b"")
    for find, replace in changes:
        if find in text:
            text = text.replace(find, replace)
            result.items.append(Item(find, replace))
        else:
            result.not_found.append(find)
    if notes:
        result.warnings.append("comments are supported in Word files only; none were added")
        result.not_found.extend(anchor for anchor, _ in notes)
    result.data = text.encode()
    return result


def edit(data: bytes, mime: str, changes: list[tuple[str, str]], notes: list[tuple[str, str]]) -> Result:
    if mime == DOCX:
        return _docx_edit(data, changes, notes)
    if mime == ODT:
        return _odt_edit(data, changes, notes)
    return _text_edit(data, changes, notes)
