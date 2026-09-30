import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from uriel_tools.gating import Caller


@dataclass
class Stored[T]:
    owner: str
    draft: T
    created: float
    result: dict | None = field(default=None)  # what acting on the draft returned, so acting again returns it


class Drafts[T]:
    """Drafts waiting for the person's yes, kept in the server's memory.

    A confirming tool (file_issue, create_schedule) acts only on a stored draft, so the model can't skip
    showing it or act on something the person never saw. A restart loses pending drafts, and the model is
    told to draft again. Each kind of draft has its own store, so one kind's id never confirms another.
    """

    def __init__(self, ttl_s: float = 24 * 3600, max_drafts: int = 500, clock=time.monotonic):
        self._items: OrderedDict[str, Stored[T]] = OrderedDict()
        self._ttl, self._max, self._clock = ttl_s, max_drafts, clock

    @staticmethod
    def _owner(caller: Caller) -> str:
        return caller.sub or caller.user or ""

    def add(self, caller: Caller, draft: T) -> str:
        while len(self._items) >= self._max:
            self._items.popitem(last=False)
        draft_id = secrets.token_urlsafe(9)
        self._items[draft_id] = Stored(self._owner(caller), draft, self._clock())
        return draft_id

    def get(self, caller: Caller, draft_id: str) -> Stored[T] | None:
        stored = self._items.get(draft_id)
        if stored is None or stored.owner != self._owner(caller):
            return None
        if self._clock() - stored.created > self._ttl:
            del self._items[draft_id]
            return None
        return stored
