import pytest

from uriel_tools.calendars.visibility import SHARING_STEPS, Refused, Visible, resolve, visible_to
from uriel_tools.gating import Caller
from uriel_tools.schedules.caldav import CalendarInfo

TONY = Caller("anthony-headband", frozenset({"family"}), "r1")
LIL_B = Caller("lil-b", frozenset({"family"}), "r2")
GUEST = Caller("guest", frozenset(), "r3")
BOTH = frozenset({"VEVENT", "VTODO"})


def cal(slug, name, owner, *sharees, components=BOTH, read_only=False):
    return CalendarInfo(f"/c/uriel/{slug}/", name, owner, read_only, frozenset(sharees), components)


PERSONAL = cal("p", "Personal", "anthony-headband")
WORK = cal("w", "Work", "anthony-headband", components=frozenset({"VEVENT"}))
FAMILY = cal("f", "Family", "uriel", "groups/family")
LILS = cal("l", "Lil-B", "lil-b")
MIRROR = cal("uriel-anthony-headband", "Uriel · Tony", "uriel", "users/anthony-headband")
OTHER_GROUP = cal("o", "Friends", "uriel", "groups/friends")
LOOKALIKE = cal("x", "Lookalike", "uriel", "groups/familyx")


def test_people_see_their_own_shared_calendars_and_their_groups():
    infos = [PERSONAL, WORK, FAMILY, LILS, MIRROR, OTHER_GROUP, LOOKALIKE]
    assert visible_to(infos, TONY) == [Visible(PERSONAL, True), Visible(WORK, True), Visible(FAMILY, False)]
    assert visible_to(infos, LIL_B) == [Visible(FAMILY, False), Visible(LILS, True)]
    assert visible_to(infos, GUEST) == []


def test_a_group_calendar_needs_the_callers_group_exactly():
    assert visible_to([FAMILY], GUEST) == []
    assert visible_to([LOOKALIKE, MIRROR], TONY) == []


def test_nobody_signed_in_is_refused():
    with pytest.raises(Refused, match="signed-in"):
        visible_to([PERSONAL], Caller(None, frozenset({"family"}), "r"))


def test_mine_resolves_to_the_only_calendar_holding_the_component():
    cals = visible_to([PERSONAL, WORK, FAMILY], TONY)
    assert resolve(cals, "mine", "VTODO", None).info == PERSONAL  # Work holds no tasks


def test_several_candidates_ask_which_one_unless_a_default_is_saved():
    cals = visible_to([PERSONAL, WORK], TONY)
    with pytest.raises(Refused, match="Personal, Work") as e:
        resolve(cals, "mine", "VEVENT", None)
    assert "set_default_calendar" in str(e.value)
    assert resolve(cals, "mine", "VEVENT", WORK.href).info == WORK


def test_a_stale_default_is_ignored():
    cals = visible_to([PERSONAL], TONY)
    assert resolve(cals, "mine", "VEVENT", "/c/uriel/gone/").info == PERSONAL


def test_no_calendar_of_theirs_gives_the_sharing_steps():
    with pytest.raises(Refused) as e:
        resolve(visible_to([FAMILY], TONY), "mine", "VEVENT", None)
    assert SHARING_STEPS in str(e.value)


def test_by_name_any_visible_calendar_case_insensitively():
    cals = visible_to([PERSONAL, FAMILY], TONY)
    assert resolve(cals, "family", "VEVENT", None).info == FAMILY
    with pytest.raises(Refused, match="Personal, Family"):
        resolve(cals, "Garden", "VEVENT", None)


def test_a_calendar_without_tasks_says_which_ones_have_them():
    cals = visible_to([PERSONAL, WORK], TONY)
    with pytest.raises(Refused, match="Work doesn't hold tasks: pick another \\(Personal\\)"):
        resolve(cals, "Work", "VTODO", None)


def test_a_persons_calendar_shared_with_a_group_stays_theirs_alone():
    tonys_family = cal("tf", "Tony's family", "anthony-headband", "groups/family", "users/uriel")
    assert visible_to([tonys_family], TONY) == [Visible(tonys_family, True)]
    assert visible_to([tonys_family], LIL_B) == []


def test_a_calendar_without_a_known_owner_is_invisible():
    assert visible_to([cal("n", "Nobody's", None, "groups/family")], TONY) == []


def test_a_shared_name_prefers_the_persons_own_calendar():
    own = cal("tf", "Family", "anthony-headband")
    assert resolve(visible_to([FAMILY, own], TONY), "Family", "VEVENT", None).info == own
    other = cal("f2", "Family", "uriel", "groups/family")
    with pytest.raises(Refused, match="2 calendars are called Family: rename one"):
        resolve(visible_to([FAMILY, other], TONY), "Family", "VEVENT", None)
