"""The Nextcloud mirror of each person's schedules: an event per schedule, a done-task per finished run.

The database is the source of truth. Push writes what changed (rows with mirror_dirty) and always overwrites.
Pull reads sync-collection and cancels a schedule only when its event comes back as an explicit 404: a
failed request, an empty answer, a full resync or a missing calendar never cancels anything. An event that
comes back changed, with an ETag other than the one Uriel's own PUT got, was edited in Nextcloud: it's marked
for the next push, which puts Uriel's version back.
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from uriel_tools.rag.webdav import WebDavError
from uriel_tools.schedules import ics, store
from uriel_tools.schedules.caldav import CalDav, CalendarMissing, InvalidSyncToken

log = logging.getLogger(__name__)
MAX_BACKOFF_S = 15 * 60
SKIPPED = "Uriel couldn't run this on time, and it was too late to send, so it was skipped."


def slug(owner: str) -> str:
    """A URL-safe calendar name. When that changes the name, a hash of the owner keeps it theirs alone:
    "anna.b" and "anna_b" would otherwise share a calendar, since MKCALENDAR's 405 reads as "already made"."""
    safe = re.sub(r"[^a-z0-9-]+", "-", owner.lower()).strip("-") or "unnamed"
    if safe == owner:
        return safe
    return f"{safe}-{hashlib.sha256(owner.encode()).hexdigest()[:6]}"


class Mirror:
    def __init__(
        self,
        pool: Callable[[], Awaitable[AsyncConnectionPool]],
        dav: CalDav,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        base_backoff_s: float = 30.0,
    ):
        self._pool, self.dav, self._clock, self._now = pool, dav, clock, now
        self._base_backoff = base_backoff_s
        self._retry_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def _waiting(self, owner: str) -> bool:
        return self._clock() < self._retry_at.get(owner, 0.0)

    def _failed(self, owner: str, error: Exception) -> None:
        n = self._failures[owner] = self._failures.get(owner, 0) + 1
        delay = min(self._base_backoff * 2 ** (n - 1), MAX_BACKOFF_S)
        self._retry_at[owner] = self._clock() + delay
        log.warning("calendar mirror for %s failed (%s); retrying in %.0f s", owner, error, delay)

    def _succeeded(self, owner: str) -> None:
        self._failures.pop(owner, None)
        self._retry_at.pop(owner, None)

    async def _calendars(self, pool: AsyncConnectionPool, owner: str) -> dict:
        """The owner's two calendars, made and shared with them on first use."""
        row = await store.calendars(pool, owner)
        if row is not None:
            return row
        events = await self.dav.make_calendar(f"uriel-{slug(owner)}", f"Uriel · {owner}", "VEVENT")
        tasks = await self.dav.make_calendar(f"uriel-{slug(owner)}-done", "Uriel · done", "VTODO")
        # Read-write: a read-only share can't delete, and deleting the event is how a person cancels.
        await self.dav.share(events, owner)
        await self.dav.share(tasks, owner)
        await store.save_calendars(pool, owner, events, tasks)
        return await store.calendars(pool, owner)

    async def _put(
        self, pool: AsyncConnectionPool, owner: str, which: str, uid: str, text: str
    ) -> str | None:
        cals = await self._calendars(pool, owner)
        try:
            return await self.dav.put(cals[which], uid, text)
        except CalendarMissing:
            # Deleted in Nextcloud: make both again, re-push every active schedule, then this item.
            await store.forget_calendars(pool, owner)
            cals = await self._calendars(pool, owner)
            return await self.dav.put(cals[which], uid, text)

    async def _push_schedule(self, pool: AsyncConnectionPool, s: dict) -> None:
        owner, now = s["owner"], self._now()
        if s["status"] == "active":
            text = ics.event(
                uid=s["caldav_uid"],
                schedule_id=s["id"],
                title=s["title"],
                prompt=s["prompt"],
                tools=list(s["tools"]),
                rrule=s["rrule"],
                dtstart=s["dtstart"],
                tz=s["tz"],
                now=now,
            )
            etag = await self._put(pool, owner, "calendar_href", s["caldav_uid"], text)
            await store.save_etag(pool, s["id"], etag)
            return
        cals = await store.calendars(pool, owner)
        if cals is not None:
            await self.dav.delete(cals["calendar_href"], s["caldav_uid"])
        if s["cancelled_by"] == "nextcloud":
            text = ics.todo(
                uid=f"{s['caldav_uid']}-cancelled",
                summary=f"{s['title']}: cancelled in Nextcloud",
                description="The event was deleted in Nextcloud, so Uriel stopped this schedule.",
                completed=now,
                now=now,
            )
            await self._put(pool, owner, "tasks_href", f"{s['caldav_uid']}-cancelled", text)

    async def _push_run(self, pool: AsyncConnectionPool, r: dict) -> None:
        uid = f"{r['caldav_uid']}-run-{r['id']}"
        text = ics.todo(
            uid=uid,
            summary=f"{r['title']}: {r['status']}",
            description=r["summary"] or r["error"] or (SKIPPED if r["status"] == "skipped" else ""),
            completed=r["finished_at"],
            now=self._now(),
        )
        await self._put(pool, r["owner"], "tasks_href", uid, text)

    async def _push_each(self, pool: AsyncConnectionPool, rows: list[dict], push, mark) -> None:
        for row in rows:
            owner = row["owner"]
            if self._waiting(owner):
                continue
            try:
                await push(pool, row)
            except WebDavError as e:
                self._failed(owner, e)
                continue
            self._succeeded(owner)
            await mark(pool, row["id"], row["status"])

    async def push_once(self) -> None:
        pool = await self._pool()
        await self._push_each(
            pool, await store.dirty_schedules(pool), self._push_schedule, store.schedule_mirrored
        )
        await self._push_each(pool, await store.dirty_runs(pool), self._push_run, store.run_mirrored)

    async def _put_back_edits(self, pool: AsyncConnectionPool, owner: str, changed: dict) -> None:
        for uid, etag in changed.items():
            if await store.edited_in_nextcloud(pool, owner, uid, etag):
                log.info("%s edited the event %s in Nextcloud: Uriel's version goes back", owner, uid)

    async def pull_once(self) -> None:
        pool = await self._pool()
        for cals in await store.all_calendars(pool):
            owner, href, token = cals["owner"], cals["calendar_href"], cals["sync_token"]
            try:
                result = await self.dav.sync(href, token)
            except InvalidSyncToken:
                # Start over with a fresh token. A full listing says nothing about deletions.
                try:
                    fresh = await self.dav.sync(href, None)
                except WebDavError as e:
                    log.warning("calendar resync for %s failed: %s", owner, e)
                    continue
                await self._put_back_edits(pool, owner, fresh.changed)
                if fresh.token:
                    await store.set_sync_token(pool, owner, fresh.token)
                continue
            except CalendarMissing:
                log.warning("%s's Uriel calendar is gone; it is made again on the next push", owner)
                await store.forget_calendars(pool, owner)
                continue
            except WebDavError as e:
                log.warning("calendar pull for %s failed: %s", owner, e)
                continue
            if token is not None:  # the first sync lists what exists; only later ones report deletions
                for uid in result.deleted:
                    title = await store.cancel_from_nextcloud(pool, owner, uid)
                    if title is not None:
                        log.info("%s deleted the event for %r in Nextcloud: cancelled", owner, title)
            await self._put_back_edits(pool, owner, result.changed)
            if result.token:
                await store.set_sync_token(pool, owner, result.token)

    async def run(self, stop: asyncio.Event, push_s: float, pull_s: float) -> None:
        next_pull = 0.0
        while not stop.is_set():
            try:
                # Pull first: a push after a person's delete would put the event back before it's seen.
                if self._clock() >= next_pull:
                    await self.pull_once()
                    next_pull = self._clock() + pull_s
                await self.push_once()
            except Exception:  # the mirror never takes the server down; the next tick tries again
                log.exception("calendar mirror pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), push_s)
