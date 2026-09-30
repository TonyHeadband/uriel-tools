import io

import docx
import pymupdf
import pytest
from odf.opendocument import OpenDocumentText
from odf.style import Style, TextProperties
from odf.text import P, Span

from tests import filegen
from uriel_tools.edit import office, pdf
from uriel_tools.edit.core import NotEditable
from uriel_tools.edit.names import draft_path, is_draft, original_name
from uriel_tools.rag.extract import DOCX, ODT, extract


def widgets(data: bytes) -> dict:
    doc = pymupdf.open(stream=data, filetype="pdf")
    return {w.field_name: w.field_value for p in doc for w in p.widgets()}


def page_text(data: bytes, page: int) -> str:
    return pymupdf.open(stream=data, filetype="pdf")[page].get_text()


def test_inspect_reports_fields_choices_and_signature_fields():
    info = pdf.inspect(filegen.form_pdf())
    assert info["form"] == "fields"
    assert {f["name"]: f["type"] for f in info["fields"]} == {
        "cognome": "Text",
        "privacy": "CheckBox",
        "classe": "ComboBox",
        "sesso": "RadioButton",
    }
    [sesso] = [f for f in info["fields"] if f["name"] == "sesso"]
    assert sesso["choices"] == ["Maschio", "Femmina"] and sesso["value"] == ""
    assert [s["name"] for s in info["signature_fields"]] == ["firma"]
    assert pdf.inspect(filegen.flat_form_pdf())["form"] == "flat"


def test_fill_real_fields_exactly():
    result = pdf.fill(filegen.form_pdf(), {"cognome": "Rossi", "privacy": "sì", "classe": "1B"})
    got = widgets(result.data)
    assert got["cognome"] == "Rossi" and got["classe"] == "1B" and got["privacy"] not in ("Off", "")
    assert not result.approximate and result.not_found == []


def test_radio_options_are_chosen_by_their_printed_label():
    result = pdf.fill(filegen.form_pdf(), {"sesso": "femmina"})
    assert [(i.item, i.value) for i in result.items] == [("sesso", "Femmina")]
    assert [f["value"] for f in pdf.inspect(result.data)["fields"] if f["name"] == "sesso"] == ["Femmina"]
    assert "not one of its choices" in pdf.fill(filegen.form_pdf(), {"sesso": "Altro"}).warnings[0]


def test_a_value_outside_a_fields_choices_is_left_and_warned():
    result = pdf.fill(filegen.form_pdf(), {"classe": "5Z"})
    assert widgets(result.data)["classe"] == "1A"
    assert "5Z" in result.warnings[0]


def test_flat_form_writes_beside_the_blank_label_not_the_mention_in_the_notes():
    result = pdf.fill(filegen.flat_form_pdf(), {"Family name": "Rossi", "Nickname": "Mimmo"})
    [item] = result.items
    assert (item.item, item.page, item.approximate) == ("Family name", 2, True)
    assert "Rossi" in page_text(result.data, 1) and "Rossi" not in page_text(result.data, 0)
    assert item.rect[0] > 72  # to the right of the label, on its line
    assert result.not_found == ["Nickname"]


def test_xfa_forms_are_refused():
    doc = pymupdf.open(stream=filegen.form_pdf(), filetype="pdf")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/XFA", "[]")
    with pytest.raises(NotEditable, match="XFA"):
        pdf.fill(doc.tobytes(), {"cognome": "Rossi"})
    assert pdf.inspect(doc.tobytes())["form"] == "xfa"


def test_notes_and_best_effort_text_replacement():
    result = pdf.add_notes(filegen.flat_form_pdf(), [("Date of birth", "use DD/MM/YYYY"), ("Missing", "x")])
    annots = [a.info["content"] for a in pymupdf.open(stream=result.data, filetype="pdf")[1].annots()]
    assert annots == ["use DD/MM/YYYY"] and result.not_found == ["Missing"]
    replaced = pdf.replace_text(filegen.flat_form_pdf(), [("Given names", "First names")])
    assert "First names" in page_text(replaced.data, 1) and "Given names" not in page_text(replaced.data, 1)
    assert replaced.approximate and replaced.warnings


def test_sign_uses_the_signature_field_first_then_the_last_signature_word():
    image = filegen.signature_png()
    in_field = pdf.sign(filegen.form_pdf(), image, None)
    assert not in_field.approximate and in_field.items[0].page == 1
    assert "signature box 'firma'" in pdf.describe_signature(filegen.form_pdf(), None)
    by_word = pdf.sign(filegen.flat_form_pdf(), image, None)
    assert by_word.approximate and by_word.items[0].page == 2
    assert len(pymupdf.open(stream=by_word.data, filetype="pdf")[1].get_images()) == 1
    assert "beside 'Signature' on page 2" in pdf.describe_signature(filegen.flat_form_pdf(), None)
    with pytest.raises(NotEditable, match="no place to sign"):
        pdf.signature_spot(filegen.flat_form_pdf(), "Firma del genitore")


def two_signer_form() -> bytes:
    """One signature box per signer, as a text field or a bare button, as government forms often have."""
    doc = pymupdf.open()
    page = doc.new_page()
    text, button = pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.PDF_WIDGET_TYPE_BUTTON
    fields = [("1-11 Signature", text), ("1-12 Date of signature", text), ("2-23 Signature", button)]
    for i, (name, kind) in enumerate(fields):
        w = pymupdf.Widget()
        w.field_name, w.field_type = name, kind
        if kind == button:
            w.button_caption = " "
        w.rect = pymupdf.Rect(100, 100 + i * 40, 300, 120 + i * 40)
        page.add_widget(w)
    return doc.tobytes()


def test_text_boxes_named_for_a_signature_are_signed_not_typed_into():
    data = two_signer_form()
    info = pdf.inspect(data)
    assert [s["name"] for s in info["signature_fields"]] == ["1-11 Signature", "2-23 Signature"]
    assert [f["name"] for f in info["fields"]] == ["1-12 Date of signature"]
    typed = pdf.fill(data, {"1-11 Signature": "Mario Rossi"})
    assert typed.items == [] and "use sign_document" in typed.warnings[0]
    with pytest.raises(NotEditable, match="several signature boxes.*which one is theirs"):
        pdf.signature_spot(data, None)
    signed = pdf.sign(data, filegen.signature_png(), "2-23")
    assert (signed.items[0].item, signed.approximate) == ("2-23 Signature", False)
    # the placeholder is removed so it can't hide the image; the other signer's box stays
    assert [s["name"] for s in pdf.inspect(signed.data)["signature_fields"]] == ["1-11 Signature"]


def test_signature_margins_are_trimmed():
    trimmed = pymupdf.Pixmap(pdf._trim(filegen.signature_png()))
    assert trimmed.height < 80 and trimmed.width <= 300  # the drawn line and a small pad, of a 300x100 image


def test_docx_replacement_across_runs_keeps_the_first_runs_formatting():
    result = office.edit(
        filegen.styled_docx(), DOCX, [("Mario Rossi", "Luigi Bianchi"), ("Roma", "Torino")], []
    )
    doc = docx.Document(io.BytesIO(result.data))
    para = doc.paragraphs[0]
    assert para.text == "Il sottoscritto Luigi Bianchi dichiara di essere residente a Milano."
    assert [r.text for r in para.runs if r.bold] == ["Luigi Bianchi"]
    assert result.not_found == ["Roma"]


def test_docx_comment_is_anchored_on_the_text():
    result = office.edit(filegen.styled_docx(), DOCX, [], [("Milano", "was it Milano in 2024 too?")])
    doc = docx.Document(io.BytesIO(result.data))
    assert [c.text for c in doc.comments] == ["was it Milano in 2024 too?"]


def test_odt_replaces_within_a_piece_and_says_why_it_wont_across_pieces():
    data = filegen.odt_file(["Residenza: Milano, via Roma 1"])
    result = office.edit(data, ODT, [("Milano", "Torino")], [("x", "note")])
    assert [i.item for i in result.items] == ["Milano"]
    assert extract(result.data, ODT)[0].text == "Residenza: Torino, via Roma 1"
    assert "Word files only" in result.warnings[0]
    # "Milano, via" split across a bold and a plain piece can't be changed without losing formatting
    doc = OpenDocumentText()
    bold = Style(name="B", family="text")
    bold.addElement(TextProperties(fontweight="bold"))
    doc.automaticstyles.addElement(bold)
    para = P(text="Residenza: ")
    para.addElement(Span(stylename=bold, text="Milano"))
    para.addText(", via Roma 1")
    doc.text.addElement(para)
    buf = io.BytesIO()
    doc.write(buf)
    split = office.edit(buf.getvalue(), ODT, [("Milano, via", "Torino, via")], [])
    assert split.not_found == ["Milano, via"] and "ONLYOFFICE" in split.warnings[0]


def test_plain_text_edit():
    result = office.edit(b"a = 1\nb = 2\n", "text/plain", [("b = 2", "b = 3")], [])
    assert result.data == b"a = 1\nb = 3\n"


def test_draft_names():
    assert draft_path("/D/Casa/polizza.pdf", set()) == "/D/Casa/edit_polizza.pdf"
    assert draft_path("/D/polizza.pdf", {"edit_polizza.pdf"}) == "/D/edit_polizza (2).pdf"
    # a draft of a draft is named after the original
    assert draft_path("/D/edit_polizza.pdf", {"edit_polizza.pdf"}) == "/D/edit_polizza (2).pdf"
    assert original_name("edit_polizza (3).pdf") == "polizza.pdf"
    assert is_draft("/D/edit_x.pdf") and not is_draft("/D/credit_edit_x.pdf")
