# Personal calendars: reminders and events in Nextcloud

Status: implemented in 0.9.0 (2026-09-29). The gateway side (routing wording, evals) is a
small change in uriel-agent, described under [Gateway](#gateway-uriel-agent).

## Why
The family keeps its life in Nextcloud, not Google (decided 2026-09-29). People want to say "remind me to call the
dentist Thursday at 3" or "Lil-B's recital is Friday at 7" in Talk and find it in their own calendar, with their
phone ringing at the right time. Until now Uriel could only schedule its *own* work (`draft_schedule`), and the
model reached for that when asked for a reminder. This supersedes the Google track (uriel-agent #26-28).

## Decisions
- **The person's own calendar, reached by sharing.** A person shares a calendar with the Nextcloud user `uriel`
  (*can edit*). No share, no access; unsharing cuts Uriel off at once. Uriel uses its one app password
  (`URIEL_NC_APP_PASSWORD`), never the person's credentials.
- **A reminder is a task with an alarm.** `add_reminder` makes a VTODO with `DUE` and a `VALARM` at the due time:
  it shows in Nextcloud Tasks and can be ticked off. Appointments are VEVENTs. If the live check shows Nextcloud
  doesn't notify for task alarms, reminders become VEVENTs with an alarm (see [Live checks](#live-checks)).
- **A family calendar owned by Uriel and shared with a group.** A calendar `uriel` owns and shares with the
  Nextcloud group `<g>` is visible to members of the Authelia group `<g>` (Nextcloud groups mirror Authelia's).
  In practice, a "Family" calendar made in Uriel's own home and shared with the group `family`. Uriel reads its own
  calendars' sharees as owner; a sharee is never shown `oc:invite`, so a person's calendar can't be judged by it.
- **Create directly, confirm changes.** A new reminder or event on the person's own calendar is created at once,
  and the tool result says exactly what and where. Moving or deleting anything, and any write to a group
  calendar, is a draft the person confirms. Deleting an item Uriel created for the same person in the last
  15 minutes is direct, so "no, undo that" takes one turn. Ticking off a reminder is direct on any calendar,
  a group one included.
- **Extend our CalDAV client.** `schedules/caldav.py` gains calendar listing, a time-range query and conditional
  writes. No third-party CalDAV library: one client for the mirror and for these tools.
- **Structured fields, local time.** The model passes `YYYY-MM-DD` and `HH:MM` in the person's timezone (the one
  `set_timezone` keeps); a time sent as `YYYY-MM-DDTHH:MM` brings its date, and one that disagrees with `date`
  is refused. The tool writes `TZID` times. Output is local time only, since it is read by people
  (0.8.2's lesson: given a UTC stamp too, the model misreports the day).
- **Uriel's own calendars stay the mirror.** "Uriel · <name>" and "Uriel · done" are owned by `uriel` and never
  offered by these tools.

## Which calendars a person sees
Every call lists `uriel`'s calendar home (`PROPFIND` depth 1 on `/remote.php/dav/calendars/uriel/`); nothing is
cached, so a share or unshare takes effect on the next call. For each calendar Uriel reads its display name, owner
(`oc:owner-principal`), read-only flag (`oc:read-only`), sharees (`oc:invite`) and component set
(`C:supported-calendar-component-set`).

A calendar is **the caller's** when its owner is the caller (`_meta.user`, which equals the Nextcloud uid); being in
`uriel`'s home means it was shared with `uriel`. It is **a group calendar** when `uriel` owns it, it is shared with
a group `<g>` and the caller has Authelia group `<g>`. Calendars `uriel` owns and shares only with users are the
schedules mirror and are skipped. Anything else is invisible to the caller, even though `uriel` can read it.

`calendar="mine"` resolves to:
- the caller's default calendar, when one is saved and still visible;
- otherwise the only calendar of theirs that holds the needed component (VTODO for reminders, VEVENT for events);
- otherwise a refusal listing their calendars by name: the model asks which one, and the person's answer is saved
  with `set_default_calendar`.

With no calendar of theirs shared, tools refuse with the sharing steps: *Calendar app → ⋯ next to the calendar →
Share → type "uriel" → tick "can edit"*.

`calendar` may also be a calendar's display name (case-insensitive) among those the caller sees, e.g. "Family".

## Tools
Category `calendar`, group `family` (`config/tools.yaml`).

| Tool | What it does | Confirm | `unattended` |
|---|---|---|---|
| `calendar_agenda(start?, end?, calendar?)` | Events and tasks in the caller's visible calendars between two dates (default: today and tomorrow, in their timezone), at most 50, sorted by time. Open tasks that are overdue are included and flagged, at most 10 of them on top of the 50. | no (read) | yes |
| `add_reminder(title, date, time, notes?, calendar="mine")` | A VTODO: `DUE` at that local time, `VALARM` at `DUE`. | direct on a calendar of theirs; a draft on a group calendar | no |
| `add_event(title, date, start, end, location?, notes?, alarm_minutes_before?, calendar="mine")` | A VEVENT. `end` is `HH:MM` on the same date; all-day events pass no `start`/`end`. No alarm unless `alarm_minutes_before` is given. | as above | no |
| `complete_reminder(item_id)` | Marks a task `COMPLETED`. | direct | no |
| `draft_calendar_change(item_id, action, date?, start?, end?)` | `action` is `move` (new date and/or times) or `delete`. Returns what will change and a `draft_id`. A series with exceptions (cancelled or changed occurrences) can't be moved here. The one exception: `delete` of an item Uriel created for the caller in the last 15 minutes happens at once and returns `{deleted: true}` (undo). | it *is* the draft | no |
| `confirm_calendar_change(draft_id)` | Applies a drafted change, or a drafted write to a group calendar. Owner-bound, 24 h, idempotent, like `create_schedule`. | after the yes | no |
| `set_default_calendar(calendar)` | Saves which of the caller's calendars "mine" means. | direct | no |

**`item_id`** is opaque to the model: `<calendar path>/<resource name>`, base64url. Every tool that takes one
decodes it and checks that the calendar is still among the caller's visible ones; an id from another person's
calendar is "no such item". Items are fetched with their ETag.

**Results** always name the calendar and the local time, e.g.
`{"added": "reminder", "title": "Call the dentist", "calendar": "Personal", "due_local": "Thu 1 Oct 2026, 15:00",
"item_id": "…", "note": "Tell the person what you added and where; offer to undo."}`.
A draft result follows `draft_schedule`: `{draft_id, change: "<plain language>", next: "…only after they say yes…"}`.

**Recurring items.** The agenda shows each occurrence (Nextcloud expands them: `C:expand` in the query) with
`recurring: true`. A change to a recurring item applies to the whole series and the draft says so: `move` changes
only the time of day (a new date is refused: "change the days in the Calendar app"), `delete` removes the series.
Single-occurrence changes are out of scope.

**Refusals** (the model can act on each):
- a time already passed: "…(it is now Tue 29 Sep 2026, 19:06 Toronto time)…", as in 0.8.2;
- a read-only share: "give uriel *can edit* on <calendar>";
- a calendar without tasks: "<calendar> doesn't hold tasks: pick another" with the ones that do;
- the item changed in Nextcloud since the draft (ETag mismatch on `If-Match`): "it changed since: draft again";
- Nextcloud unreachable or 5xx: "calendar is unavailable right now". Each write is one `PUT` or `DELETE`, so nothing
  is half-written.

### Routing between reminders and Uriel's work
- `add_reminder`: "Remind the person of something at a time… Not for work you do for them: use draft_schedule."
- `draft_schedule` (changed): "Work you will do for the person on a schedule… Not for reminding them or their
  appointments: use add_reminder or add_event."

## ICS
In `schedules/ics.py`, next to the mirror's builders:
- VTODO: `UID` (uuid4), `SUMMARY`, `DESCRIPTION`, `DUE;TZID=…`, `STATUS:NEEDS-ACTION`, and a `VALARM` with
  `ACTION:DISPLAY` and an absolute `TRIGGER;VALUE=DATE-TIME` in UTC. Completing sets `STATUS:COMPLETED`,
  `COMPLETED` and `PERCENT-COMPLETE:100`.
- VEVENT: `DTSTART`/`DTEND` with `TZID` (or `VALUE=DATE` for all-day), `LOCATION`, optional `VALARM` with a
  relative `TRIGGER:-PT<n>M`.
- Uriel's items carry `X-URIEL-OWNER:<owner>` so they can be told apart; text is escaped as the mirror already does
  (the CR-injection fix).

Edits parse the stored object and change only the fields involved, so properties set in the Calendar app (colour,
attendees, categories) survive a move.

## CalDAV client additions (`schedules/caldav.py`)
- `calendars() -> list[CalendarInfo]`: the depth-1 `PROPFIND` above.
- `query(href, start, end) -> list[Item]`: `REPORT calendar-query` for VEVENT and VTODO in the range, with
  `C:expand`, returning resource name, ETag and ICS.
- `get(href, name) -> Item | None`.
- `put(..., if_match=None)` and `delete(..., if_match=None)`: a `412` becomes `Changed`.

## Storage
Migration `0006_calendars.sql`:

```sql
ALTER TABLE person_settings ADD COLUMN default_calendar text;  -- a calendar path, or NULL

CREATE TABLE calendar_created (
    owner      text NOT NULL,
    item_path  text NOT NULL,          -- calendar path + resource name
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, item_path)
);
```

`calendar_created` only backs the 15-minute direct undo; rows older than a day are deleted on write. Drafts use the
in-memory `Drafts` store, like schedules and issues.

## Gateway (uriel-agent)
- Pin uriel-tools 0.9.0.
- System prompt: replace "To do something for the person on a schedule…" with a sentence that separates the two:
  reminders and appointments go to `add_reminder`/`add_event` (say what was added and where; offer to undo),
  work Uriel does goes to `draft_schedule`; moves and deletes only after the person's yes to a
  `draft_calendar_change`.
- `awaiting_yes` (0.8.2) already keeps a yes to any `draft_id` on the tools route.
- Decider category description for `calendar` when the decision layer's categories land (uriel-agent Task 12).

## Live checks
With the Nextcloud test user `uriel-test` (not in lldap):
1. `uriel-test` shares a calendar with `uriel`; `calendars()` shows it with owner `uriel-test` and it is the
   caller's. A sharee never sees `oc:invite` (Nextcloud returns it empty), which is why group calendars are owned
   by `uriel`. `uriel` makes a calendar, shares it with the group `uriel-family` (only `uriel` is in it), and
   `calendars()` shows owner `uriel` and `groups/uriel-family` in its sharees; it is visible to a member of that
   group and to nobody else.
2. Add, list (with an expanded recurring event), move with `If-Match`, delete an event.
3. Add and complete a task.
4. **Task alarm notification:** a task due two minutes ahead produces a Nextcloud notification for `uriel-test`.
   If not, the fallback in [Decisions](#decisions) applies before release.

## Testing
- **`test_caldav.py`:** the `PROPFIND` and `REPORT` bodies, parsing owner, read-only, sharees, components; `412`.
- **`test_calendars.py`** (fake CalDAV in `tests/fakes/caldav.py`, extended):
  - visibility: own shared, group-shared to a member, invisible to a non-member and to another person, `uriel`'s
    own skipped;
  - "mine" resolution: saved default, single candidate, several → refusal listing names, none → sharing steps;
  - `add_reminder` and `add_event` write the right ICS (TZID, alarm, DST day); past times refused with "it is now";
  - group calendar writes are drafts; `confirm_calendar_change` is owner-bound and idempotent;
  - move keeps foreign properties, sends `If-Match`, and a changed item is refused;
  - undo within 15 minutes is direct, after that it is a draft;
  - recurring: move changes the time only, a date move is refused, delete removes the series;
  - an `item_id` from someone else's calendar is "no such item".
- **`test_contract.py`:** the new tools, category `calendar`, `calendar_agenda` `unattended`.
- **live** (`live` marker): [Live checks](#live-checks) 1-3; check 4 is manual.
- **Evals** (uriel-agent, tag `calendar`): "remind me…" calls `add_reminder`, never `draft_schedule`; "what's on
  tomorrow?" calls `calendar_agenda` and answers in local time; a move is drafted and done only after yes; "undo
  that" deletes; a family event asks or drafts; a work request ("every morning send me the news") still goes to
  `draft_schedule`.

## Rollout
1. Release uriel-tools 0.9.0; uriel-agent pins it.
2. Uriel's "Family" calendar is made in its own home and shared with the Nextcloud group `family` (*can edit*);
   the user shares their Personal calendar with `uriel`. Others do the same when they start using it.
3. Close uriel-agent #26-28 as superseded, pointing here.

## Out of scope
- Changing one occurrence of a recurring item.
- Inviting people (attendees, `iTIP`), free/busy.
- Gmail and Google Calendar into Nextcloud: a separate round, mostly Nextcloud Mail and calendar import.
- Reading events out of email.
