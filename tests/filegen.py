"""Build small document fixtures in memory, so no binary files live in the repo."""

import io

import openpyxl
from docx import Document
from odf.opendocument import OpenDocumentSpreadsheet, OpenDocumentText
from odf.table import Table, TableCell, TableRow
from odf.text import P
from PIL import Image, ImageDraw, ImageFont


def text_pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page: a real text layer."""
    n = len(pages)
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, text in enumerate(pages):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET"
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objs.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _image(text: str) -> Image.Image:
    img = Image.new("RGB", (1800, 300), "white")  # wide enough for one sentence at 36 px
    ImageDraw.Draw(img).text((30, 120), text, fill="black", font=ImageFont.load_default(size=36))
    return img


def text_png(text: str) -> bytes:
    buf = io.BytesIO()
    _image(text).save(buf, "PNG")
    return buf.getvalue()


def scanned_pdf(pages: list[str]) -> bytes:
    """Pages that are only images, like a scanner's output: no text layer at all."""
    images = [_image(t) for t in pages]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def docx_file(paras: list[str]) -> bytes:
    doc = Document()
    for p in paras:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def odt_file(paras: list[str]) -> bytes:
    doc = OpenDocumentText()
    for p in paras:
        doc.text.addElement(P(text=p))
    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()


def xlsx_file(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def ods_file(sheets: dict[str, list[list[str]]]) -> bytes:
    doc = OpenDocumentSpreadsheet()
    for name, rows in sheets.items():
        table = Table(name=name)
        for row in rows:
            tr = TableRow()
            for value in row:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=value))
                tr.addElement(cell)
            table.addElement(tr)
        doc.spreadsheet.addElement(table)
    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()


def form_pdf() -> bytes:
    """A PDF with real form fields: text, checkbox, choice, a signature field, a radio group with printed
    option labels, and a print button."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Domanda di iscrizione", fontsize=14)
    for i, (name, kind) in enumerate(
        [
            ("cognome", pymupdf.PDF_WIDGET_TYPE_TEXT),
            ("privacy", pymupdf.PDF_WIDGET_TYPE_CHECKBOX),
            ("classe", pymupdf.PDF_WIDGET_TYPE_COMBOBOX),
            ("firma", pymupdf.PDF_WIDGET_TYPE_SIGNATURE),
        ]
    ):
        w = pymupdf.Widget()
        w.field_name, w.field_type = name, kind
        w.rect = pymupdf.Rect(200, 120 + i * 40, 400, 140 + i * 40)
        if kind == pymupdf.PDF_WIDGET_TYPE_COMBOBOX:
            w.choice_values = ["1A", "1B"]
            w.field_value = "1A"
        page.add_widget(w)
    _radio_group(doc, page, "sesso", ["Maschio", "Femmina"], 300)
    w = pymupdf.Widget()
    w.field_name, w.field_type = "Stampa", pymupdf.PDF_WIDGET_TYPE_BUTTON
    w.button_caption = "Stampa"
    w.rect = pymupdf.Rect(200, 340, 260, 360)
    page.add_widget(w)
    return doc.tobytes()


def _radio_group(doc, page, name: str, labels: list[str], y: float) -> None:
    """PyMuPDF can't add radio buttons (1.28 fails validating a widget that has no xref yet), so add
    checkboxes and turn them into one radio field: a parent with the radio flags, each kid its own
    on-state."""
    import pymupdf

    kids = []
    for i, label in enumerate(labels):
        x = 200 + i * 120
        w = pymupdf.Widget()
        w.field_name, w.field_type = f"{name}{i}", pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        w.rect = pymupdf.Rect(x, y, x + 10, y + 10)
        kids.append(page.add_widget(w).xref)
        page.insert_text((x + 14, y + 9), label, fontsize=9)
    catalog = doc.pdf_catalog()
    fields = doc.xref_get_key(catalog, "AcroForm/Fields")[1]
    parent = doc.get_new_xref()
    refs = " ".join(f"{k} 0 R" for k in kids)
    doc.update_object(parent, f"<</FT/Btn/Ff 49152/T({name})/V/Off/Kids[{refs}]>>")
    for i, k in enumerate(kids):
        for key in ("T", "FT", "Ff", "V"):
            doc.xref_set_key(k, key, "null")
        doc.xref_set_key(k, "Parent", f"{parent} 0 R")
        doc.xref_set_key(k, "AP/N", doc.xref_get_key(k, "AP/N")[1].replace("/Yes", f"/{i}"))
        doc.xref_set_key(k, "AS", "/Off")
        fields = fields.replace(f"{k} 0 R", f"{parent} 0 R" if i == 0 else "")
    doc.xref_set_key(catalog, "AcroForm/Fields", fields)


def flat_form_pdf() -> bytes:
    """A printable form without fields: labels followed by blanks, and notes repeating a label."""
    import pymupdf

    doc = pymupdf.open()
    notes = doc.new_page()
    notes.insert_text((72, 90), "Notes: write your Family name exactly as on your passport.", fontsize=10)
    form = doc.new_page()
    for i, label in enumerate(["Family name", "Given names", "Date of birth"]):
        form.insert_text((72, 120 + i * 40), label, fontsize=10)
    form.insert_text((72, 700), "Signature", fontsize=10)
    return doc.tobytes()


def styled_docx() -> bytes:
    """One visible sentence split across differently formatted runs, as Word often does."""
    doc = Document()
    p = doc.add_paragraph("Il sottoscritto ")
    p.add_run("Mario").bold = True
    p.add_run(" Ro")
    p.add_run("ssi dichiara di essere residente a Milano.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def signature_png() -> bytes:
    img = Image.new("RGBA", (300, 100), (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(10, 80), (90, 20), (170, 70), (290, 30)], fill=(0, 0, 0, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
