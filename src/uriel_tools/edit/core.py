from dataclasses import dataclass, field


class NotEditable(Exception):
    """The document can't be edited this way; the message says why and what to do instead."""


@dataclass(frozen=True)
class Item:
    """One value placed or change made, recorded in edit_items."""

    item: str  # field name, label, anchor or the text that was replaced
    value: str
    page: int | None = None
    rect: tuple[float, float, float, float] | None = None
    approximate: bool = False


@dataclass
class Result:
    data: bytes
    items: list[Item] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def approximate(self) -> bool:
        return any(i.approximate for i in self.items)
