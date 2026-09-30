"""USER.md and SOUL.md as markdown, rendered from the notes on demand."""

from uriel_tools.memory.store import Row

TITLES = {"user": "About {name}", "soul": "How to talk to {name}"}


def section_name(raw: str) -> str:
    """ " music " and "Music" are one section: spaces collapse and the first letter is upper-cased."""
    s = " ".join(raw.split())
    return s[:1].upper() + s[1:]


def render(rows: list[Row], file: str, name: str, order: list[str]) -> str:
    # lower-cased section -> (heading, notes), in first-created order
    notes: dict[str, tuple[str, list[str]]] = {}
    for r in rows:
        if r.note is not None:
            notes.setdefault(r.section.lower(), (r.section, []))[1].append(r.note)
    if not notes:
        return ""
    rank = {s.lower(): i for i, s in enumerate(order)}
    lines = [f"# {TITLES[file].format(name=name)}"]
    for key in sorted(notes, key=lambda k: rank.get(k, len(rank))):  # stable: the rest keep their order
        heading, items = notes[key]
        lines += [f"## {heading}", *(f"- {n}" for n in items)]
    return "\n".join(lines)
