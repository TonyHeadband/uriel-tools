import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import docx
import openpyxl
from odf import teletype
from odf.namespaces import TEXTNS
from odf.opendocument import load as load_odf
from odf.table import Table, TableCell, TableRow
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

MAX_PAGES = 300
MAX_ROWS = 5000  # per sheet
OCR_MIN_CHARS = 20  # a PDF page with less text than this is treated as a scan
# When a font has no usable character map, pypdf emits glyph ids ("/0/1/2/i255/5") instead of words. On the
# first real-drive index (2026-09-28) such pages scored ~1.0 and every readable page 0.0.
_GLYPH_RUN = re.compile(r"(?:/[A-Za-z]{0,4}\d{1,5}){3,}")
GLYPH_NOISE_MAX = 0.5

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ODT = "application/vnd.oasis.opendocument.text"
ODS = "application/vnd.oasis.opendocument.spreadsheet"
IMAGES = frozenset({"image/png", "image/jpeg", "image/webp", "image/tiff"})
ALLOWED = frozenset({PDF, DOCX, XLSX, ODT, ODS, "text/plain", "text/markdown"}) | IMAGES
_BY_EXTENSION = {
    ".pdf": PDF,
    ".docx": DOCX,
    ".xlsx": XLSX,
    ".odt": ODT,
    ".ods": ODS,
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class Skipped(Exception):
    """The file is readable but not indexable; the message is stored as the reason."""


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    needs_ocr: bool = False
    source: str = "text"


def resolve_mime(mime: str, filename: str) -> str:
    base = mime.split(";")[0].strip().lower()
    if base in ALLOWED:
        return base
    return _BY_EXTENSION.get(PurePosixPath(filename).suffix.lower(), base)


def extract(data: bytes, mime: str) -> list[Page]:
    if mime == PDF:
        return _pdf(data)
    if mime == DOCX:
        return [Page(1, _docx(data))]
    if mime == ODT:
        doc = load_odf(io.BytesIO(data))
        return [Page(1, "\n\n".join(b for b in _odf_blocks(doc.text) if b.strip()))]
    if mime == XLSX:
        return _xlsx(data)
    if mime == ODS:
        return _ods(data)
    if mime in ("text/plain", "text/markdown"):
        return [Page(1, data.decode("utf-8", errors="replace"))]
    if mime in IMAGES:
        return _image_pages(data)
    raise Skipped(f"unsupported type {mime or 'unknown'}")


def _pdf(data: bytes) -> list[Page]:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise Skipped("encrypted PDF")
    if len(reader.pages) > MAX_PAGES:
        raise Skipped(f"more than {MAX_PAGES} pages")
    pages = []
    for number, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        pages.append(Page(number, text, needs_ocr=len(text) < OCR_MIN_CHARS or _glyph_noise(text)))
    return pages


def _glyph_noise(text: str) -> bool:
    """True when most of the text is glyph ids, so the page must be read like a scan."""
    visible = len("".join(text.split()))
    return sum(len(m) for m in _GLYPH_RUN.findall(text)) > GLYPH_NOISE_MAX * visible


def _docx(data: bytes) -> str:
    document = docx.Document(io.BytesIO(data))
    blocks = [p.text for p in document.paragraphs]
    for table in document.tables:
        blocks += ["\t".join(cell.text for cell in row.cells) for row in table.rows]
    return "\n\n".join(b for b in blocks if b.strip())


def _odf_blocks(node):
    for child in getattr(node, "childNodes", []):
        if getattr(child, "qname", None) in ((TEXTNS, "p"), (TEXTNS, "h")):
            yield teletype.extractText(child)
        else:
            yield from _odf_blocks(child)


def _xlsx(data: bytes) -> list[Page]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        pages = []
        for number, ws in enumerate(wb.worksheets, 1):
            rows = []
            for row in ws.iter_rows(values_only=True, max_row=MAX_ROWS):
                cells = ["" if v is None else str(v) for v in row]
                if any(cells):
                    rows.append("\t".join(cells).rstrip())
            # Blank lines between rows let the chunker pack whole rows.
            pages.append(Page(number, "\n\n".join([ws.title, *rows])))
        return pages
    finally:
        wb.close()


def _ods(data: bytes) -> list[Page]:
    doc = load_odf(io.BytesIO(data))
    pages = []
    for number, table in enumerate(doc.spreadsheet.getElementsByType(Table), 1):
        rows = []
        for row in table.getElementsByType(TableRow)[:MAX_ROWS]:
            cells = [teletype.extractText(c) for c in row.getElementsByType(TableCell)]
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells).rstrip())
        pages.append(Page(number, "\n\n".join([str(table.getAttribute("name")), *rows])))
    return pages


def _image_pages(data: bytes) -> list[Page]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            frames = getattr(img, "n_frames", 1)
    except (UnidentifiedImageError, Image.DecompressionBombError) as e:
        raise Skipped(f"unreadable image: {type(e).__name__}") from e
    if frames > MAX_PAGES:
        raise Skipped(f"more than {MAX_PAGES} pages")
    return [Page(n, "", needs_ocr=True) for n in range(1, frames + 1)]
