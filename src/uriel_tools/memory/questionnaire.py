"""The profile questions, from config/questionnaire.yaml."""

from typing import Literal

from pydantic import BaseModel

from uriel_tools.memory.store import Row

File = Literal["user", "soul"]


class Question(BaseModel):
    section: str
    ask: str
    replace: bool = False


class Questionnaire(BaseModel):
    user: list[Question] = []
    soul: list[Question] = []

    def questions(self, file: File) -> list[Question]:
        return self.user if file == "user" else self.soul


def pending(questions: list[Question], rows: list[Row]) -> list[Question]:
    """Questions still to ask: a section with a note or a skip row is settled, however it got there."""
    settled = {r.section.lower() for r in rows}
    return [q for q in questions if q.section.lower() not in settled]
