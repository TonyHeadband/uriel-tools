from uriel_tools.rag.chunk import MAX_CHARS, chunk_pages
from uriel_tools.rag.extract import Page


def test_short_page_is_one_chunk():
    [c] = chunk_pages([Page(3, "Uno.\n\nDue.")])
    assert (c.page, c.source, c.text) == (3, "text", "Uno.\n\nDue.")


def test_long_text_splits_with_overlap():
    paras = [f"Paragrafo {i} " + "parola " * 60 for i in range(40)]
    chunks = chunk_pages([Page(1, "\n\n".join(paras))])
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHARS for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.text[:50] in prev.text  # the next chunk starts with the tail of the previous one


def test_huge_paragraph_is_hard_split():
    chunks = chunk_pages([Page(1, "x" * 7000)])
    assert [len(c.text) for c in chunks][:2] == [MAX_CHARS, MAX_CHARS]
    assert all(len(c.text) <= MAX_CHARS for c in chunks)


def test_pages_keep_numbers_and_source_and_ocr_pending_pages_are_skipped():
    chunks = chunk_pages(
        [Page(1, "testo\x00 uno"), Page(2, "", needs_ocr=True), Page(3, "letto", source="ocr")]
    )
    assert [(c.page, c.source, c.text) for c in chunks] == [(1, "text", "testo uno"), (3, "ocr", "letto")]


def test_empty_pages_make_no_chunks():
    assert chunk_pages([Page(1, "  \n\n  ")]) == []
