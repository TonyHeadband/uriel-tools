# Wire contract with uriel-agent

uriel-tools is the MCP server that the Uriel gateway (repo `uriel-agent`) calls for every tool. The two repos share
no code. This page is the whole interface between them. A change that breaks it bumps the major version of
uriel-tools, and uriel-agent pins the image tag it was tested against.

## Transport
- MCP streamable HTTP at `POST /mcp`. The container listens on port 8001, set by `URIEL_MCP_LISTEN_PORT`.
- Every HTTP request carries `x-api-key: <key>`. A missing or unknown key gets `401 {"error": "unauthorized"}`
  before MCP sees the request. `URIEL_MCP_API_KEYS` is comma-separated, so a key can be rotated without downtime.

## Caller identity
The gateway authenticates the human (Authelia OIDC) and forwards who they are as the MCP request `_meta` on
`tools/list` and `tools/call`:

| key          | type          | meaning                                              |
|--------------|---------------|------------------------------------------------------|
| `user`       | string        | the caller's user id (`preferred_username`)          |
| `groups`     | list[string]  | the caller's groups, e.g. `["admins", "family"]`     |
| `request_id` | string        | the gateway's request id, echoed in the audit log    |
| `sub`        | string, optional | the caller's OIDC subject (since 0.3.0); matches Nextcloud accounts that sociallogin named `authelia-<sub>` |

The API key is what makes `_meta` trustworthy, and the cluster NetworkPolicy keeps any other pod from reaching the
server. A missing `_meta` means no groups, so every tool is denied.

## Gating
`config/tools.yaml` (`URIEL_MCP_TOOLS_FILE`) maps each tool to the groups that may use it. A tool that isn't
listed is denied.
- `tools/list` returns only the tools the caller's groups allow. Since 0.7.0 each listed tool carries
  `_meta.uriel.category` (from `tools.yaml`; required), which the agent uses to narrow the model's tools.
- Since 0.10.0 each listed tool also carries `_meta.uriel.companion_action` (from `tools.yaml`): what a call
  looks like from outside, for the desktop companion's character. `lookup` reads, `change` writes right away,
  `draft` prepares a change that waits for a yes, `confirm` applies one, `ask` puts a question to the person. It is
  `null` only on hidden tools. It describes the tool, not one call: `add_event` is `change` but returns a
  `draft_id` on a family calendar, so whether a call waits for a yes comes from its result.
- A denied `tools/call` returns an MCP tool error (`isError: true`) with the text
  `<tool> is not permitted for <user>`, which the agent can relay.

Every call writes a JSON line to the `uriel.mcp.audit` logger with `request_id`, `user`, `tool`, `allowed` and
`duration_ms`.

## Events intake (since 0.2.0)
A second process, `events` (port 8002, `URIEL_EVENTS_LISTEN_PORT`), receives webhooks from other apps:
`POST /events/{source}` with `X-Uriel-Source-Key: <URIEL_EVENTS_KEY_<SOURCE>>`. Responses: `202 {"queued": n}`
(including events outside any opted-in folder, which are dropped unstored), `401` bad key, `404` unknown source,
`413` body over 1 MB, `422` unreadable payload, `503` database down (the nightly reconcile catches up).
Sources today: `nextcloud` (webhook_listeners node events; register them with
`scripts/register-nextcloud-webhooks.sh`). Only the cluster's `cloud` namespace may reach this port.

## Document tools (since 0.2.0)
`search_documents(query, k=5, folder=None)` and `ocr_now(path)` filter inside the query by `_meta.user`,
`_meta.sub` and `_meta.groups`. Since 0.3.0 a caller sees folders they shared with the `uriel` Nextcloud account,
and folders shared with a `uriel-<group>` group for one of their groups. A hit from someone else's folder carries
`shared_by`. `ocr_now` is for the folder's owners only. See [guide.md](guide.md).

## Editing tools (since 0.4.0)
`inspect_document(path)`, `fill_form(path, values)`, `edit_text(path, changes, notes)`,
`preview_signature(path, where=None)`, `place_signature(draft_id)` and `confirm_edit(path)` apply the same visibility rule as
search. They never modify a file: results are saved as `edit_<name>` next to the source, only in folders whose share
allows creating files. A refusal is an MCP tool error whose text says why and what to do. Results:
`{saved, url, done, not_found, approximate, warnings, note}`. Search hits from these copies carry `edit_of` and
`edit_status` (`draft`, `changed`, `confirmed`). See [the design](specs/2026-09-28-document-editing-design.md).

Since 0.11.0 signing is two tools instead of `sign_document(path, where, confirmed)`, so a signature can't be
placed without a preview, whatever the model does. `preview_signature` saves nothing and returns
`{draft_id, would_place, next}`; `place_signature(draft_id)` places exactly that preview, for the person who
previewed it, and returns the editing result above (the same one again on a repeated call). Previews are kept in
memory like the other drafts: a restart loses them, and an unknown or expired one is the tool error
`no such preview (it may have expired): …`.

## Memory tools (since 0.6.0)
Per-person notes, see [the design](specs/2026-09-28-personal-memory-design.md). Each tool acts only on the notes of
`_meta.user`, and fails with "memory needs a signed-in person" without it.
- For the model: `remember(file, section, note, replace=false)`, `forget(file, text)`, `show_memory(file)`,
  `next_question(file, skip=false)`, where `file` is `"user"` or `"soul"`.
- **`memory_context()`** returns `{"user": markdown, "soul": markdown, "tz": "America/Toronto", "now_local": "Tue 29
  Sep 2026, 22:41"}`: the caller's USER.md and SOUL.md (`""` when empty), their timezone and the time there now
  (since 0.8.2), so the model knows today's date. The gateway calls it once per turn for the system prompt and does **not** offer it to the model. When it
  isn't listed for the caller (no `family` group), the gateway goes on without memory.

## Schedules and web search (since 0.8.0)
Work Uriel runs for a person on a schedule, and web search. See
[the design](specs/2026-09-29-schedules-web-search-design.md).

**Times.** Every timestamp in tool input and output is UTC ISO 8601 with `Z` (`2026-09-30T13:00:00Z`). Output puts
a `<name>_local` string beside each one, in the schedule's timezone, for people (`Wed 30 Sep 2026, 9:00`). The
one exception is `draft_schedule`'s `next_runs`, below.

**For the model** (`family`). Each tool acts only on `_meta.user`'s schedules and fails with "schedules need a
signed-in person" without it:
- **`draft_schedule(title, prompt, frequency, time, weekdays=None, date=None, day_of_month=None, tools=None)`**:
  - `frequency` is `once` (needs `date`, YYYY-MM-DD), `daily`, `weekdays`, `weekly` (needs `weekdays`, e.g.
    `["mon", "thu"]`) or `monthly` (needs `day_of_month`).
  - `time` is `HH:MM` in the person's timezone.
  - `tools` defaults to `["web_search"]`. Each one must be `unattended` and allowed to the caller's groups;
    otherwise the tool error names the ones that aren't (`can't run unattended: …` or
    `not available to you: …`) and lists the ones that are.
  - Returns `{draft_id, title, prompt, when, next_runs: [at_local], tools, tz, next}` and schedules nothing.
    `next_runs` is local time only (since 0.8.2), the one exception to UTC output: it is for the person to
    read, and given a UTC stamp beside it the model reported an evening run as the next day.
- **`create_schedule(draft_id)`**: creates exactly that draft, for its owner only.
  - Returns `{created, schedule_id, title, prompt, when, tools, tz, status, next_run_at, next_run_at_local}`;
    `status` is `active`.
  - Calling it again returns the same schedule while it is active. Once that schedule was cancelled or has
    finished, it fails with `that schedule was cancelled: call draft_schedule again to make a new one` (or
    `finished`).
- **`list_schedules()`**: `{schedules: [...]}`, the active ones, shaped like `create_schedule`'s result without
  `created`, each with a `last_run` (`{status, due_at, due_at_local}` or `null`).
- **`list_runs(schedule_id=None, since=None)`**: `{runs: [...]}`, newest first, at most 20.
  - Each run is `{run_id, schedule_id, title, status, due_at, due_at_local, finished_at, finished_at_local, summary, error}`.
- **`cancel_schedule(schedule_id)`**: `{schedule_id, title, status}`.
- **`set_timezone(tz)`**: `{tz, now_local, note, unchanged_schedules?}`. `tz` is an IANA name, matched without
  regard to case. Existing schedules keep their own timezone.
- **`web_search(query, time_range=None, max_results=8)`**: `{results: [{title, url, snippet, engine, published}], query, next}`.
  - `next` tells the model to answer from these results only and end with the URL of every result it used.
  - `time_range` is `day`, `week`, `month` or `year`; results are deduplicated by URL, at most 20.
  - Any failure is the tool error `web search is unavailable right now`.

**Tool meta in `tools/list`.** Next to 0.7.0's `_meta.uriel.category`, every tool carries two booleans from
`config/tools.yaml`:
- `_meta.uriel.unattended`: true means a scheduled run may use the tool. Today that is `web_search`,
  `search_documents`, `show_memory`, `list_schedules` and `calendar_agenda`.
- `_meta.uriel.hidden`: true means the tool is for the gateway itself and is never offered to the model. Today
  that is `memory_context`, `claim_due_runs` and `finish_run`.

**For the gateway's runner** (hidden, gated to the internal group `uriel-internal`). The gateway sends that group
only on its own runner calls (`_meta = {"user": "uriel-gateway", "groups": ["uriel-internal"], "request_id": ...}`)
and strips it from every person's groups.
- **`claim_due_runs(limit=5)`**: `{runs: [{run_id, owner, owner_sub, title, prompt, tools, due_at, due_at_local, tz}]}`.
  - `owner` is the person's `_meta.user` (and Nextcloud uid); `owner_sub` is their OIDC subject, or `null`.
  - Each occurrence is handed out once, with a 10-minute lease. `limit` (1-20) caps new claims.
  - Runs are at most once. A run whose lease expires without `finish_run` is never handed out again: it becomes
    `failed` with the error `lease expired (gateway lost the run)`, and is mirrored like any other failed run.
  - An occurrence missed by more than its grace is recorded as `skipped`, never handed out, and mirrored as a
    done-task "<title>: skipped". The grace is 10 min for a one-off, otherwise a quarter of the interval, at
    most 2 h.
- **`finish_run(run_id, status, summary=None, error=None, talk_message_id=None)`**: `status` is `ok`, `failed` or
  `undelivered`.
  - Returns `{run_id, status, finished}`. `finished` is false when the run was already finished: a retry
    changes nothing.
  - `summary` and `error` are kept up to 1000 characters.

**Calendar mirror.** For each person with a schedule, the `uriel` Nextcloud account keeps two calendars, both
shared read-write with them:
- `uriel-<owner>`: an event per active schedule;
- `uriel-<owner>-done`: a completed task per finished run, skipped runs included.

`<owner>` is the username lowercased, with anything but `a-z`, `0-9` and `-` turned into `-`; when that changes
it, 6 hex digits of the username's SHA-256 are appended (`anna.b` → `anna-b-<hash>`), so no two people share one.

Deleting a schedule's event in Nextcloud cancels it. Edits there are overwritten: the next pull sees the event's
ETag differ from the one Uriel's own write got, and the next push puts Uriel's version back.

## People's calendars (since 0.9.0)
Reminders and events in people's own Nextcloud calendars. See
[the design](specs/2026-09-29-personal-calendars-design.md). All seven tools are in category `calendar`, for
`family`; each acts for `_meta.user` and fails with "calendars need a signed-in person" without it. Without
Nextcloud credentials they fail with `calendars are not configured`. A Nextcloud or database failure is the tool
error `calendar is unavailable right now`.

**Times.** Input is local: `date` is `YYYY-MM-DD`, times are `HH:MM` (24-hour), in the person's timezone (the one
`set_timezone` keeps). Output is local time only, with no UTC stamp: the model misreports the day given both
(0.8.2's lesson). It reads like `Thu 1 Oct 2026, 15:00`, `Fri 2 Oct 2026, 19:00–20:00`, `due Thu 1 Oct 2026, 15:00`
or `Fri 2 Oct 2026, all day`.

**Which calendars.** A person sees the calendars they own and have shared with the Nextcloud user `uriel`, plus any
calendar Uriel owns and has shared with the group `<g>` for an Authelia group `<g>` they are in (Uriel's "Family"
calendar, shared with `family`). Anything else is invisible: an item elsewhere is "no such item". `calendar` is `"mine"` (their own,
the one `set_default_calendar` saved, or their only one holding that kind of item) or a calendar's name, matched
without regard to case. A calendar shared read-only is refused with a message asking for "can edit".

**Drafts.** Writes to a family calendar, and every move or delete, return `{draft_id, change, next}` and change
nothing: `change` is plain language for the person and `next` tells the model to ask. `confirm_calendar_change`
applies exactly that draft, for its owner only; drafts are kept in memory and expire. Writes to the person's own
calendar happen at once. If the item changed in Nextcloud after the draft, confirming fails with
`it changed in Nextcloud since the draft: call draft_calendar_change again and show the new draft`.

**Undo.** Deleting, with `draft_calendar_change`, an item Uriel added to the person's own calendar in the last
15 minutes happens at once and returns `{deleted, title, calendar}` instead of a draft.

- **`calendar_agenda(start=None, end=None, calendar=None)`**: events and open reminders from `start` to `end`
  inclusive, default today and tomorrow, at most 31 days. `calendar` limits it to one by name.
  - Returns `{from, to, tz, items, more?, overdue_more?}`; `from` and `to` read like `Thu 1 Oct 2026`.
  - Each item is `{item_id, kind, title, calendar, when, recurring, location?, overdue?}`. `kind` is `event` or
    `task`; `overdue` is on tasks only. Items are sorted by time. Overdue tasks are capped apart from the rest:
    the 10 most recent, with `overdue_more` counting the older ones; the other items at most 50, with `more`
    counting the rest.
  - With no shared calendar the tool error explains how to share one.
  - `unattended`: a scheduled run may use it.
- **`add_reminder(title, date, time, notes=None, calendar="mine")`**: a task with an alarm at the due time.
  - Times here and in `add_event` and `draft_calendar_change` are `HH:MM`, or `YYYY-MM-DDTHH:MM[:SS]` whose date
    stands in for `date`; a `date` that differs is refused (`the date and the time disagree: pass the date once`).
  - Own calendar: `{added: "reminder", title, calendar, due_local, item_id, note}`.
  - Family calendar: a draft. A time already past is refused (`that time has already passed …`).
- **`add_event(title, date, start=None, end=None, location=None, notes=None, alarm_minutes_before=None, calendar="mine")`**:
  - No `start` and `end` means all-day. `end` defaults to an hour after `start` and stays within the day.
    `alarm_minutes_before` is 0 to 10080.
  - Own calendar: `{added: "event", title, calendar, when, item_id, note}`. Family calendar: a draft.
- **`complete_reminder(item_id)`**: `{completed: true, title, calendar}`; ticking one already done returns
  `{completed: true, already: true, title}`. An event is refused.
- **`draft_calendar_change(item_id, action, date=None, start=None, end=None)`**: `action` is `move` or `delete`.
  - A move needs a new `date` and/or times; a timed event keeps its length unless `end` is given, and a
    repeating item can change its time but not its days. A series with exceptions (EXDATE, RDATE or a changed
    occurrence) is refused: `this series has exceptions: change it in the Calendar app`. Returns a draft.
  - A delete returns a draft, or `{deleted: true, title, calendar}` for an undo.
- **`confirm_calendar_change(draft_id)`**: applies the draft and returns what happened: `{added, title, calendar,
  when, item_id, note}` (`added` is `reminder` or `event`), `{moved: true, title, calendar, change}` or
  `{deleted: true, title, calendar}`. Calling it again returns the same result.
- **`set_default_calendar(calendar)`**: `{default: name}`; only one of the person's own calendars.
