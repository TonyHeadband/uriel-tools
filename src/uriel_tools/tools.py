import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import psycopg
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from uriel_tools.db import apply_migrations
from uriel_tools.drafts import Drafts
from uriel_tools.edit.service import Denied, Editor
from uriel_tools.gating import ToolPolicy, caller_from
from uriel_tools.issues import Draft, Forgejo, TrackerError, draft_for
from uriel_tools.memory.questionnaire import File, Questionnaire
from uriel_tools.memory.service import Memories, Refused
from uriel_tools.rag import jobs, store
from uriel_tools.rag.search import search
from uriel_tools.rag.sources import normalise
from uriel_tools.schedules import rules
from uriel_tools.schedules.rules import Frequency, Weekday
from uriel_tools.schedules.service import Refused as ScheduleRefused
from uriel_tools.schedules.service import ScheduleDraft, Schedules
from uriel_tools.search import SearchUnavailable, TimeRange, WebSearch

log = logging.getLogger(__name__)


class Rag:
    """What the document tools need: a pool opened and migrated on first use, the query embedder, and the
    editor when Nextcloud credentials are configured."""

    def __init__(
        self, pool: AsyncConnectionPool, embedder, public_url: str = "", editor: Editor | None = None
    ):
        self.pool, self.embedder, self.public_url, self.editor = pool, embedder, public_url, editor
        self._ready = False
        self._lock = asyncio.Lock()

    async def ready(self) -> AsyncConnectionPool:
        if not self._ready:
            async with self._lock:
                if not self._ready:
                    await self.pool.open()
                    await apply_migrations(self.pool)
                    self._ready = True
        return self.pool


async def _pool(rag: Rag | None, what: str = "document search") -> AsyncConnectionPool:
    if rag is None:
        raise ToolError(f"{what} is not configured")
    try:
        return await rag.ready()
    except psycopg.Error as e:  # psycopg_pool's PoolTimeout is an OperationalError too
        raise ToolError(f"{what} is unavailable right now") from e


class Change(BaseModel):
    find: str
    replace: str


class Note(BaseModel):
    anchor: str  # the text the note is attached to
    text: str


async def _editor(rag: Rag | None) -> Editor:
    await _pool(rag)
    if rag.editor is None:
        raise ToolError("document editing is not configured")
    return rag.editor


async def _edit_call(coro):
    try:
        return await coro
    except Denied as e:
        raise ToolError(str(e)) from e
    except psycopg.Error as e:
        raise ToolError("document editing is unavailable right now") from e


async def _memory_call(coro):
    try:
        return await coro
    except Refused as e:
        raise ToolError(str(e)) from e
    except psycopg.Error as e:
        raise ToolError("memory is unavailable right now") from e


async def _schedule_call(coro):
    try:
        return await coro
    except ScheduleRefused as e:
        raise ToolError(str(e)) from e
    except psycopg.Error as e:
        raise ToolError("scheduling is unavailable right now") from e


def register_tools(
    mcp: FastMCP,
    rag: Rag | None = None,
    issues: Forgejo | None = None,
    questionnaire: Questionnaire | None = None,
    *,
    policy: ToolPolicy | None = None,
    web: WebSearch | None = None,
    default_tz: str = rules.DEFAULT_TZ,
) -> None:
    # Stubs with realistic shapes; the real integrations come in later specs.
    @mcp.tool
    def homelab_status() -> dict:
        """Summarise the homelab cluster: nodes, and pods that are not running."""
        return {
            "nodes": [{"name": "homelab", "ready": True, "cpu_pct": 23, "mem_pct": 58}],
            "pods_not_running": [],
            "gpu": {"name": "RTX 3070", "vram_used_mib": 5120, "vram_total_mib": 8192},
        }

    @mcp.tool
    def door_camera_last_event() -> dict:
        """Return the most recent event seen by the front door camera."""
        at = datetime.now(UTC) - timedelta(minutes=12)
        return {
            "camera": "front_door",
            "label": "person",
            "score": 0.91,
            "start_time": at.isoformat(),
            "snapshot_url": None,
        }

    @mcp.tool
    async def search_documents(query: str, ctx: Context, k: int = 5, folder: str | None = None) -> dict:
        """Search the household documents the caller may see: folders they shared with Uriel, and folders
        shared with a Uriel group they belong to (hits from those say who shared them in shared_by).

        Cite the title and url of every hit you use. Hits with source "ocr" were read from a scan and may
        contain transcription mistakes: say so and suggest opening the original. If awaiting_ocr is above
        zero, some scans are not searchable yet; offer ocr_now for the one the user means. A url opens only
        for people Nextcloud itself lets see the file, so don't promise it will open for a shared_by hit. A
        hit with edit_of is Uriel's edited copy of that document: present it as such, with edit_status
        ("draft" not reviewed yet, "changed" edited by a person since, "confirmed" approved)."""
        pool = await _pool(rag)
        try:
            return await search(pool, rag.embedder, caller_from(ctx), query, k, folder, rag.public_url)
        except psycopg.Error as e:
            raise ToolError("document search is unavailable right now") from e

    @mcp.tool
    async def ocr_now(path: str, ctx: Context) -> dict:
        """Read one of the caller's own scanned documents now instead of waiting for the nightly OCR run.

        Pass the document's path. It becomes searchable a few minutes after the reading finishes.
        """
        pool = await _pool(rag)
        caller = caller_from(ctx)
        try:
            folder_id = (
                await store.find_awaiting_ocr(pool, caller.nextcloud_ids, path) if caller.user else None
            )
            if folder_id is None:
                return {"queued": False, "message": "not found among your documents waiting for OCR"}
            return {"queued": True, "jobs_ahead": await jobs.request_ocr(pool, folder_id, normalise(path))}
        except psycopg.Error as e:
            raise ToolError("document search is unavailable right now") from e

    @mcp.tool
    async def inspect_document(path: str, ctx: Context) -> dict:
        """Describe a document before editing it: its type and pages, its form fields (name, type, value,
        choices) or whether it is a flat form, its signature fields, and whether an edited copy can be saved
        in its folder. Pass the path exactly as a search hit returned it."""
        editor = await _editor(rag)
        return await _edit_call(editor.inspect(caller_from(ctx), path))

    @mcp.tool
    async def fill_form(path: str, values: dict[str, str], ctx: Context) -> dict:
        """Fill a PDF form. Keys are form field names from inspect_document; on a flat form without fields,
        keys are the printed labels (e.g. "Family name") and each value is written beside its label.

        The original is never changed: the result is a new edit_ file next to it. Give the person the url, say
        what was filled, repeat not_found and warnings, and if approximate is true ask them to check the
        placed values. Never say a document was changed unless this returned `saved`."""
        editor = await _editor(rag)
        return await _edit_call(editor.fill(caller_from(ctx), path, values))

    @mcp.tool
    async def edit_text(
        path: str,
        ctx: Context,
        changes: list[Change] | None = None,
        notes: list[Note] | None = None,
    ) -> dict:
        """Change text in a Word, LibreOffice, text or PDF document (`changes`: find and replace), and/or
        attach notes to a piece of text (`notes`: comments in Word files, sticky notes in PDFs). Replacing
        printed text in a PDF is approximate.

        The original is never changed: the result is a new edit_ file next to it. Give the person the url, say
        what changed, repeat not_found and warnings. Never say a document was changed unless this returned
        `saved`."""
        editor = await _editor(rag)
        pairs = [(c.find, c.replace) for c in changes or []]
        anchored = [(n.anchor, n.text) for n in notes or []]
        return await _edit_call(editor.edit_text(caller_from(ctx), path, pairs, anchored))

    @mcp.tool
    async def sign_document(
        path: str, ctx: Context, where: str | None = None, confirmed: bool = False
    ) -> dict:
        """Place the person's own handwritten-signature image on a PDF: a visual signature, never anyone
        else's, and not a digital signature. `where` is the signature box to use (a name from
        inspect_document's signature_fields) or the printed text to sign beside (e.g. "Firma del genitore").
        When a form has boxes for several people, ask the person which is theirs: never guess. Default: the
        only signature box, else the last "Signature"/"Firma".

        First call with confirmed=false: it only says where the signature would go and saves nothing. Ask the
        person to confirm in the chat, and call again with confirmed=true only after they say yes. The result
        is a new edit_ file next to the original: give the url and ask them to review it."""
        editor = await _editor(rag)
        return await _edit_call(editor.sign(caller_from(ctx), path, where, confirmed))

    @mcp.tool
    async def confirm_edit(path: str, ctx: Context) -> dict:
        """Record that the person reviewed an edited copy (an edit_ file) and approves it. Call only when they
        say so."""
        editor = await _editor(rag)
        return await _edit_call(editor.confirm(caller_from(ctx), path))

    async def memories() -> Memories:
        return Memories(await _pool(rag, "memory"), questionnaire or Questionnaire())

    @mcp.tool
    async def remember(file: File, section: str, note: str, ctx: Context, replace: bool = False) -> dict:
        """Save one thing about the person you are talking to in their own files: file="user" (USER.md) for
        facts about them and their family, file="soul" (SOUL.md) for how they want you to talk. Save only what
        they told you, shortened: never guesses. For a reply to a profile question from next_question, use
        answer_question instead.

        section is a short heading for what the note is about. For user: Name (just the name, and what to
        call them: "call me Sir" is user Name, never a soul section), Birthday, Music, Hobbies, Food, Family,
        Work; for soul: Tone, Length, Language, Humour, Avoid. One note per fact. replace=true only for a new
        version of the same thing (a new name, a corrected birthday): it deletes the section's other notes,
        and the result lists them in `replaced`; if that wasn't meant, save them again. Tell the person in a
        few words what you saved; never claim to remember something unless this returned saved or
        unchanged. If the result has `questionnaire`, ask its question next."""
        m = await memories()
        return await _memory_call(m.remember(caller_from(ctx), file, section, note, replace))

    @mcp.tool
    async def forget(file: File, text: str, ctx: Context) -> dict:
        """Forget something from the person's USER.md (file="user") or SOUL.md (file="soul"). text is a
        section name ("Birthday") to drop the whole section, or words from the note. If the result is
        ambiguous, read the candidates to the person and ask which one."""
        m = await memories()
        return await _memory_call(m.forget(caller_from(ctx), file, text))

    @mcp.tool
    async def show_memory(file: File, ctx: Context) -> dict:
        """Show the person their USER.md (file="user": what you know about them) or SOUL.md (file="soul": how
        they want you to talk), as markdown."""
        m = await memories()
        return await _memory_call(m.show(caller_from(ctx), file))

    @mcp.tool
    async def next_question(file: File, ctx: Context, skip: bool = False) -> dict:
        """Start or resume setting up the person's USER.md (file="user") or SOUL.md (file="soul"), one
        question at a time. Put the result's `ask` to the person in your own words, and pass their reply to
        answer_question with the result's `section`. If they want to skip this question, call
        next_question(file, skip=true). Stop whenever they want: it resumes later where they left off. When
        status is done, show them the markdown."""
        m = await memories()
        return await _memory_call(m.next_question(caller_from(ctx), file, skip))

    @mcp.tool
    async def answer_question(file: File, section: str, answer: str, ctx: Context) -> dict:
        """Save the person's reply to the profile question you just asked: pass the `section` from that
        question's result, and the reply shortened to what matters ("Tony", "12 March"). This saves it; don't
        also call remember. The result holds the next question to ask (`ask`) or, when status is done, their
        file to show them."""
        m = await memories()
        return await _memory_call(m.answer_question(caller_from(ctx), file, section, answer))

    @mcp.tool
    async def set_style(
        ctx: Context,
        tone: str,
        length: str | None = None,
        humour: str | None = None,
        language: str | None = None,
        avoid: str | None = None,
    ) -> dict:
        """Save how the person wants you to talk, in SOUL.md, when they describe it: directly ("be brief") or
        by example (a character, a person, a style: "talk like a pirate", "like Jarvis"). Work out what that
        means and fill every part that applies, concretely: tone (the voice and manner), length, humour,
        language (only if they ask for one), avoid (what not to do). Each part replaces that part of their
        old style. Then summarise what you saved, and talk that way."""
        m = await memories()
        parts = {"tone": tone, "length": length, "humour": humour, "language": language, "avoid": avoid}
        return await _memory_call(m.set_style(caller_from(ctx), **parts))

    @mcp.tool
    async def memory_context(ctx: Context) -> dict:
        """The caller's USER.md and SOUL.md, for the gateway's system prompt: not for the model."""
        m = await memories()
        return await _memory_call(m.context(caller_from(ctx)))

    drafts: Drafts[Draft] = Drafts()

    def _tracker() -> Forgejo:
        if issues is None:
            raise ToolError("reporting problems is not configured")
        return issues

    @mcp.tool
    async def draft_issue(
        title: str,
        kind: Literal["bug", "feature"],
        description: str,
        ctx: Context,
        what_happened: str | None = None,
    ) -> dict:
        """Prepare a report for Anthony about Uriel itself: a problem (kind "bug") or something the person
        wishes Uriel could do (kind "feature"). Nothing is sent: it returns the exact issue that would be
        filed and a draft_id for file_issue.

        Use it when the person asks to tell Anthony about Uriel, and offer it when a tool fails or they are
        unhappy with a result. Write the title and description yourself from what the person said, keeping
        their meaning; never ask them to write it. Put in `what_happened` only what happened in this
        conversation (what you tried, any tool error). Leave out document contents and personal details
        that aren't needed to understand the problem. Then show the person the title and body and ask
        whether to send it."""
        _tracker()
        caller = caller_from(ctx)
        draft = draft_for(caller, title, kind, description, what_happened)
        return {
            "draft_id": drafts.add(caller, draft),
            "title": draft.title,
            "labels": draft.labels,
            "body": draft.body,
            "next": "Nothing has been sent yet. Show the person this title and body and ask whether to send "
            "it; call file_issue with this draft_id only after they say yes.",
        }

    @mcp.tool
    async def file_issue(draft_id: str, ctx: Context) -> dict:
        """Send a report prepared with draft_issue to Anthony's issue tracker. Call it only after the
        person has seen that draft and said yes. Give them the issue number and url. Never say a report
        was sent unless this returned `filed`."""
        tracker = _tracker()
        stored = drafts.get(caller_from(ctx), draft_id)
        if stored is None:
            raise ToolError(
                "no such draft (it may have expired): call draft_issue again and show the new draft"
            )
        if stored.result is None:
            try:
                stored.result = await tracker.create(stored.draft)
            except TrackerError as e:
                log.warning("filing an issue failed: %s", e)
                raise ToolError(
                    "couldn't reach the issue tracker right now; the draft is kept, try again later"
                ) from e
        return stored.result

    schedule_drafts: Drafts[ScheduleDraft] = Drafts()

    async def schedules() -> Schedules:
        return Schedules(await _pool(rag, "scheduling"), schedule_drafts, policy, default_tz)

    @mcp.tool
    async def draft_schedule(
        title: str,
        prompt: str,
        frequency: Frequency,
        time: str,
        ctx: Context,
        weekdays: list[Weekday] | None = None,
        date: str | None = None,
        day_of_month: int | None = None,
        tools: list[str] | None = None,
    ) -> dict:
        """Prepare work you will do for the person on a schedule, e.g. "every day at 9, a digest of news
        about X". Nothing is scheduled: it returns a draft_id for create_schedule. Not for the person's own
        reminders or appointments.

        title: a few words. prompt: what to do each time, written as the request you'll get then. time: HH:MM,
        24-hour, in the person's timezone. frequency: "once" (needs date, YYYY-MM-DD), "daily", "weekdays"
        (Monday to Friday), "weekly" (needs weekdays, e.g. ["mon", "thu"]) or "monthly" (needs day_of_month).
        tools: what the run may use, web_search when left out. Show the person `when`, `next_runs` and the
        prompt, and ask whether to schedule it."""
        s = await schedules()
        return await _schedule_call(
            s.draft(
                caller_from(ctx),
                title,
                prompt,
                frequency,
                time,
                weekdays=weekdays,
                on=date,
                day_of_month=day_of_month,
                tools=tools,
            )
        )

    @mcp.tool
    async def create_schedule(draft_id: str, ctx: Context) -> dict:
        """Schedule a draft from draft_schedule. Call it only after the person has seen that draft and said
        yes. Never say something is scheduled unless this returned `created`."""
        s = await schedules()
        return await _schedule_call(s.create(caller_from(ctx), draft_id))

    @mcp.tool
    async def list_schedules(ctx: Context) -> dict:
        """What you are doing for the person on a schedule: each active schedule with when it runs, its next
        run and how its last run went. Use it for "what are you doing for me?" and to find a schedule_id."""
        s = await schedules()
        return await _schedule_call(s.list(caller_from(ctx)))

    @mcp.tool
    async def list_runs(ctx: Context, schedule_id: int | None = None, since: str | None = None) -> dict:
        """Recent runs of the person's schedules, newest first (up to 20), with status and a summary of what
        was sent. schedule_id narrows it to one schedule; since (ISO 8601) to runs due from then on."""
        s = await schedules()
        return await _schedule_call(s.runs(caller_from(ctx), schedule_id, since))

    @mcp.tool
    async def cancel_schedule(schedule_id: int, ctx: Context) -> dict:
        """Stop one of the person's schedules. Find the schedule_id with list_schedules, and if more than one
        could be meant, ask which. To change a schedule, cancel it and draft a new one."""
        s = await schedules()
        return await _schedule_call(s.cancel(caller_from(ctx), schedule_id))

    @mcp.tool
    async def set_timezone(tz: str, ctx: Context) -> dict:
        """Save the person's timezone as an IANA name (e.g. "Europe/Rome") when they say where they are or ask
        for times in another zone. New schedules use it. If the result lists unchanged_schedules, tell the
        person those keep their old timezone and offer to redraft them."""
        s = await schedules()
        return await _schedule_call(s.set_timezone(caller_from(ctx), tz))

    @mcp.tool
    async def claim_due_runs(limit: int = 5) -> dict:
        """For the gateway's runner, not for the model: claim the scheduled runs that are due now."""
        s = await schedules()
        return await _schedule_call(s.claim_due(limit))

    @mcp.tool
    async def finish_run(
        run_id: int,
        status: Literal["ok", "failed", "undelivered"],
        summary: str | None = None,
        error: str | None = None,
        talk_message_id: int | None = None,
    ) -> dict:
        """For the gateway's runner, not for the model: record how a claimed run went."""
        s = await schedules()
        return await _schedule_call(s.finish(run_id, status, summary, error, talk_message_id))

    @mcp.tool
    async def web_search(query: str, time_range: TimeRange | None = None, max_results: int = 8) -> dict:
        """Search the web. For news, current events and anything your own knowledge may be out of date on.
        time_range ("day", "week", "month", "year") keeps only recent pages; max_results is at most 20.

        Answer from the snippets, cite the url of every result you use, and never invent a result or a url:
        if nothing relevant came back, say so."""
        if web is None:
            raise ToolError("web search is not configured")
        if not query.strip():
            raise ToolError("a web search needs a query")
        try:
            found = await web.search(query.strip(), time_range, max_results)
        except SearchUnavailable as e:
            log.warning("web search failed: %s", e)
            raise ToolError("web search is unavailable right now") from e
        # In the result as well as the docstring: some models skip the citations otherwise.
        return found | {
            "next": "Answer from these results only, and end with the URL of every result you used. "
            "If none is relevant, say so; never invent a result or a URL."
        }
