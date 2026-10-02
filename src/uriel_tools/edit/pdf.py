"""PDF edits with PyMuPDF. Every function takes the original bytes and returns new bytes."""

import io

import pymupdf
from PIL import Image, ImageOps

from uriel_tools.edit.core import Item, NotEditable, Result

INK = (0.05, 0.15, 0.55)  # dark blue, so values Uriel placed stand out from the printed form on review
FONT = "helv"  # built in; covers Latin-1 (Italian, French and English accents)
SIGNATURE_WORDS = ("Signature", "Firma", "Signé", "Signed")
_FREE_RIGHT = 60  # points to the right of a label that must be empty for it to look like a blank to fill


def _open(data: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as e:
        raise NotEditable("the PDF can't be read") from e
    if doc.needs_pass:
        raise NotEditable("the PDF is password-protected")
    return doc


def _is_xfa(doc: pymupdf.Document) -> bool:
    kind, _ = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")
    return kind != "null"


def _widgets(doc: pymupdf.Document):
    for page in doc:
        for w in page.widgets() or []:
            yield page, w


def _is_signature(w: pymupdf.Widget) -> bool:
    """A signature field, or a text box or button named for one: many forms mark the spot that way."""
    if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
        return True
    name = w.field_name.casefold()
    return (
        w.field_type in (pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.PDF_WIDGET_TYPE_BUTTON)
        and any(word.casefold() in name for word in SIGNATURE_WORDS)
        and not any(d in name for d in ("date", "data"))  # "Date of signature", "Data della firma"
    )


def _option_label(page: pymupdf.Page, w: pymupdf.Widget) -> str:
    """The printed words right of a radio button on its own line, up to the next widget: its option's label.

    A radio's own value is an opaque on-state such as "0" or "Yes", which means nothing to the person.
    """
    r = w.rect
    mid = (r.y0 + r.y1) / 2
    others = [o.rect.x0 for o in page.widgets() if o.rect.x0 > r.x1 and o.rect.y0 < mid < o.rect.y1]
    stop = min([*others, r.x1 + 150])
    words = sorted(page.get_text("words"), key=lambda x: x[0])
    return " ".join(x[4] for x in words if x[0] >= r.x1 - 1 and x[2] <= stop + 1 and x[1] < mid < x[3])


def _radio_groups(doc: pymupdf.Document) -> dict[str, list[tuple[pymupdf.Page, pymupdf.Widget, str]]]:
    groups: dict[str, list] = {}
    for page, w in _widgets(doc):
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
            options = groups.setdefault(w.field_name, [])
            options.append((page, w, _option_label(page, w) or f"option {len(options) + 1}"))
    return groups


def _same(a: str, b: str) -> bool:
    return a.replace("\u2212", "-").casefold().strip() == b.replace("\u2212", "-").casefold().strip()


def inspect(data: bytes) -> dict:
    doc = _open(data)
    if _is_xfa(doc):
        return {"type": "pdf", "pages": doc.page_count, "form": "xfa"}
    fields, signatures, radios = [], [], _radio_groups(doc)
    for page, w in _widgets(doc):
        entry = {"name": w.field_name, "type": w.field_type_string, "page": page.number + 1}
        if _is_signature(w):
            signatures.append(entry)
            continue
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_BUTTON:  # print/reset buttons: nothing to fill
            continue
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
            options = radios.pop(w.field_name, None)
            if options is None:  # the group was listed at its first button
                continue
            entry["choices"] = [label for _, _, label in options]
            entry["value"] = next((label for _, o, label in options if o.field_value not in ("Off", "")), "")
            fields.append(entry)
            continue
        entry["value"] = w.field_value
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_TEXT and w.text_maxlen:  # often a row of boxes: 20260928
            entry["max_length"] = w.text_maxlen
        if w.choice_values:
            entry["choices"] = [c if isinstance(c, str) else c[1] for c in w.choice_values]
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
            entry["choices"] = ["on", "off"]
        fields.append(entry)
    return {
        "type": "pdf",
        "pages": doc.page_count,
        "form": "fields" if fields else "flat",
        "fields": fields,
        "signature_fields": signatures,
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"on", "yes", "si", "sì", "oui", "true", "x", "1", "checked"}


def _free_to_the_right(page: pymupdf.Page, hit: pymupdf.Rect, words: list) -> bool:
    band = pymupdf.Rect(hit.x1 + 1, hit.y0, hit.x1 + _FREE_RIGHT, hit.y1)
    return not any(pymupdf.Rect(w[:4]).intersects(band) for w in words)


def _find_label(doc: pymupdf.Document, label: str) -> tuple[pymupdf.Page, pymupdf.Rect] | None:
    """The first occurrence of `label` that looks like a blank to fill (nothing written right after it).

    Forms repeat their labels in explanatory notes; those are followed by more text, the blanks are not.
    """
    first = None
    for page in doc:
        words = page.get_text("words")
        for hit in page.search_for(label):
            if _free_to_the_right(page, hit, words):
                return page, hit
            first = first or (page, hit)
    return first


def _place_text(page: pymupdf.Page, anchor: pymupdf.Rect, value: str) -> pymupdf.Rect:
    size = max(6.0, min(11.0, anchor.height * 0.85))
    width = pymupdf.get_text_length(value, fontname=FONT, fontsize=size)
    x, y = anchor.x1 + 4, anchor.y1 - anchor.height * 0.2
    if x + width > page.rect.x1 - 10:  # no room on the line: write under the label instead
        x, y = anchor.x0, anchor.y1 + size + 2
    page.insert_text((x, y), value, fontsize=size, fontname=FONT, color=INK)
    return pymupdf.Rect(x, y - size, x + width, y + 2)


def _rect(r: pymupdf.Rect) -> tuple[float, float, float, float]:
    return (round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1))


def fill(data: bytes, values: dict[str, str]) -> Result:
    """Fill form fields by name; any key that isn't a field is treated as a printed label and the value is
    written beside it (approximate: to be reviewed)."""
    doc = _open(data)
    if _is_xfa(doc):
        raise NotEditable(
            "this is a dynamic (XFA) form, which can't be filled reliably; open it in ONLYOFFICE"
        )
    result = Result(b"")
    remaining = dict(values)
    for name, options in _radio_groups(doc).items():
        if name in remaining:
            _choose(options, name, str(remaining.pop(name)), result)
    skip = (
        pymupdf.PDF_WIDGET_TYPE_SIGNATURE,
        pymupdf.PDF_WIDGET_TYPE_BUTTON,
        pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON,
    )
    for page, w in _widgets(doc):
        if w.field_name not in remaining or w.field_type in skip:
            continue
        if _is_signature(w):
            remaining.pop(w.field_name)
            result.warnings.append(f"{w.field_name} is a signature box: use preview_signature, not text")
            continue
        value = str(remaining.pop(w.field_name))
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
            w.field_value = w.on_state() if _truthy(value) else "Off"
        elif w.choice_values and value not in [c if isinstance(c, str) else c[1] for c in w.choice_values]:
            result.warnings.append(f"{w.field_name}: {value!r} is not one of its choices; left unchanged")
            continue
        else:
            w.field_value = value
        w.update()
        result.items.append(Item(w.field_name, value, page.number + 1, _rect(w.rect)))
    for label, value in remaining.items():
        found = _find_label(doc, label)
        if found is None:
            result.not_found.append(label)
            continue
        page, hit = found
        placed = _place_text(page, hit, str(value))
        result.items.append(Item(label, str(value), page.number + 1, _rect(placed), approximate=True))
    result.data = doc.tobytes(garbage=3, deflate=True)
    return result


def _choose(options: list, name: str, value: str, result: Result) -> None:
    """Select the radio option whose printed label (or 1-based number) is `value`; the others go off."""
    labels = [label for _, _, label in options]
    pick = next((i for i, label in enumerate(labels) if _same(label, value)), None)
    if pick is None and value.strip().isdigit() and 1 <= int(value) <= len(options):
        pick = int(value) - 1
    if pick is None:
        result.warnings.append(f"{name}: {value!r} is not one of its choices {labels}; left unchanged")
        return
    for i, (_, w, _) in enumerate(options):
        w.field_value = w.on_state() if i == pick else "Off"
        w.update()
    page, w, label = options[pick]
    result.items.append(Item(name, label, page.number + 1, _rect(w.rect)))


def add_notes(data: bytes, notes: list[tuple[str, str]]) -> Result:
    doc = _open(data)
    result = Result(b"")
    for anchor, text in notes:
        found = next(((p, h) for p in doc for h in p.search_for(anchor)[:1]), None)
        if found is None:
            result.not_found.append(anchor)
            continue
        page, hit = found
        page.add_text_annot(pymupdf.Point(hit.x1, hit.y0), text, icon="Comment").set_info(title="Uriel")
        result.items.append(Item(anchor, text, page.number + 1, _rect(hit)))
    result.data = doc.tobytes(garbage=3, deflate=True)
    return result


def replace_text(data: bytes, changes: list[tuple[str, str]]) -> Result:
    """Best effort: the old text is redacted and the new one written in its place with a standard font."""
    doc = _open(data)
    result = Result(b"")
    placements: dict[int, list[tuple[pymupdf.Rect, str, str]]] = {}
    for find, replace in changes:
        hits = [(p, h) for p in doc for h in p.search_for(find)]
        if not hits:
            result.not_found.append(find)
            continue
        for page, hit in hits:
            page.add_redact_annot(hit, fill=(1, 1, 1))
            placements.setdefault(page.number, []).append((hit, find, replace))
    for number, items in placements.items():
        page = doc[number]
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for hit, find, replace in items:
            size = max(6.0, hit.height * 0.8)
            page.insert_text(
                (hit.x0, hit.y1 - hit.height * 0.2), replace, fontsize=size, fontname=FONT, color=(0, 0, 0)
            )
            result.items.append(Item(find, replace, number + 1, _rect(hit), approximate=True))
    if placements:
        result.warnings.append(
            "replaced PDF text is written in a standard font and may not match the original"
        )
    result.data = doc.tobytes(garbage=3, deflate=True)
    return result


def signature_spot(data: bytes, where: str | None) -> tuple[int, pymupdf.Rect, str | None]:
    """(page index, anchor rect, signature box name or None when placed beside printed text).

    A signature box wins: the one named by `where`, or the only one. Forms signed by several people have
    several boxes, and guessing could put the signature in someone else's, so that is refused with the
    list. Without boxes: the last occurrence of `where` or of a usual signature word, since signatures go
    at the end.
    """
    doc = _open(data)
    boxes = [(page, w) for page, w in _widgets(doc) if _is_signature(w)]
    if where:
        boxes = [(page, w) for page, w in boxes if where.casefold() in w.field_name.casefold()]
    if len(boxes) == 1:
        page, w = boxes[0]
        return page.number, pymupdf.Rect(w.rect), w.field_name
    if len(boxes) > 1:
        names = ", ".join(f"{w.field_name!r} (page {page.number + 1})" for page, w in boxes)
        raise NotEditable(
            f"this document has several signature boxes: {names}. Ask the person which one is theirs and "
            "pass its name as where"
        )
    for word in [where] if where else SIGNATURE_WORDS:
        for page in reversed(doc):
            hits = page.search_for(word)
            if hits:
                return page.number, hits[-1], None
    near = f" near {where!r}" if where else ""
    raise NotEditable(f"no place to sign found{near}; say which text the signature should go next to")


def _trim(image: bytes) -> bytes:
    """Crop the signature to its ink: scans and exports carry wide empty margins that would shrink it."""
    img = Image.open(io.BytesIO(image))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        box = img.getchannel("A").getbbox()
    else:
        box = ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 40 else 0).getbbox()
    if not box:
        return image
    pad = 4
    box = (
        max(box[0] - pad, 0),
        max(box[1] - pad, 0),
        min(box[2] + pad, img.width),
        min(box[3] + pad, img.height),
    )
    out = io.BytesIO()
    img.crop(box).save(out, "PNG")
    return out.getvalue()


def _signature_rect(
    page: pymupdf.Page, anchor: pymupdf.Rect, field: str | None, image: bytes
) -> pymupdf.Rect:
    pix = pymupdf.Pixmap(image)
    ratio = pix.width / max(pix.height, 1)
    if field:  # boxes are often a single text line high: let the signature rise above it, as ink would
        return pymupdf.Rect(anchor.x0, min(anchor.y0, anchor.y1 - 24.0), anchor.x1, anchor.y1)
    height = max(anchor.height * 2.2, 24.0)
    width = height * ratio
    x0 = anchor.x1 + 6
    if x0 + width > page.rect.x1 - 10:  # no room on the line: under the anchor
        return pymupdf.Rect(anchor.x0, anchor.y1 + 2, anchor.x0 + width, anchor.y1 + 2 + height)
    return pymupdf.Rect(x0, anchor.y1 - height + 4, x0 + width, anchor.y1 + 4)


def describe_signature(data: bytes, where: str | None) -> str:
    number, anchor, field = signature_spot(data, where)
    if field:
        return f"your signature in the signature box {field!r} on page {number + 1}"
    doc = _open(data)
    near = doc[number].get_textbox(anchor + (-2, -2, 2, 2)).strip() or where or "the signature line"
    return f"your signature beside {near!r} on page {number + 1}"


def sign(data: bytes, image: bytes, where: str | None) -> Result:
    number, anchor, field = signature_spot(data, where)
    image = _trim(image)
    doc = _open(data)
    page = doc[number]
    rect = _signature_rect(page, anchor, field, image)
    # A text or button placeholder ("Signature mandatory" in red) is drawn above page content and would hide
    # the image, so it goes; a real signature field is left for whoever signs digitally later.
    for w in page.widgets():
        if w.field_name == field and w.field_type != pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
            page.delete_widget(w)
            break
    page.insert_image(rect, stream=image, keep_proportion=True, overlay=True)
    item = Item(field or "signature", "signature image", number + 1, _rect(rect), approximate=not field)
    return Result(doc.tobytes(garbage=3, deflate=True), [item])
