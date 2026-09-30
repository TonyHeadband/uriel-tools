# Editing documents: `edit_` copies for review

Status: implemented in 0.4.0 (2026-09-28). Target release: 0.4.0. Builds on
[2026-09-28-share-based-sources-design.md](2026-09-28-share-based-sources-design.md).

## Why
Uriel can find and discuss the family's documents, but it can't act on "fill this in", "change the date to…" or
"sign it". Humans now have ONLYOFFICE inside Nextcloud for that. Uriel should do the tedious first pass itself and
leave the result next to the original for a person to check and fix in ONLYOFFICE.

**The rule everything follows:** Uriel **never changes an existing file**. Every edit is a new file,
`edit_<original name>`, in the same folder, and the reply links to it.

## Verified facts (2026-09-28)
- **PDF forms come in two kinds, even in one folder** (`/Documents/Marriage`):
  - *Application for the designation of an officiant* has **68 AcroForm fields** and no XFA, so the fields can be
    filled properly;
  - *SJ-217A Questionnaire – Marriage* has **no form fields** at all (a flat, printable PDF), so "filling" it means
    placing text next to its printed labels ("Family name", "Date of birth").
- **ONLYOFFICE** (9.4, `office.example.com`) opens both and edits form fields; that's the human review step.
- **Current access:** today's shares with `uriel` are read-only (`permissions: 1`). Nextcloud's permission bits are
  1 read, 2 update, 4 create, 8 delete, 16 share. Saving an `edit_` file needs **create (4)** on the share.

## Scope of 0.4.0

| Request | How | Reliability |
|---|---|---|
| Fill a PDF that has form fields | set the AcroForm fields (pypdf), text, checkboxes, choices | reliable |
| Fill a flat PDF | place text beside or below a printed label, found by its text (PyMuPDF) | approximate: review needed |
| Add a note to a PDF | a sticky-note annotation next to an anchor text | reliable |
| Sign a PDF | the caller's own signature image, placed at a signature field or beside an anchor such as "Signature"/"Firma" | approximate placement |
| Change text in `.docx` / `.odt` | find and replace, keeping the formatting of the matched text | reliable |
| Add a comment to `.docx` | a Word comment anchored on the matched text (python-docx ≥ 1.2) | reliable |
| Change printed text in a PDF | redact the old text and write the new text in its place | best-effort, labelled as such |

**Not in scope:**
- **Cryptographic signatures** (PAdES, *firma digitale*): these need the person's own device or provider; see Signing.
- **XFA forms:** detected and reported ("this is a dynamic form: open it in ONLYOFFICE"), never filled.
- **Spreadsheets, and converting between formats.**

## Tools
Four editing tools, plus `confirm_edit` (see Draft states), all gated to `family` in `tools.yaml`. Each takes a `path` exactly as a search hit returns it. Each works on
a document the caller may see, meaning the same visibility rule as search, applied to the folder containing the
path.
- **`inspect_document(path)`** returns:
  - the type and page count;
  - form fields (name, type, current value, choices), or `"flat"`, or `"xfa"`:
    - a radio group is one entry whose choices are the options' printed labels ("Male", "Female"), because a
      radio's own values are opaque ("0", "Yes");
    - text fields report `max_length` when set, as rows of boxes (a date as `20260928`) often are;
    - push buttons (print, reset, help) are left out;
  - the signature boxes found: signature fields, plus text fields and buttons named for a signature
    (`1-11 Signature mandatory`), which many forms use instead; `fill_form` won't type into them;
  - whether the caller can save edits in that folder, and if not, why ("shared read-only: ask the owner to allow
    creating files").
- **`fill_form(path, values)`**, with `values: {field name or label text: value}`:
  - field names fill AcroForm fields;
  - on a flat PDF, the keys are label texts and the value is written beside the label.
  - It returns what was filled, what wasn't found, and `approximate: true` for flat placement.
- **`edit_text(path, changes, notes=[])`**, with `changes: [{find, replace}]` and `notes: [{anchor, text}]`:
  - Word/LibreOffice: replacements and comments;
  - PDF: notes, plus best-effort replacements flagged `approximate: true`.
- **`sign_document(path, where=None, confirmed=false)`** places the caller's signature (see Signing).

**Why a handful of focused tools rather than one generic `edit` tool with a list of operations:** qwen3:8b calls
small, flat schemas far more reliably than nested unions. The tool-calling step already needed brief reasoning to
call `search_documents` at all.

**Every tool that writes returns:**
```json
{"saved": "/Documents (2)/Marriage/edit_Application ….pdf",
 "url": "https://cloud.example.com/f/<new id>",
 "done": ["…"], "not_found": ["…"], "approximate": false, "warnings": ["…"]}
```
The tool descriptions tell the model to give the link, say "please review it in Nextcloud before using it", and
repeat any `not_found` / `approximate` / `warnings` plainly.

## Saving
- **File name:** `edit_<original name>` in the source's folder. If that name exists, the result is
  `edit_<stem> (2).<ext>`, then `(3)` and so on, the way Nextcloud names copies. Nothing is ever overwritten,
  including earlier drafts, which a person may have fixed by hand in ONLYOFFICE.
- **Continuing an edit:** pass the draft as `path` ("now also sign it"). The result is the next draft, so the
  history stays visible and each step can be reviewed.
- **Write mechanics:**
  - WebDAV `PUT` with `If-None-Match: *`, so the server refuses to replace a file created in the meantime; on a
    `412`, retry with the next number;
  - then a `PROPFIND` for the new file id, to build the link.
- **Where writes are allowed:**
  - only in folders whose share grants **create**; the shares listing's `permissions` is stored per folder as
    `can_create`;
  - a read-only share still works for search and `inspect_document`, and the write tools answer with the reason
    instead of failing.
- **Who can write:** anyone who can see the folder. That's owners, and members of the group for a `uriel-<group>`
  share, which is what Nextcloud itself lets them do with a "can create" group share.
- **Indexing drafts (decided):** drafts keep their `edit_` name for good, since the family wants the original and
  the edited version side by side. They are indexed, but **only overnight**:
  - an `edit_` file found by a reconcile or webhook gets its index job at the next start of the night window
    (the OCR window, 01:00–06:00), so a draft being reviewed during the day isn't re-read at every save;
  - search hits from a draft carry `edit_of: <original path>`, and the tool description tells the model to
    present them as "the edited version of …".

## The personal `Uriel` folder
Each person may share a folder named **`Uriel`** with the user `uriel`, as a user share. It holds personal items
Uriel may use on that person's behalf:
- `firma.png` or `signature.png`: their handwritten signature, as a transparent PNG;
- later: a profile picture, and other items the next features need.

**Rules**
- **Recognising the folder:** a user share whose mount is `Uriel` in any capitalisation, or `Uriel (2)` and so on, since two people's
  folders clash in `uriel`'s tree. A group share named `Uriel` doesn't count, because a group-owned item could be
  used on everyone's behalf.
- **Use:** an item is only ever used for that folder's owner.
- **Not indexed:** it holds items, not documents to search.
- **Guide:** a section on how to set it up.

## Signing (decided)
- **Only visual signatures:** Uriel places the person's own signature image on the document, legally a simple
  electronic signature, like signing a printout and scanning it. No cryptographic signatures: a qualified *firma
  digitale* (CIE, SPID remote signing, providers) needs the person's own device or OTP by design, and self-issued
  certificates would mean Uriel holding everyone's private key. For documents that need a real digital signature,
  Uriel prepares the final file and says so.
- **The image:** the caller's own `firma.png` / `signature.png` from **their** `Uriel` folder, never anyone else's.
  None, or several, means an error that says what to do.
- **Placement:**
  - a signature box: the one whose name contains `where`, or the only one. A form with a box per signer and no
    `where` is refused with the list of boxes, so the model asks the person which is theirs;
  - otherwise beside the anchor text in `where`, or the last occurrence of "Signature"/"Firma"/"Signé";
  - scaled to the line height of the anchor's text, at least 24 pt; the image is first cropped to its ink;
  - a text or button placeholder is removed, since it is drawn above the page and would hide the image.
- **Confirmation in the chat:**
  - `sign_document` with `confirmed=false` only describes the placement ("your signature, beside 'Firma' on page 2
    of …") and writes nothing;
  - the model asks the person in the chat, and calls again with `confirmed=true` only after they say yes;
  - the tool description and the gateway prompt both say so.
- **Safeguards:** the model could skip the question, so the real safeguards are that the result is a new `edit_`
  file the person reviews, it's always their own signature, and the reply shows "used sign_document".

## Where it runs
- **Process:** the MCP server, not the indexer, because the edits are interactive.
- **Credentials:** the MCP server now needs WebDAV access too, with the same `URIEL_NC_USER` /
  `URIEL_NC_APP_PASSWORD` as the indexer.
- **Limits:** the same file size limit (50 MB) and a 60 s time limit per edit; rendering runs in a thread like
  extraction.
- **New dependency: PyMuPDF.** It finds text positions, places text and images, and handles annotations and
  redaction. pypdf stays for AcroForm fields and for extraction.
- **Licence note:** PyMuPDF is AGPL-3.0. uriel-tools is private and serves only the household, which the AGPL
  permits without publishing the source. Revisit if the repo is ever distributed.

## Who can edit what
Editing follows exactly the rule search uses: a caller can have Uriel edit documents in folders **they** shared with
`uriel`, and in folders shared with a `uriel-<group>` group they belong to. Nothing else, and in particular not a
document another person shared with them in Nextcloud but not with Uriel: if Lil-B shares a folder only with
Anthony, Uriel can't see it, so it can't edit it for him. Saving additionally needs "can create" on the share (see
Saving).

## Draft states
A draft keeps its `edit_` name; its state lives in the `edits` table, not in the file name:

| State | Meaning | How Uriel knows |
|---|---|---|
| `draft` | exactly as Uriel wrote it | the file's etag equals the one recorded when it was saved |
| `changed` | a person edited it after Uriel (e.g. fixed a field in ONLYOFFICE) | the etag differs; the file id is the same |
| `confirmed` | a person approved it | they said so in the chat, and `confirm_edit(path)` recorded it |

- **How `changed` is detected:** when the indexer reconciles a folder (nightly, or after a webhook) and an `edit_`
  file's etag differs from the recorded one, a `draft` becomes `changed`. `confirmed` is final: later changes are
  the person's own.
- **Search:** hits from a draft carry `edit_status` next to `edit_of`, and the model says "draft, not reviewed
  yet", "edited by you after Uriel's draft" or "confirmed".
- **`confirm_edit(path)`:** a fifth, tiny tool, allowed for the same callers as the edit tools.
- **For learning later:** `changed` drafts are the ones a person corrected, which is the labelled signal the
  "learning from reviews" step below needs.
- **Visible in Nextcloud's file list, later:** Nextcloud tags (`uriel-draft`, `uriel-confirmed`). Not in 0.4.0,
  because it's unverified whether `uriel` may tag files in someone else's share.

## Recording edits (in 0.4.0) and checking them (later)
**In 0.4.0:** every edit is recorded in a table `edits` (migration `0003`), with these columns:
- the source path, the output path, its file id, and the etag Uriel wrote;
- `status` (`draft`, `changed` or `confirmed`);
- the tool, the caller and the time;
- one row per change in `edit_items`: the field or label, the value, the page, the rectangle it was placed in,
  and `approximate`.

Nothing reads this table yet. It exists so the next two steps don't need to reconstruct what was done.

**Later: a check on each fill.** "Did this value land in the right field?" is a yes/no judgement with a
confidence, which is what uriel-agent's decider points already are.
- **How it works:** after a flat-PDF fill, render a crop around each placed value and ask a decider whether the
  value sits in the field for its label.
- **The decider:**
  - today it would be the vision model (Qwen3-VL);
  - later a trained checker, such as OpenJev Verdict, behind the same `Decider` interface.
- **The result:** doubtful items come back in the tool result ("I'm not sure 'Rossi' landed in *Family name* on
  page 1"), so the person knows where to look.

**Later: learning from reviews.** When someone fixes a draft in ONLYOFFICE and keeps it (renames it), comparing the
kept file with the draft's `edit_items` shows which placements were accepted and which were moved. Those are
labelled examples for the checker, the same idea as `corrected_choice` in uriel-agent's decisions table, gathered
without asking anyone to label anything.

## Changes elsewhere
- **uriel-tools:**
  - store `can_create` per folder from the share permissions, and add the `edits` / `edit_items` tables
    (migration `0003`);
  - index `edit_*` files only in the night window and mark their hits `edit_of`; skip `Uriel` folders when
    indexing;
  - add the four tools to `tools.yaml` (`family`);
  - give the MCP container the WebDAV settings.
- **uriel-agent:**
  - add "fill", "edit", "sign", "change", "correct" requests to the router rubric, so they reach the tools path;
  - extend the prompt: never claim a document was changed without a tool result, always give the `edit_` link, and
    ask before signing.
- **Nextcloud:** owners who want edits in a folder re-share it with **"Allow creating"** ticked. The guide explains
  this in a new "Letting Uriel prepare edits" section.

## Testing
- **Unit tests with generated files** (tests/filegen gains an AcroForm PDF, a flat labelled PDF, and `.docx`/`.odt`
  with formatted runs):
  - field filling;
  - label placement lands on the same page, next to the label's rectangle;
  - replacement keeps run formatting;
  - naming (`edit_`, then ` (2)`);
  - an XFA document is refused;
  - `confirmed=false` writes nothing;
  - someone else's signature is never used, and a group-shared `Uriel` folder is ignored;
  - `Uriel` folders are never indexed;
  - an `edit_` file's index job waits for the night window, and its hits carry `edit_of`.
- **Fake Nextcloud:** gains `PUT` with `If-None-Match` and share permissions.
- **e2e:** fill a generated form in a writable share, then read the `edit_` file back; a read-only share refuses
  with the reason.
- **Live check:** on a disposable copy of the officiant application, in a writable test folder, open the result in
  ONLYOFFICE.

## Decisions (2026-09-28)
1. **Personal items:** a `Uriel` folder each person shares privately with `uriel` (signature now, profile picture
   later), not a file convention in any folder.
2. **Drafts:** they keep their `edit_` name next to the original and are indexed overnight, not excluded.
3. **Signing:** visual only, placed after the person confirms in the chat; no *firma digitale*.
4. **Draft states:** `draft`, `changed`, `confirmed`, tracked in `edits` rather than in file names (no second
   prefix).
5. **Access:** editing uses exactly the rules search uses; documents not shared with Uriel can't be edited.
