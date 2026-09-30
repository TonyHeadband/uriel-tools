from datetime import UTC, datetime

import pytest

from uriel_tools.gating import ToolPolicy

# Friday 6 March 2026, 10:00 in Toronto (UTC-5).
NOW = datetime(2026, 3, 6, 15, 0, tzinfo=UTC)
NEWS = {
    "title": "AI news",
    "prompt": "A digest of this morning's AI news",
    "frequency": "daily",
    "at": "09:00",
}


def unattended(*tools: str, groups: tuple[str, ...] = ("family",)) -> ToolPolicy:
    """A policy where these tools may run unattended, for these groups."""
    rule = {"groups": list(groups), "category": "test", "unattended": True}
    return ToolPolicy.model_validate({"tools": dict.fromkeys(tools, rule)})


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def clock():
    return Clock(NOW)


async def make(svc, caller, **fields):
    draft = await svc.draft(caller, **(NEWS | fields))
    return await svc.create(caller, draft["draft_id"])
