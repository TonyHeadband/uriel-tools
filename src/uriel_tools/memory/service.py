"""Per-person memory: the rules on top of the notes table. Each method acts on the caller's notes only."""

from psycopg_pool import AsyncConnectionPool

from uriel_tools.gating import Caller
from uriel_tools.memory import store
from uriel_tools.memory.questionnaire import File, Questionnaire, pending
from uriel_tools.memory.render import render, section_name
from uriel_tools.memory.store import Row

NOTE_MAX, SECTION_MAX, FILE_MAX = 200, 40, 20  # both files go into every prompt: ~800 tokens each at most
FILE_NAMES = {"user": "USER.md", "soul": "SOUL.md"}
STYLE_SECTIONS = {
    "tone": "Tone",
    "length": "Length",
    "humour": "Humour",
    "language": "Language",
    "avoid": "Avoid",
}


class Refused(ValueError):
    """A request the model can fix by rephrasing or asking the person; the message says how."""


def _owner(caller: Caller) -> str:
    if not caller.user:
        raise Refused("memory needs a signed-in person")
    return caller.user


def _name(user_rows: list[Row], owner: str) -> str:
    return next((r.note for r in user_rows if r.note is not None and r.section.lower() == "name"), owner)


class Memories:
    def __init__(self, pool: AsyncConnectionPool, questionnaire: Questionnaire):
        self.pool, self.questionnaire = pool, questionnaire

    async def remember(
        self, caller: Caller, file: File, section: str, note: str, replace: bool = False
    ) -> dict:
        owner = _owner(caller)
        section, note = section_name(section), " ".join(note.split())
        if not section or not note:
            raise Refused("both a section and a note are needed")
        if len(section) > SECTION_MAX:
            raise Refused(f"section names are at most {SECTION_MAX} characters: use a short heading (Music)")
        if len(note) > NOTE_MAX:
            raise Refused(f"notes are at most {NOTE_MAX} characters: save the gist")
        rows = await store.rows(self.pool, owner, file)
        same = [r for r in rows if r.note is not None and r.section.lower() == section.lower()]
        result = {"file": FILE_NAMES[file], "section": section}
        if not replace and any(r.note.lower() == note.lower() for r in same):
            return {"status": "unchanged"} | result
        kept = sum(r.note is not None for r in rows) - (len(same) if replace else 0)
        if kept >= FILE_MAX:
            raise Refused(f"{FILE_NAMES[file]} is full ({FILE_MAX} notes): ask the person what to forget")
        was_pending = any(
            q.section.lower() == section.lower() for q in pending(self.questionnaire.questions(file), rows)
        )
        await store.add(self.pool, owner, file, section, note, clear_section=replace)
        replaced = [r.note for r in same if r.note.lower() != note.lower()] if replace else []
        out = {"status": "saved"} | result | ({"replaced": replaced} if replaced else {})
        if was_pending:  # models answer profile questions with remember; hand them the next step here
            nxt = await self.next_question(caller, file)
            if nxt["status"] == "question":
                nxt["note"] = "if you are going through their profile questions, ask this one next"
            out["questionnaire"] = nxt
        return out

    async def forget(self, caller: Caller, file: File, text: str) -> dict:
        owner = _owner(caller)
        needle = " ".join(text.split()).lower()
        if not needle:
            raise Refused("say what to forget: a section like Birthday, or words from the note")
        rows = await store.rows(self.pool, owner, file)
        section = [r for r in rows if r.section.lower() == needle]
        if section:  # the whole section, skip row included, so the questionnaire asks it again
            await store.delete(self.pool, [r.id for r in section])
            return {"status": "forgotten", "removed": [r.note for r in section if r.note is not None]}
        notes = [r for r in rows if r.note is not None]
        # An exact note, or the "Section: note" form of the candidates, wins over a note containing it:
        # otherwise "Jazz" next to "Jazz fusion" could never be forgotten on its own.
        exact = [r for r in notes if needle in (r.note.lower(), f"{r.section}: {r.note}".lower())]
        hits = exact or [r for r in notes if needle in r.note.lower()]
        if not hits:
            return {"status": "not_found"}
        if len(hits) > 1:
            return {"status": "ambiguous", "candidates": [f"{r.section}: {r.note}" for r in hits]}
        await store.delete(self.pool, [hits[0].id])
        return {"status": "forgotten", "removed": [hits[0].note]}

    async def context(self, caller: Caller) -> dict:
        owner = _owner(caller)
        user = await store.rows(self.pool, owner, "user")
        soul = await store.rows(self.pool, owner, "soul")
        name = _name(user, owner)
        return {"user": self._render(user, "user", name), "soul": self._render(soul, "soul", name)}

    async def show(self, caller: Caller, file: File) -> dict:
        markdown = (await self.context(caller))[file]
        hint = {} if markdown else {"hint": "nothing saved yet: offer the questions (next_question)"}
        return {"file": FILE_NAMES[file], "markdown": markdown} | hint

    async def next_question(self, caller: Caller, file: File, skip: bool = False) -> dict:
        owner = _owner(caller)
        # Progress is the notes themselves, so a questionnaire resumes in any later conversation.
        left = pending(self.questionnaire.questions(file), await store.rows(self.pool, owner, file))
        if skip and left:
            await store.add(self.pool, owner, file, left[0].section, None, clear_section=False)
            left = left[1:]
        if not left:
            return {
                "status": "done",
                "file": FILE_NAMES[file],
                "markdown": (await self.context(caller))[file],
            }
        q = left[0]
        return {
            "status": "question",
            "section": q.section,
            "ask": q.ask,
            "replace": q.replace,
            "remaining": len(left),
        }

    async def answer_question(self, caller: Caller, file: File, section: str, answer: str) -> dict:
        """Save the reply to one profile question and return the next one, so the next step is in the result
        rather than left to the model to remember. An already answered question saves nothing: a reply the
        model also saved with remember must not land in the following question's section."""
        wanted = section_name(section).lower()
        question = next((q for q in self.questionnaire.questions(file) if q.section.lower() == wanted), None)
        if question is None:
            raise Refused(f"{section} is not a profile question in {FILE_NAMES[file]}: save it with remember")
        left = pending(self.questionnaire.questions(file), await store.rows(self.pool, _owner(caller), file))
        if question in left:
            saved = await self.remember(caller, file, question.section, answer, replace=question.replace)
            saved.pop("questionnaire", None)  # the next question is returned below, once
        else:
            saved = {"status": "already answered", "file": FILE_NAMES[file], "section": question.section}
        return {"saved": saved} | await self.next_question(caller, file)

    async def set_style(self, caller: Caller, **parts: str | None) -> dict:
        """Save a whole way of talking at once, one soul section per part, each replacing that section."""
        given = [(STYLE_SECTIONS[k], v) for k, v in parts.items() if v and v.strip()]
        if not given:
            raise Refused("describe at least the tone (and length, humour, language or avoid if they apply)")
        saved, replaced = [], {}
        for section, note in given:
            result = await self.remember(caller, "soul", section, note, replace=True)
            saved.append(result["section"])
            if result.get("replaced"):
                replaced[result["section"]] = result["replaced"]
        out = {"status": "saved", "saved": saved, "markdown": (await self.context(caller))["soul"]}
        return out | ({"replaced": replaced} if replaced else {})

    def _render(self, rows: list[Row], file: File, name: str) -> str:
        return render(rows, file, name, [q.section for q in self.questionnaire.questions(file)])
