"""Schedules: the rules on top of the tables. Person-facing methods act on the caller's schedules only."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from psycopg_pool import AsyncConnectionPool

from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller, ToolPolicy
from uriel_tools.schedules import rules, store
from uriel_tools.schedules.rules import Frequency, Rule, RuleError

TITLE_MAX, PROMPT_MAX = 120, 2000
RUNS_MAX = 20
DEFAULT_TOOLS = ["web_search"]
SUMMARY_MAX, ERROR_MAX = 1000, 1000
LEASE = timedelta(minutes=10)
FINISHED = ("ok", "failed", "undelivered")


class Refused(ValueError):
    """A request the model can fix by rephrasing or asking the person; the message says how."""


@dataclass(frozen=True)
class ScheduleDraft:
    title: str
    prompt: str
    rule: Rule
    tools: tuple[str, ...]
    caldav_uid: str  # made at draft time, so creating the same draft twice finds the first schedule


def _owner(caller: Caller) -> str:
    if not caller.user:
        raise Refused("schedules need a signed-in person")
    return caller.user


def _stamp(name: str, at: datetime | None, tz: str) -> dict:
    """A timestamp for tool output: UTC with Z for machines, and local time for people."""
    if at is None:
        return {name: None, f"{name}_local": None}
    return {name: rules.iso(at), f"{name}_local": rules.local(at, tz)}


def _run(row: dict) -> dict:
    return (
        {
            "run_id": row["id"],
            "schedule_id": row["schedule_id"],
            "title": row["title"],
            "status": row["status"],
        }
        | _stamp("due_at", row["due_at"], row["tz"])
        | _stamp("finished_at", row["finished_at"], row["tz"])
        | {"summary": row["summary"], "error": row["error"]}
    )


class Schedules:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        drafts: Drafts[ScheduleDraft],
        policy: ToolPolicy | None,
        default_tz: str = rules.DEFAULT_TZ,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.pool, self.drafts, self.policy = pool, drafts, policy
        self.unattended = policy.unattended_tools() if policy else frozenset()
        self.default_tz, self.now = default_tz, clock

    def _schedule(self, row: dict) -> dict:
        return {
            "schedule_id": row["id"],
            "title": row["title"],
            "prompt": row["prompt"],
            "when": rules.describe(row["rrule"], row["dtstart"], row["tz"]),
            "tools": list(row["tools"]),
            "tz": row["tz"],
            "status": row["status"],
        } | _stamp("next_run_at", row["next_run_at"], row["tz"])

    def _created(self, row: dict) -> dict:
        """A create, or its replay: only while the schedule still runs, since a replay can't bring it back."""
        if row["status"] != "active":
            raise Refused(f"that schedule was {row['status']}: call draft_schedule again to make a new one")
        return {"created": True} | self._schedule(row)

    async def draft(
        self,
        caller: Caller,
        title: str,
        prompt: str,
        frequency: Frequency,
        at: str,
        *,
        weekdays: list[str] | None = None,
        on: str | None = None,
        day_of_month: int | None = None,
        tools: list[str] | None = None,
    ) -> dict:
        owner = _owner(caller)
        title, prompt = " ".join(title.split()), prompt.strip()
        if not title or not prompt:
            raise Refused("a schedule needs a title and a prompt")
        if len(title) > TITLE_MAX:
            raise Refused(f"titles are at most {TITLE_MAX} characters: shorten it")
        if len(prompt) > PROMPT_MAX:
            raise Refused(f"prompts are at most {PROMPT_MAX} characters: say what to do more briefly")
        wanted = list(dict.fromkeys(tools if tools is not None else DEFAULT_TOOLS))
        refused = [t for t in wanted if t not in self.unattended]
        if refused:
            raise Refused(
                f"these tools can't run unattended: {', '.join(refused)}. "
                f"Tools a schedule can use: {', '.join(sorted(self.unattended)) or 'none'}"
            )
        # A run acts for the person with their tools only: the gateway would refuse any other every time.
        theirs = sorted(t for t in self.unattended if self.policy.allowed(t, caller.groups))
        refused = [t for t in wanted if t not in theirs]
        if refused:
            raise Refused(
                f"these tools are not available to you: {', '.join(refused)}. "
                f"Tools your schedules can use: {', '.join(theirs) or 'none'}"
            )
        tz = await store.get_tz(self.pool, owner, self.default_tz)
        now = self.now()
        try:
            rule = rules.build(frequency, at, tz, now, weekdays=weekdays, on=on, day_of_month=day_of_month)
        except RuleError as e:
            raise Refused(str(e)) from e
        draft = ScheduleDraft(title, prompt, rule, tuple(wanted), f"uriel-{uuid.uuid4().hex}")
        next_runs = rules.upcoming(rule.rrule, rule.dtstart, rule.tz, now, 3)
        return {
            "draft_id": self.drafts.add(caller, draft),
            "title": title,
            "prompt": prompt,
            "when": rules.describe(rule.rrule, rule.dtstart, rule.tz),
            "next_runs": [_stamp("at", at_, rule.tz) for at_ in next_runs],
            "tools": wanted,
            "tz": rule.tz,
            "next": "Nothing is scheduled yet. Show the person what will run and when, and call "
            "create_schedule with this draft_id only after they say yes. If they want a change, draft again.",
        }

    async def create(self, caller: Caller, draft_id: str) -> dict:
        owner = _owner(caller)
        stored = self.drafts.get(caller, draft_id)
        if stored is None:
            raise Refused(
                "no such draft (it may have expired): call draft_schedule again and show the new draft"
            )
        if stored.result is not None:
            return self._created(await store.get(self.pool, owner, stored.result["schedule_id"]))
        d = stored.draft
        first = next(rules.occurrences(d.rule.rrule, d.rule.dtstart, d.rule.tz, self.now()), None)
        if first is None:
            raise Refused("that time has already passed: call draft_schedule again with a later time")
        row = await store.insert(
            self.pool,
            owner=owner,
            owner_sub=caller.sub,
            title=d.title,
            prompt=d.prompt,
            rule=d.rule,
            tools=list(d.tools),
            caldav_uid=d.caldav_uid,
            next_run_at=first,
        )
        stored.result = self._created(row)
        return stored.result

    async def list(self, caller: Caller) -> dict:
        rows = await store.active(self.pool, _owner(caller))
        schedules = []
        for row in rows:
            last = None
            if row["last_status"] is not None:
                last = {"status": row["last_status"]} | _stamp("due_at", row["last_due_at"], row["tz"])
            schedules.append(self._schedule(row) | {"last_run": last})
        return {"schedules": schedules}

    async def runs(self, caller: Caller, schedule_id: int | None = None, since: str | None = None) -> dict:
        owner = _owner(caller)
        if schedule_id is not None and await store.get(self.pool, owner, schedule_id) is None:
            raise Refused(f"there's no schedule {schedule_id} among yours: call list_schedules")
        after = None
        if since:
            try:
                after = datetime.fromisoformat(since.strip())
            except ValueError as e:
                raise Refused("since must be an ISO 8601 time, e.g. 2026-09-29T00:00:00Z") from e
            if after.tzinfo is None:
                after = after.replace(tzinfo=UTC)
        rows = await store.runs(self.pool, owner, schedule_id, after, RUNS_MAX)
        return {"runs": [_run(r) for r in rows]}

    async def cancel(self, caller: Caller, schedule_id: int) -> dict:
        owner = _owner(caller)
        await store.cancel(self.pool, owner, schedule_id, "chat")
        row = await store.get(self.pool, owner, schedule_id)
        if row is None:
            raise Refused(f"there's no schedule {schedule_id} among yours: call list_schedules")
        return {"schedule_id": row["id"], "title": row["title"], "status": row["status"]}

    async def set_timezone(self, caller: Caller, tz: str) -> dict:
        owner = _owner(caller)
        try:
            tz = rules.zone_name(tz)
        except RuleError as e:
            raise Refused(str(e)) from e
        await store.set_tz(self.pool, owner, tz)
        others = await store.in_other_tz(self.pool, owner, tz)
        result = {"tz": tz, "now_local": rules.local(self.now(), tz)}
        if not others:
            return result | {"note": "New schedules will use this timezone."}
        return result | {
            "unchanged_schedules": [
                {"schedule_id": r["id"], "title": r["title"], "tz": r["tz"]} for r in others
            ],
            "note": "New schedules will use this timezone. Existing schedules keep the one they were made "
            "in: offer to redraft these (cancel_schedule, then draft_schedule again).",
        }

    async def claim_due(self, limit: int = 5) -> dict:
        limit = max(1, min(limit, 20))
        rows = await store.claim_due(self.pool, self.now(), limit, LEASE)
        runs = []
        for r in rows:
            runs.append(
                {k: r[k] for k in ("run_id", "owner", "owner_sub", "title", "prompt")}
                | {"tools": list(r["tools"])}
                | _stamp("due_at", r["due_at"], r["tz"])
                | {"tz": r["tz"]}
            )
        return {"runs": runs}

    async def finish(
        self,
        run_id: int,
        status: str,
        summary: str | None = None,
        error: str | None = None,
        talk_message_id: int | None = None,
    ) -> dict:
        if status not in FINISHED:
            raise Refused(f"status must be one of {', '.join(FINISHED)}")
        summary = summary[:SUMMARY_MAX] if summary else None
        error = error[:ERROR_MAX] if error else None
        out = await store.finish(self.pool, run_id, status, summary, error, talk_message_id, self.now())
        if out is None:
            raise Refused(f"there's no run {run_id}")
        return out
