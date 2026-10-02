# Schedules, a Nextcloud calendar mirror, and web search

Status: implemented in 0.8.0 (2026-09-29). Target release: 0.8.0, on top of 0.7.0's tool categories. The gateway side (Talk
channel, run executor) is in uriel-agent
[2026-09-29-coworker-talk-scheduler-design.md](https://github.com/TonyHeadband/uriel-agent/blob/main/docs/specs/2026-09-29-coworker-talk-scheduler-design.md),
which also holds the overall picture and roadmap.

## Why
People want Uriel to do recurring work for them: "every day at 9, give me a digest of news about X". Uriel has no
schedules, no way to know what it has been asked to do, and no web search. The family should also be able to *see*
Uriel's work where they already look: a calendar and a task list in Nextcloud.

## Decisions
- **Uriel's work, not the person's reminders.** Schedules are tasks Uriel runs. "Remind me…" and appointments go to
  the person's own Nextcloud calendar ([personal calendars](2026-09-29-personal-calendars-design.md), which replaced
  the Google track), and are not schedules.
- **The DB is the source of truth; Nextcloud is a mirror.** Every schedule shows up as a recurring event, and every
  run as a completed task. Deleting the event in Nextcloud cancels the schedule. Edits there are ignored and
  overwritten: changes go through chat, where Uriel confirms them.
- **Draft, then confirm.** Like `draft_issue` / `file_issue`: nothing is scheduled that the person didn't see.
- **Structured fields, never a raw RRULE from the model.** The tool builds the rule. A small model (qwen3:8b)
  shouldn't write RFC 5545.
- **UTC inside, local time at the edges.** Every stored and transmitted instant is a UTC `timestamptz`. A schedule
  also keeps its owner's IANA timezone, so "9:00 daily" stays 9:00 across DST. Tool output shows local times.
- **Per person.** Every schedule has an owner (`_meta.user`); nobody can see or cancel someone else's.

## Storage
Migration `0005_schedules.sql`:

```sql
CREATE TABLE person_settings (
    owner      text PRIMARY KEY,
    tz         text NOT NULL DEFAULT 'America/Toronto',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE schedules (
    id           bigserial PRIMARY KEY,
    owner        text NOT NULL,
    owner_sub    text,
    title        text NOT NULL,
    prompt       text NOT NULL,
    rrule        text NOT NULL,          -- RFC 5545, without DTSTART
    dtstart      timestamp NOT NULL,     -- wall-clock, in tz
    tz           text NOT NULL,
    tools        text[] NOT NULL,
    status       text NOT NULL DEFAULT 'active',   -- active | cancelled | finished
    next_run_at  timestamptz,            -- UTC; null once finished or cancelled
    cancelled_by text,                   -- chat | nextcloud
    caldav_uid   text UNIQUE NOT NULL,
    caldav_etag  text,                   -- ETag of Uriel's last PUT of the event
    mirror_dirty boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX schedules_due ON schedules (next_run_at) WHERE status = 'active';

CREATE TABLE schedule_runs (
    id              bigserial PRIMARY KEY,
    schedule_id     bigint NOT NULL REFERENCES schedules(id),
    due_at          timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'claimed',  -- claimed | ok | failed | undelivered | skipped
    lease_until     timestamptz,
    summary         text,
    error           text,
    talk_message_id bigint,
    finished_at     timestamptz,
    mirror_dirty    boolean NOT NULL DEFAULT false,
    UNIQUE (schedule_id, due_at)
);

CREATE TABLE caldav_sync (
    owner          text PRIMARY KEY,
    calendar_href  text NOT NULL,
    tasks_href     text NOT NULL,
    sync_token     text
);
```

`UNIQUE (schedule_id, due_at)` makes claiming idempotent: one occurrence is run at most once.

## Recurrence
`schedules/rules.py` builds an RRULE from structured fields and computes occurrences with `dateutil.rrule` in the
schedule's `tz`, converting each to UTC.

| `frequency` | Needs | RRULE |
|---|---|---|
| `once` | `date`, `time` | none (`COUNT=1`) |
| `daily` | `time` | `FREQ=DAILY` |
| `weekdays` | `time` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR` |
| `weekly` | `time`, `weekdays` | `FREQ=WEEKLY;BYDAY=…` |
| `monthly` | `time`, `day_of_month` | `FREQ=MONTHLY;BYMONTHDAY=…` |

A wall-clock time that doesn't exist (spring-forward gap) runs at the first valid minute after it. One that happens
twice (fall-back) runs once, at the first occurrence.

## Tools
Registered in `tools.py` `register_tools`, gated to `family` in `config/tools.yaml` with category `schedules`.
Every tool acts on `_meta.user`'s schedules only and fails with "schedules need a signed-in person" without it.

- **`draft_schedule(title, prompt, frequency, time, weekdays=None, date=None, day_of_month=None, tools=None)`**
  - `tools` defaults to `["web_search"]`. Every name must exist, be marked `unattended` and be allowed to the
    caller's groups (a run acts for them, so the gateway would refuse anything else every time); otherwise the tool
    says which ones can't run unattended, or aren't available to them.
  - Returns `{draft_id, title, prompt, when, next_runs, tools, tz, next}`. `when` is plain language in the person's
    timezone ("every weekday at 9:00 (Montreal time)"), `next_runs` the next 3 local times. `next` tells the model
    to show this and wait for a yes.
  - Drafts use the in-memory owner-bound store from `issues.py` (`Drafts`, 24 h), generalised into a small shared
    module.
- **`create_schedule(draft_id)`:** creates exactly the stored draft. Owner-bound and idempotent: a second call
  returns the same schedule, with its `status`, while it is active. Once it was cancelled or has finished, a second
  call refuses ("that schedule was cancelled: call draft_schedule again…") rather than report it created. Marks it
  `mirror_dirty`.
- **`list_schedules()`:** active schedules, each with `when`, the next run and the last run's status and time (local).
  This is how Uriel answers "what are you doing for me?".
- **`list_runs(schedule_id=None, since=None)`:** recent runs with status and `summary`, newest first, up to 20.
- **`cancel_schedule(schedule_id)`:** `status='cancelled'`, `cancelled_by='chat'`, `mirror_dirty`.
- **`set_timezone(tz)`:** validates an IANA name and saves it in `person_settings`. Existing schedules keep their
  own `tz`; the tool says so and offers to redraft them.

### For the gateway only
Marked `hidden: true` in `config/tools.yaml` (category `schedules`) and gated to the internal group
`uriel-internal`, which the gateway sets only on its own runner calls and strips from every human principal.

- **`claim_due_runs(limit=5)`:**
  1. Finds active schedules with `next_run_at <= now()`.
  2. Skips missed occurrences older than the grace period (10 min for `once`; otherwise a quarter of the interval,
     capped at 2 h), recording them as `skipped` (marked `mirror_dirty`, so each shows as a done-task
     "<title>: skipped" instead of silently vanishing), and keeps only the latest due one.
  3. Inserts a `claimed` run with `lease_until = now() + 10 min` (`FOR UPDATE SKIP LOCKED`, as in `rag/jobs.py`).
  4. Advances `next_run_at` to the next occurrence, or marks the schedule `finished`.
  5. Runs are at most once. A run whose lease expired without a `finish_run` is never returned again: it's marked
     `failed` with the error "lease expired (gateway lost the run)", and mirrored like any other failed run.
     Handing it out again could post the same answer to Talk twice when only the `finish_run` call was lost.

  Returns `[{run_id, owner, owner_sub, title, prompt, tools, due_at, tz}]`.
- **`finish_run(run_id, status, summary=None, error=None, talk_message_id=None)`:** records the outcome and marks the
  run `mirror_dirty`.

### `unattended`
`ToolRule` gains `hidden: bool = False` and `unattended: bool = False`, next to 0.7.0's required `category`.
`GroupGate` sends all three in each listed tool's `_meta.uriel` (`{category, hidden, unattended}`), so `tools/list`
shows them to the gateway. `memory_context` is marked `hidden: true` too.

The initial set is `web_search`, `search_documents`, `show_memory` and `list_schedules`. Tools that change something
or need a confirmation (`file_issue`, `sign_document`, `confirm_edit`, `remember`…) stay `false`.

## Calendar mirror
`schedules/mirror.py`, a background task in the uriel-tools server process. It reuses the Nextcloud credentials and
HTTP client from `rag/webdav.py`. CalDAV is reached at `{nextcloud_url}/remote.php/dav/calendars/uriel/`.

As of 2026-09-29, `uriel`'s calendars report `cs:can-be-shared`, so a non-admin user can share them.

### Per person
On an owner's first schedule, the mirror:
1. Runs `MKCALENDAR` for `uriel-<owner>` ("Uriel · <display name>", VEVENT) and `uriel-<owner>-done`
   ("Uriel · done", VTODO). `<owner>` is made URL-safe (lowercase, `-` for anything else); when that changes it,
   6 hex digits of the owner's SHA-256 are appended, so `anna.b` and `anna_b` never land on the same calendar
   (`MKCALENDAR` answers 405 for one that exists, which reads as "already made").
2. Shares both read-write with the owner's Nextcloud account (`CS:share`). Nextcloud usernames equal lldap
   usernames once Nextcloud logs in through `user_oidc` (see the uriel-agent spec), so that's `owner`.
3. Stores the hrefs in `caldav_sync`.

Read-write is required: deleting from a read-only share isn't possible.

### Push (every 30 s, rows with `mirror_dirty`)
- **Active schedule:** `PUT` a VEVENT with the fields below. Uriel always overwrites (no `If-Match`), and keeps
  the `ETag` the `PUT` returns in `schedules.caldav_etag`.
  - `UID = caldav_uid`, `SUMMARY = title`, `DTSTART;TZID=tz`, `RRULE`, a 15-minute `DURATION`.
  - `DESCRIPTION` holds the prompt and tools.
  - `X-URIEL-SCHEDULE-ID`.
- **Cancelled or finished schedule:** `DELETE` the event (a 404 is fine).
- **Finished run:** `PUT` a VTODO with `STATUS:COMPLETED`, `COMPLETED` at the run's finish time, and `SUMMARY` set to
  "<title>: ok", "… failed", "… skipped" and so on. `DESCRIPTION` holds the summary or error.
- **Cancellation from Nextcloud:** logged the same way, as a done-task "<title>: cancelled in Nextcloud".
- A failed push leaves `mirror_dirty` set and is retried with backoff. Runs never wait on the mirror.

### Pull (every 60 s, per owner)
1. `REPORT sync-collection` with the stored `sync_token`.
2. A response entry with status 404 is a **deletion**. If its href's UID belongs to an active schedule, the schedule
   is cancelled with `cancelled_by='nextcloud'`.
3. A changed entry (any status but 404) whose UID belongs to an active schedule and whose `getetag` differs from
   `caldav_etag` was edited in Nextcloud: the schedule is marked `mirror_dirty`, so the next push puts Uriel's
   version back. An entry carrying the ETag of Uriel's own `PUT` is its own write coming back, and is ignored, so
   nothing loops. Without both ETags nothing is marked. Events the person added themselves are left alone.
4. Store the new `sync_token`.

**Deletes come only from explicit sync-collection 404 entries, never from an item missing in a listing.** A failed
request, an empty response, an invalid token (which triggers a full resync with a fresh token) or a missing calendar
never cancels anything.

If a calendar is missing (for example the person deleted the whole calendar), it's recreated on the next push. Its
schedules stay active.

## Web search
`search.py`: **`web_search(query, time_range=None, max_results=8)`**.
- Calls SearXNG `GET {searxng_url}/search?q=…&format=json[&time_range=day|week|month|year]`. The JSON format is
  enabled (checked 2026-09-29 on `searxng.example.com`).
- Returns `{results: [{title, url, snippet, engine, published}], query, next}`, deduplicated by URL. `max_results`
  is capped at 20. `next`, as in `draft_issue`, tells the model to answer from these results only and end with the
  URL of every result it used (evals showed a model skipping citations with only the docstring asking).
- Errors and timeouts (10 s) raise `ToolError("web search is unavailable right now")`, which carries the gateway's
  report hint.
- Gated to `family`, category `web`, `unattended: true`.
- No page fetching in 0.8.0: fetching arbitrary URLs from inside the private network needs an allowlist and
  private-address blocking, designed separately.
- Setting: `URIEL_SEARXNG_URL` (in-cluster service URL; compose uses the public host).

## Configuration
New settings: `URIEL_SEARXNG_URL`, `URIEL_MIRROR_ENABLED` (default true when Nextcloud is configured),
`URIEL_MIRROR_PUSH_SECONDS`, `URIEL_MIRROR_PULL_SECONDS`, `URIEL_DEFAULT_TZ` (default `America/Toronto`).

## Contract changes (`docs/contract.md`)
- The schedule tools and `web_search`.
- The hidden gateway tools and the `uriel-internal` group.
- `_meta.uriel.unattended` and `_meta.uriel.hidden` in `tools/list`, next to `_meta.uriel.category`.
- Times: every timestamp in tool I/O is UTC ISO 8601 with `Z`, alongside a `…_local` string for display.

## Testing
- **`test_rules.py`:**
  - Every frequency, and the next runs across DST in America/Toronto (the gap and the overlap).
  - UTC storage.
  - `once` in the past is rejected.
- **`test_schedules.py`** (db):
  - Draft→create is owner-bound and idempotent; a stranger can't see or cancel another person's schedule.
  - `claim_due_runs`: claims only due runs, one per occurrence under concurrency (two claimers), skips missed runs,
    never returns an expired lease again (it becomes `failed`), marks `once` finished.
  - `finish_run`.
  - `unattended` validation in `draft_schedule`.
- **`test_mirror.py`** (fake CalDAV in `tests/fakes/`):
  - First schedule creates and shares both calendars.
  - Push creates, updates and deletes.
  - A run becomes a VTODO.
  - A sync-collection 404 cancels. An edit is put back on the next push; Uriel's own write coming back is not.
  - **A failed, empty or invalid-token pull cancels nothing.**
  - A missing calendar is recreated.
- **`test_search.py`** (fake SearXNG): mapping, dedupe, cap, `time_range`, timeout → `ToolError`.
- **`test_contract.py`:** hidden tools, `_meta.uriel.hidden` and `_meta.uriel.unattended`, gating to
  `uriel-internal`.
- **live** (`live` marker): create, share, pull a deletion against the real Nextcloud with a test user. One real
  SearXNG query.
- **Evals** (uriel-agent `evals/scenarios/`), new areas:
  - `schedule`: draft shown before create; no create without a yes; a "no, make it 8" leads to a new draft; list
    ("what are you doing for me?"); cancel the right one; a timezone question answered in local time.
  - `search`: news questions call `web_search` and cite URLs; no invented results.

## Out of scope
- Page fetching.
- Editing a schedule in place (cancel and redraft instead).
- Shared or family-wide schedules.
- The person's own reminders and appointments (Google track).
- The RAG worker's `next_daily` still defaults to Europe/Rome; that's a separate follow-up.
