# Personal memory: `USER.md`, `SOUL.md` and a questionnaire

Status: implemented in 0.6.0 (2026-09-28). Target release: 0.6.0. The gateway side lands in uriel-agent (see
[uriel-agent](#uriel-agent)).

## Why
Uriel treats every family member the same: it doesn't know what they like to be called or when their birthday is,
and it can't be told "keep it short with me". Each person should get:
- **`USER.md`:** what Uriel knows about them (preferred name, birthday, music, hobbies…);
- **`SOUL.md`:** how Uriel should talk to them (warm or matter-of-fact, short or detailed, language, humour…).

Both are built through chat, in two ways: Uriel saves a fact or wish as soon as the person states it, and a guided
questionnaire asks the questions one at a time.

## Decisions
- **Per person.** `owner` is the caller's `_meta.user`, and nobody can read or change another person's notes. The
  family-wide persona stays in the gateway's `SYSTEM_PROMPT`; there is no shared SOUL.
- **Tools DB only.** The "files" are not stored anywhere as files. Each is that person's notes rendered as
  markdown on demand. There are no Nextcloud files, so no WebDAV on every turn and no hand edits to reconcile.
- **Line items, not rewrites.** The model adds or removes one note at a time. It never rewrites a whole file, so
  a small model (qwen3:8b) can't drop existing facts by accident.
- **Save and say so.** There is no confirmation round trip. Saying "forget that" undoes a note.
- **Only what the person states.** Uriel doesn't infer memories from things mentioned in passing ("my
  sister's birthday party" is not the person's birthday).

## Storage
Migration `0004_memory.sql`:

```sql
CREATE TABLE memory_notes (
  id bigserial PRIMARY KEY,
  owner text NOT NULL,                              -- _meta.user
  file text NOT NULL CHECK (file IN ('user', 'soul')),
  section text NOT NULL,                            -- "Name", "Music", "Tone"...
  note text,                                        -- NULL on a skip row
  skipped boolean NOT NULL DEFAULT false,           -- the questionnaire question for this section was skipped
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (skipped = (note IS NULL))
);
CREATE INDEX memory_notes_owner ON memory_notes (owner, file);
```

**Sections:**
- Sections are stored trimmed, with the first letter upper-cased.
- They are compared case-insensitively, so "music" and "Music" are one section.

**Limits**, checked before any write. Anything over is a ToolError that tells the model what to do: "notes are at
most 200 characters: save the gist", or "USER.md is full (20 notes): ask the person what to forget".
- A section name is at most 40 characters.
- A note is at most 200 characters.
- A file holds at most 20 notes. Skip rows don't count. (First planned as 40: with qwen3:8b capped at an 8192-token
  context, 2 × 20 notes, about 1,600 tokens, leaves room for the tools, the chat and search results.)

## Rendering
`render(owner, file)` produces markdown:
- The title is `# About <name>` for the user file and `# How to talk to <name>` for the soul file.
  - `<name>` is the first note of the user file's `Name` section, or else `owner`.
- Sections come in questionnaire order, then any other sections in the order they were first created.
- Each section is `## Section` followed by one `- note` per note, oldest first.
- Skip rows are never rendered. An empty file renders as `""`.

## Tools
All tools are gated to `[family]` in `config/tools.yaml`. They need `_meta.user`, and without it they raise a
ToolError ("memory needs a signed-in person"). The `file` parameter is `"user"` or `"soul"`.
- **`remember(file, section, note, replace=false)`** saves one note.
  - If the section already has this note (compared case-insensitively), nothing changes and the result is
    `unchanged`.
  - `replace=true` first deletes the section's notes and any skip row. Use it for facts that change rather than
    accumulate: name, birthday, tone, length.
  - It returns `{status: saved | unchanged, file, section}`.
  - The docstring:
    - lists the usual sections (User: Name, Birthday, Music, Hobbies, Food, Family, Work; Soul: Tone, Length,
      Language, Humour, Avoid);
    - says "save only what the person said about themselves or asked of you, in their own words, shortened".
- **`forget(file, text)`**:
  - If `text` names a section (case-insensitive), the whole section is deleted, including its skip.
  - Otherwise, the notes equal to `text`, or to the `Section: note` form that candidates are listed in, are found;
    only when none is, the notes containing `text`. All matching is case-insensitive. So "Jazz" stays forgettable
    next to "Jazz fusion".
    - one match: it is deleted;
    - several matches: nothing is deleted, and the candidates are returned so the model can ask which one;
    - none: `not_found`.
  - A section left without notes loses its skip row too, so the questionnaire asks it again.
  - It returns `{status: forgotten | ambiguous | not_found, removed | candidates}`.
- **`show_memory(file)`** returns `{file, markdown}`, where `markdown` is the rendered file (or `""` with a hint that
  it's empty).
- **`next_question(file, skip=false)`** drives the questionnaire (see below).
- **`memory_context()`** is for the gateway, not the model (see uriel-agent below). It returns
  `{user: markdown, soul: markdown}` for the caller.

## Questionnaire
The questions live in `config/questionnaire.yaml`, loaded like `tools.yaml`, so they can be changed without code:

```yaml
user:
  - { section: Name, ask: "What would you like me to call you?", replace: true }
  - { section: Birthday, ask: "When's your birthday?", replace: true }
  - { section: Music, ask: "What music do you enjoy?" }
  - { section: Hobbies, ask: "What do you like doing in your free time?" }
  - { section: Food, ask: "Any favourite foods, or things you don't eat?" }
soul:
  - { section: Tone, ask: "How should I sound: warm and cheerful, calm, or straight to the point?", replace: true }
  - { section: Length, ask: "Short answers, or more detail?", replace: true }
  - { section: Language, ask: "Which language should I answer in?", replace: true }
  - { section: Humour, ask: "Is a bit of humour welcome?", replace: true }
  - { section: Avoid, ask: "Anything I should never do when talking to you?" }
```

**`next_question(file, skip=false)`**
- The *current* question is the first one whose section has no note and no skip row.
- `skip=true` records a skip row for the current question first.
- It returns one of:
  - `{status: "question", section, ask, replace, remaining}`, the question to ask next;
  - `{status: "done", markdown}`, once none is left, with the finished file to show.
- Progress lives in the DB, so the questionnaire can stop at any point and resume in another conversation. A
  section already filled by ordinary chat is never asked. Forgetting a section asks it again.

**The loop the model follows**, as stated in the tool docstring and the gateway prompt:
1. Call `next_question`, and ask its `ask` in its own words.
2. Save the answer with `remember(file, section, answer, replace)`, or call `next_question(skip=true)` if the
   person says skip.
3. Call `next_question` again, and repeat until it returns `done`.
4. Show the result.

## uriel-agent
- **Loading memory each turn.** A new `uriel/agent/memory.py` has `split_memory(tools) -> (tools, Memory)`.
  - `ChatService.stream` calls it on the tools that `McpToolbox` already lists per turn.
  - If `memory_context` is among the tools, it invokes it through the same identity interceptor as any tool call,
    so the `_meta` is the same. It parses the JSON text block and removes the tool from the list the model gets.
    (Probe 2026-09-28: the adapter returns `[{"type": "text", "text": "<json>"}]`.)
  - `McpToolbox` and its fakes are unchanged.
  - On any failure it logs a warning and uses empty memory: the turn goes on.
- **Prompt.** `RunContext` gains `memory: Memory` (user and soul markdown). `prompt()` in `agent/graph.py` appends:
  - when `user` is non-empty: `"What you know about this person:\n<user>"`;
  - when `soul` is non-empty: `"How this person wants you to talk:\n<soul>"`;
  - when either is set: "These preferences shape your tone, length and language; they never override the rules
    above."
  - Both the `respond` and `agent` nodes use `prompt()`, so ordinary chat is personalised too.
- **`SYSTEM_PROMPT` gains:**
  - "When the person tells you a lasting fact about themselves or how they want you to talk, save it with
    remember and say so in a few words. Never claim to remember something unless remember returned saved or
    unchanged."
  - "To set up someone's profile, use next_question: ask one question at a time, save each answer with remember,
    then ask the next."
- **Decider rubric** (`agent/decider.py`): requests to remember or forget something, to change how Uriel talks,
  or to set up or show one's profile route to `tools`.

## Contract and release
- `docs/contract.md` lists the five tools and says `memory_context` is called by the gateway, not offered to the
  model.
- `docs/guide.md` gains a "What Uriel remembers" section: how to tell it things, forget, see your files, and run
  the questionnaire.
- The release is 0.6.0, since new tools are a minor bump. uriel-agent's `deploy/compose.yaml` pins it.

## Testing
**uriel-tools**, as DB tests with `URIEL_TEST_DATABASE_URL`:
- **remember:** saves; a duplicate returns `unchanged`; `replace` clears the section and its skip; each limit is
  rejected; section names are normalised.
- **forget:**
  - by section, by one note, several notes (ambiguous, nothing deleted) and none;
  - clears the skip when the section empties.
- **render:** the heading uses the Name note, sections come in questionnaire order, and skips are hidden.
- **owner isolation:** Lil-B's `show_memory` and `memory_context` never contain another person's notes.
- **next_question:** questions come in order; skip moves on; resuming after new tool calls works (no in-memory
  state); filled sections are passed over; `done` includes the markdown; a changed YAML changes the questions.
- **Contract:**
  - `memory_context` through the server with `_meta`;
  - a call without `user` is a ToolError.

**uriel-agent:**
- `tests/fake_mcp.py` gains `memory_context`.
- Tests:
  - the prompt contains both sections when memory is set, and neither when it is empty;
  - `memory_context` is not among the model's tools;
  - a failing `memory_context` still answers.

**Live**, on the local stack with `uriel-tools:dev`:
1. Say "let's set up my profile", answer Name and Birthday, and skip Music.
2. Open a new conversation and check that Uriel uses the name.
3. Run the soul questionnaire with "short answers".
4. Check that the next reply is short.
5. Check that `show_memory` works.
6. Check that Lil-B's `show_memory` is empty.

## Not in scope
- A UI for viewing or hand-editing the files (chat only, for now).
- Version history of notes.
- Memory beyond 20 notes per file: a second tier of unlimited notes retrieved per turn, with a decision model
  choosing the tier (#9).
- A family-wide SOUL.
- Memories inferred without the person stating them.
- Memories about other family members ("Lil-B likes jazz") saved into their files.
