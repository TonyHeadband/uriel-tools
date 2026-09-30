import re
from pathlib import PurePosixPath

PREFIX = "edit_"
_COPY = re.compile(r" \(\d+\)$")


def is_draft(path: str) -> bool:
    return PurePosixPath(path).name.startswith(PREFIX)


def original_name(name: str) -> str:
    """`edit_x (2).pdf` -> `x.pdf`: what a draft was made from, whatever draft it was made from."""
    p = PurePosixPath(name)
    stem = p.stem.removeprefix(PREFIX) if p.stem.startswith(PREFIX) else p.stem
    return _COPY.sub("", stem) + p.suffix


def draft_path(source: str, taken: set[str]) -> str:
    """`edit_<original name>` next to the source; `edit_<stem> (2).<ext>` and so on when taken, as Nextcloud
    names copies.

    A draft of a draft is named after the original, so "now sign it" on edit_x.pdf gives edit_x (2).pdf.
    """
    src = PurePosixPath(source)
    name = PurePosixPath(PREFIX + original_name(src.name))
    candidate, n = name.name, 2
    while candidate in taken:
        candidate, n = f"{name.stem} ({n}){name.suffix}", n + 1
    return str(src.parent / candidate)
