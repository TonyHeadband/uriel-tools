import re
from collections.abc import Iterable
from dataclasses import dataclass

from uriel_tools.rag.extract import Page

MAX_CHARS = 3200  # ≈ 800 tokens at ~4 characters per token (mixed IT/EN/FR prose)
OVERLAP_CHARS = 400  # ≈ 100 tokens


@dataclass(frozen=True)
class Chunk:
    page: int
    source: str
    text: str


def chunk_pages(
    pages: Iterable[Page], max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS
) -> list[Chunk]:
    chunks = []
    for page in pages:
        if page.needs_ocr:
            continue  # contributes once the OCR job has read it
        for text in _split(page.text.replace("\x00", ""), max_chars, overlap):
            chunks.append(Chunk(page.number, page.source, text))
    return chunks


def _split(text: str, max_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if para:
            pieces += _hard_split(para, max_chars) if len(para) > max_chars else [para]
    out: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > max_chars:
            out.append(current)
            tail = current[-overlap:]
            tail = tail[tail.find(" ") + 1 :] if " " in tail else tail  # start the overlap on a word
            current = f"{tail}\n\n{piece}" if len(tail) + 2 + len(piece) <= max_chars else piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        out.append(current)
    return out


def _hard_split(para: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for word in para.split():
        if len(word) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces += [word[i : i + max_chars] for i in range(0, len(word), max_chars)]
        elif current and len(current) + 1 + len(word) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        pieces.append(current)
    return pieces
