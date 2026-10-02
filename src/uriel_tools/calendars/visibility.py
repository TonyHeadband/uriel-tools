"""Which of the calendars shared with uriel a person may use.

uriel can read every calendar shared with it, so this is the only thing keeping one person's calendar from
another: a calendar is theirs when they own it (being in uriel's home means it was shared with uriel), and a
group calendar when uriel owns it and shares it with Nextcloud group g, which the caller is in (Nextcloud
groups mirror Authelia groups). uriel's calendars shared only with users are the schedules mirror.
"""

from dataclasses import dataclass

from uriel_tools.gating import Caller
from uriel_tools.schedules.caldav import CalendarInfo

SHARING_STEPS = 'Calendar app → ⋯ next to the calendar → Share → type "uriel" → tick "can edit"'
KINDS = {"VTODO": "tasks", "VEVENT": "events"}


class Refused(ValueError):
    """A request the model can fix by asking the person or calling another tool; the message says how."""


@dataclass(frozen=True)
class Visible:
    info: CalendarInfo
    mine: bool  # False: a group calendar, which others read too


def visible_to(infos: list[CalendarInfo], caller: Caller, *, uriel: str = "uriel") -> list[Visible]:
    if not caller.user:
        raise Refused("calendars need a signed-in person")
    found = []
    for info in infos:
        if info.owner == uriel:
            groups = {s.removeprefix("groups/") for s in info.sharees if s.startswith("groups/")}
            if groups & caller.groups:
                found.append(Visible(info, False))
        elif info.owner == caller.user:
            found.append(Visible(info, True))
    return found


def _names(cals: list[Visible]) -> str:
    return ", ".join(c.info.name for c in cals)


def resolve(cals: list[Visible], calendar: str, component: str, default_href: str | None) -> Visible:
    """The calendar a write goes to. `calendar` is "mine" or a display name."""
    if calendar.strip().lower() != "mine":
        named = [c for c in cals if c.info.name.lower() == calendar.strip().lower()]
        if not named:
            raise Refused(f"no calendar called {calendar!r}; the ones you can use: {_names(cals) or 'none'}")
        if len(named) > 1:  # a person's own calendar can share a name with a group one
            named = [c for c in named if c.mine] or named
        if len(named) > 1:
            raise Refused(
                f"{len(named)} calendars are called {named[0].info.name}: "
                "rename one in the Calendar app so they can be told apart"
            )
        chosen = named[0]
        if component not in chosen.info.components:
            others = [c for c in cals if component in c.info.components]
            msg = (
                f"{chosen.info.name} doesn't hold {KINDS[component]}: "
                f"pick another ({_names(others) or 'none'})"
            )
            raise Refused(msg)
        return chosen
    mine = [c for c in cals if c.mine]
    if not mine:
        raise Refused(f"you haven't shared a calendar with Uriel yet. To share one: {SHARING_STEPS}")
    holding = [c for c in mine if component in c.info.components]
    if not holding:
        raise Refused(f"none of your calendars holds {KINDS[component]}: {_names(mine)}")
    saved = next((c for c in holding if c.info.href == default_href), None)
    if saved is not None:
        return saved
    if len(holding) == 1:
        return holding[0]
    raise Refused(
        f"which calendar: {_names(holding)}? Ask the person, then save their answer with set_default_calendar"
    )
