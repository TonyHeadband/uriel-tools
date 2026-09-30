import io

import pytest
from pypdf import PdfWriter
from pypdf.errors import PdfReadError

from tests import filegen
from uriel_tools.rag.extract import DOCX, ODS, ODT, PDF, XLSX, Skipped, extract, resolve_mime


def test_text_pdf_pages():
    pages = extract(
        filegen.text_pdf(["Manuale della caldaia", "Premere reset per 5 secondi"]), "application/pdf"
    )
    assert [p.number for p in pages] == [1, 2]
    assert "caldaia" in pages[0].text and not any(p.needs_ocr for p in pages)


def test_scanned_pdf_needs_ocr():
    pages = extract(filegen.scanned_pdf(["Ricevuta 4471", "Pagina due"]), "application/pdf")
    assert [(p.number, p.needs_ocr) for p in pages] == [(1, True), (2, True)]


def test_mixed_pdf_flags_only_the_scanned_page():
    writer = PdfWriter()
    writer.append(io.BytesIO(filegen.text_pdf(["Contratto di assicurazione casa, polizza AB-1234"])))
    writer.append(io.BytesIO(filegen.scanned_pdf(["firma"])))
    buf = io.BytesIO()
    writer.write(buf)
    pages = extract(buf.getvalue(), "application/pdf")
    assert [p.needs_ocr for p in pages] == [False, True]


def test_pdf_page_of_glyph_ids_needs_ocr():
    garbled = "/0/1/2/3/4/2/5/3/5/4/6/i255/0/5/8/0/9/10 /11/12/13/i255/14/15/16/17/i255/18/19/20/15/21/12"
    pages = extract(filegen.text_pdf([garbled, "Rinnovo 12/05/2021, rif. 2021/08/06, importo 80.00"]), PDF)
    assert [p.needs_ocr for p in pages] == [True, False]


def test_office_formats():
    assert "Riunione" in extract(filegen.docx_file(["Riunione genitori", "martedì"]), DOCX)[0].text
    assert "réunion" in extract(filegen.odt_file(["Compte rendu", "réunion de parents"]), ODT)[0].text
    sheets = extract(filegen.xlsx_file({"Spese": [["mese", "importo"], ["marzo", 120]], "Vuoto": []}), XLSX)
    assert sheets[0].number == 1 and "marzo\t120" in sheets[0].text and sheets[0].text.startswith("Spese")
    ods = extract(filegen.ods_file({"Voti": [["materia", "voto"], ["storia", "8"]]}), ODS)
    assert "storia\t8" in ods[0].text


def test_plain_text_and_markdown():
    assert extract("Città: Genève\n".encode(), "text/plain")[0].text.startswith("Città")
    assert extract(b"# Titolo", "text/markdown")[0].text == "# Titolo"


def test_images_need_ocr():
    [page] = extract(filegen.text_png("Scontrino"), "image/png")
    assert page.needs_ocr and page.text == ""


def test_unsupported_and_oversized_are_skipped():
    with pytest.raises(Skipped, match="unsupported"):
        extract(b"PK..", "application/zip")
    with pytest.raises(Skipped, match="300 pages"):
        extract(filegen.text_pdf(["x"] * 301), "application/pdf")
    with pytest.raises(Skipped, match="unreadable image"):
        extract(b"not an image", "image/png")


def test_corrupt_pdf_raises_for_retry():
    with pytest.raises(PdfReadError):
        extract(b"%PDF-1.4 garbage", "application/pdf")


def test_resolve_mime():
    assert resolve_mime("application/octet-stream", "/D/a.PDF") == "application/pdf"
    assert resolve_mime("text/markdown; charset=utf-8", "/D/a.md") == "text/markdown"
    assert resolve_mime("", "/D/a.odt") == ODT
    assert resolve_mime("application/zip", "/D/a.zip") == "application/zip"
