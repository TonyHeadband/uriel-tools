import pytest
from fastmcp import Client

from tests.fakes.models import HashEmbedder
from uriel_tools.app import build_mcp
from uriel_tools.gating import Caller, ToolPolicy
from uriel_tools.memory.questionnaire import Question, Questionnaire
from uriel_tools.memory.service import Memories, Refused
from uriel_tools.tools import Rag

pytestmark = pytest.mark.db

TONY = Caller("anthony-headband", frozenset({"family"}), "r1")
LIL_B = Caller("lil-b", frozenset({"family"}), "r2")
QUESTIONS = Questionnaire(
    user=[
        Question(section="Name", ask="What should I call you?", replace=True),
        Question(section="Music", ask="What music do you enjoy?"),
    ],
    soul=[Question(section="Tone", ask="How should I sound?", replace=True)],
)


@pytest.fixture
def mem(pool):
    return Memories(pool, QUESTIONS)


async def md(mem, caller=TONY, file="user"):
    return (await mem.show(caller, file))["markdown"]


async def test_the_first_note_creates_the_file_in_questionnaire_order(mem):
    assert await md(mem) == ""
    out = await mem.remember(TONY, "user", "Hobbies", "Climbing")
    assert out == {"status": "saved", "file": "USER.md", "section": "Hobbies"}
    await mem.remember(TONY, "user", "Music", "Jazz")
    await mem.remember(TONY, "user", "Name", "Tony", replace=True)
    assert await md(mem) == "# About Tony\n## Name\n- Tony\n## Music\n- Jazz\n## Hobbies\n- Climbing"


async def test_sections_are_normalised_and_duplicates_unchanged(mem):
    await mem.remember(TONY, "user", "  music ", "Jazz")
    again = await mem.remember(TONY, "user", "MUSIC", "  jazz ")
    assert again["status"] == "unchanged"
    await mem.remember(TONY, "user", "MUSIC", "Blues")
    assert await md(mem) == "# About anthony-headband\n## Music\n- Jazz\n- Blues"


async def test_replace_clears_the_section(mem):
    await mem.remember(TONY, "user", "Name", "Anthony")
    await mem.remember(TONY, "user", "Name", "Tony", replace=True)
    assert await md(mem) == "# About Tony\n## Name\n- Tony"


async def test_limits(mem):
    with pytest.raises(Refused, match="200"):
        await mem.remember(TONY, "user", "Music", "x" * 201)
    with pytest.raises(Refused, match="40 characters"):
        await mem.remember(TONY, "user", "s" * 41, "x")
    for i in range(20):  # a file holds 20 notes
        await mem.remember(TONY, "user", "Hobbies", f"hobby {i}")
    with pytest.raises(Refused, match="USER.md is full"):
        await mem.remember(TONY, "user", "Music", "Jazz")
    # Replacing a section of a full file is still allowed: it doesn't grow the file.
    assert (await mem.remember(TONY, "user", "Hobbies", "Climbing", replace=True))["status"] == "saved"
    assert (await mem.remember(TONY, "soul", "Tone", "Warm"))["status"] == "saved"  # files count separately


async def test_blank_input_is_refused(mem):
    await mem.remember(TONY, "user", "Music", "Jazz")
    with pytest.raises(Refused):
        await mem.remember(TONY, "user", "  ", "Jazz")
    with pytest.raises(Refused):
        await mem.remember(TONY, "user", "Music", "   ")
    with pytest.raises(Refused):
        await mem.forget(TONY, "user", "  ")
    assert await md(mem) == "# About anthony-headband\n## Music\n- Jazz"


async def test_forget_by_section_by_note_ambiguous_and_missing(mem):
    await mem.remember(TONY, "user", "Birthday", "3 May")
    await mem.remember(TONY, "user", "Music", "Jazz")
    await mem.remember(TONY, "user", "Hobbies", "Jazz piano")
    assert await mem.forget(TONY, "user", "birthday") == {"status": "forgotten", "removed": ["3 May"]}
    ambiguous = await mem.forget(TONY, "user", "JAZ")
    assert ambiguous == {"status": "ambiguous", "candidates": ["Music: Jazz", "Hobbies: Jazz piano"]}
    assert await mem.forget(TONY, "user", "piano") == {"status": "forgotten", "removed": ["Jazz piano"]}
    assert await mem.forget(TONY, "user", "opera") == {"status": "not_found"}
    assert await md(mem) == "# About anthony-headband\n## Music\n- Jazz"


async def test_each_person_sees_only_their_own(mem):
    await mem.remember(TONY, "user", "Name", "Tony", replace=True)
    await mem.remember(TONY, "soul", "Tone", "Warm and cheerful")
    assert await md(mem, LIL_B) == ""
    assert await mem.context(LIL_B) == {"user": "", "soul": ""}
    assert await mem.context(TONY) == {
        "user": "# About Tony\n## Name\n- Tony",
        "soul": "# How to talk to Tony\n## Tone\n- Warm and cheerful",
    }
    assert await mem.forget(LIL_B, "user", "Tony") == {"status": "not_found"}


async def test_signed_out_callers_are_refused(mem):
    with pytest.raises(Refused, match="signed-in"):
        await mem.remember(Caller(None, frozenset({"family"}), "r"), "user", "Music", "Jazz")


async def test_questionnaire_asks_in_order_skips_and_resumes(pool, mem):
    q = await mem.next_question(TONY, "user")
    assert q == {
        "status": "question",
        "section": "Name",
        "ask": "What should I call you?",
        "replace": True,
        "remaining": 2,
    }
    await mem.remember(TONY, "user", "Name", "Tony", replace=q["replace"])
    resumed = Memories(pool, QUESTIONS)  # a new conversation: nothing is kept in memory between calls
    assert (await resumed.next_question(TONY, "user"))["section"] == "Music"
    done = await resumed.next_question(TONY, "user", skip=True)
    assert done == {"status": "done", "file": "USER.md", "markdown": "# About Tony\n## Name\n- Tony"}
    assert (await resumed.next_question(TONY, "user", skip=True))["status"] == "done"  # nothing left to skip
    assert await resumed.forget(TONY, "user", "music") == {"status": "forgotten", "removed": []}
    assert (await resumed.next_question(TONY, "user"))["section"] == "Music"  # forgetting asks it again


async def test_answering_a_skipped_section_in_chat_settles_it(mem):
    await mem.next_question(TONY, "user")
    await mem.next_question(TONY, "user", skip=True)  # skips Name
    await mem.remember(TONY, "user", "name", "Tony")
    assert await md(mem) == "# About Tony\n## Name\n- Tony"
    assert (await mem.next_question(TONY, "user"))["section"] == "Music"


async def test_answer_question_saves_the_answer_and_returns_the_next_question(mem):
    await mem.next_question(TONY, "user")
    first = await mem.answer_question(TONY, "user", "Name", "Tony")
    assert first == {
        "saved": {"status": "saved", "file": "USER.md", "section": "Name"},
        "status": "question",
        "section": "Music",
        "ask": "What music do you enjoy?",
        "replace": False,
        "remaining": 1,
    }
    last = await mem.answer_question(TONY, "user", "music", "Jazz")  # section names are matched loosely
    assert last["saved"]["section"] == "Music"
    assert last["status"] == "done"
    assert last["markdown"] == "# About Tony\n## Name\n- Tony\n## Music\n- Jazz"


async def test_an_answer_already_saved_is_not_saved_again_elsewhere(mem):
    # A model that saved the reply with remember and then also calls answer_question must not have "Tony"
    # land in the next question's section.
    await mem.remember(TONY, "user", "Name", "Tony", replace=True)
    again = await mem.answer_question(TONY, "user", "Name", "Tony")
    assert again["saved"] == {"status": "already answered", "file": "USER.md", "section": "Name"}
    assert again["section"] == "Music"
    assert await md(mem) == "# About Tony\n## Name\n- Tony"


async def test_answer_question_only_takes_profile_questions(mem):
    with pytest.raises(Refused, match="remember"):
        await mem.answer_question(TONY, "user", "Pets", "A cat")
    assert await md(mem) == ""


async def test_set_style_saves_each_part_and_reports_what_it_replaced(mem):
    await mem.remember(TONY, "soul", "Tone", "Calm", replace=True)
    await mem.remember(TONY, "soul", "Language", "Italian")
    out = await mem.set_style(TONY, tone="Hearty pirate, dramatic", length="Short", avoid="Modern slang")
    assert out["status"] == "saved"
    assert out["saved"] == ["Tone", "Length", "Avoid"]
    assert out["replaced"] == {"Tone": ["Calm"]}
    shown = await md(mem, file="soul")
    for text in ("Hearty pirate, dramatic", "Short", "Modern slang", "Italian"):  # other parts are kept
        assert text in shown
    assert "Calm" not in shown


async def test_set_style_needs_at_least_one_part(mem):
    with pytest.raises(Refused, match="tone"):
        await mem.set_style(TONY)


async def test_sections_filled_in_chat_are_not_asked(mem):
    await mem.remember(TONY, "soul", "Tone", "Straight to the point")
    assert (await mem.next_question(TONY, "soul"))["status"] == "done"


async def test_the_questions_come_from_the_questionnaire(pool):
    pets = Memories(pool, Questionnaire(user=[Question(section="Pets", ask="Any pets?")]))
    assert (await pets.next_question(TONY, "user"))["ask"] == "Any pets?"
    assert (await pets.next_question(TONY, "soul"))["status"] == "done"


MEMORY_TOOLS = [
    "remember",
    "forget",
    "show_memory",
    "next_question",
    "answer_question",
    "set_style",
    "memory_context",
]
POLICY = ToolPolicy.model_validate(
    {"tools": {t: {"groups": ["family"], "category": "test"} for t in MEMORY_TOOLS}}
)
TONY_META = {"user": "anthony-headband", "groups": ["family"], "request_id": "r1"}
LIL_B_META = {"user": "lil-b", "groups": ["family"], "request_id": "r2"}


async def call(pool, tool, args, meta):
    async with Client(build_mcp(POLICY, Rag(pool, HashEmbedder()), questionnaire=QUESTIONS)) as c:
        return await c.call_tool(tool, args, meta=meta, raise_on_error=False)


async def test_memory_tools_through_the_server(pool):
    saved = await call(pool, "remember", {"file": "user", "section": "Music", "note": "Jazz"}, TONY_META)
    assert saved.structured_content["status"] == "saved"
    ctx = await call(pool, "memory_context", {}, TONY_META)
    assert ctx.structured_content == {"user": "# About anthony-headband\n## Music\n- Jazz", "soul": ""}
    assert (await call(pool, "memory_context", {}, LIL_B_META)).structured_content == {"user": "", "soul": ""}
    q = await call(pool, "next_question", {"file": "user"}, TONY_META)
    assert q.structured_content["section"] == "Name"
    shown = await call(pool, "show_memory", {"file": "soul"}, TONY_META)
    assert shown.structured_content["markdown"] == "" and "next_question" in shown.structured_content["hint"]
    gone = await call(pool, "forget", {"file": "user", "text": "jazz"}, TONY_META)
    assert gone.structured_content == {"status": "forgotten", "removed": ["Jazz"]}


async def test_answer_question_and_set_style_through_the_server(pool):
    answer = {"file": "user", "section": "Name", "answer": "Tony"}
    answered = await call(pool, "answer_question", answer, TONY_META)
    assert answered.structured_content["saved"]["section"] == "Name"
    assert answered.structured_content["section"] == "Music"
    styled = await call(pool, "set_style", {"tone": "Like Jarvis: dry, polite, precise"}, TONY_META)
    assert styled.structured_content["saved"] == ["Tone"]
    assert "Jarvis" in styled.structured_content["markdown"]


async def test_memory_tool_errors_are_readable(pool):
    note = {"file": "user", "section": "Music", "note": "x"}
    anonymous = await call(pool, "remember", note, {"groups": ["family"]})
    assert anonymous.is_error and "signed-in" in anonymous.content[0].text
    long = await call(pool, "remember", note | {"note": "x" * 201}, TONY_META)
    assert long.is_error and "200" in long.content[0].text
    diary = await call(pool, "show_memory", {"file": "diary"}, TONY_META)
    assert diary.is_error
    guest = await call(pool, "memory_context", {}, {"user": "guest", "groups": [], "request_id": "r"})
    assert guest.is_error and "not permitted" in guest.content[0].text


async def test_memory_without_a_database_says_not_configured():
    async with Client(build_mcp(POLICY, questionnaire=QUESTIONS)) as c:
        res = await c.call_tool("show_memory", {"file": "user"}, meta=TONY_META, raise_on_error=False)
    assert res.is_error and "memory is not configured" in res.content[0].text


async def test_a_note_inside_another_can_still_be_forgotten(mem):
    await mem.remember(TONY, "user", "Music", "Jazz")
    await mem.remember(TONY, "user", "Music", "Jazz fusion")
    await mem.remember(TONY, "user", "Hobbies", "Jazz")
    assert (await mem.forget(TONY, "user", "jazz"))["status"] == "ambiguous"  # the same note in two sections
    # The candidate text forget hands out picks exactly one note.
    assert await mem.forget(TONY, "user", "Hobbies: Jazz") == {"status": "forgotten", "removed": ["Jazz"]}
    assert await mem.forget(TONY, "user", "jazz") == {"status": "forgotten", "removed": ["Jazz"]}
    assert await md(mem) == "# About anthony-headband\n## Music\n- Jazz fusion"


async def test_replace_reports_what_it_overwrote(mem):
    # A small model once saved "Call me Sir" over the Tone with replace=true; it must see what it lost.
    await mem.remember(TONY, "soul", "Tone", "Straight to the point")
    out = await mem.remember(TONY, "soul", "Tone", "Call me Sir", replace=True)
    assert out["status"] == "saved" and out["replaced"] == ["Straight to the point"]
    assert "replaced" not in await mem.remember(TONY, "soul", "Length", "Short", replace=True)


async def test_remember_answering_a_profile_question_carries_the_next_one(mem):
    # qwen3:8b answers the questionnaire with remember and never calls answer_question (evals 2026-09-29), so
    # the next step has to ride on remember's own result.
    out = await mem.remember(TONY, "user", "Name", "Tony", replace=True)
    assert out["questionnaire"] == {
        "status": "question",
        "section": "Music",
        "ask": "What music do you enjoy?",
        "replace": False,
        "remaining": 1,
        "note": "if you are going through their profile questions, ask this one next",
    }
    done = await mem.remember(TONY, "user", "Music", "Jazz")
    assert done["questionnaire"]["status"] == "done"
    # A section outside the questionnaire, or already answered, says nothing about it.
    assert "questionnaire" not in await mem.remember(TONY, "user", "Hobbies", "Climbing")
    assert "questionnaire" not in await mem.remember(TONY, "user", "Music", "Blues")
