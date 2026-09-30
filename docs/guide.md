# Using Uriel's tools

What the Uriel family assistant can look up for you, and how to give it your documents. This page covers
uriel-tools **0.8**.

## What Uriel can do today

| Tool | Who gets it | What it does | Status |
|---|---|---|---|
| `search_documents` | `family` | Searches the household documents you may see and answers with the title, page and a link to each file | live |
| `ocr_now` | `family` | Reads one of your scans right away instead of overnight | live |
| `inspect_document`, `fill_form`, `edit_text`, `sign_document`, `confirm_edit` | `family` | Prepare an edited copy of a document for you to review (see "Having Uriel edit a document") | live |
| `remember`, `forget`, `show_memory`, `next_question`, `answer_question`, `set_style` | `family` | Remember things about you and how you like it to talk (see "What Uriel remembers") | live |
| `draft_issue`, `file_issue` | `family` | Sends Anthony a problem report or a feature idea (see "Telling Anthony something is wrong") | live |
| `draft_schedule`, `create_schedule`, `list_schedules`, `list_runs`, `cancel_schedule`, `set_timezone` | `family` | Does recurring work for you, like a morning news digest (see "Uriel working on a schedule") | live |
| `web_search` | `family` | Searches the web and cites the pages it used | live |
| `homelab_status` | `admins` | Summarises the cluster, pods and GPU | placeholder data |
| `door_camera_last_event` | `family` | Last person or vehicle seen at the front door | placeholder data |

The groups are your Authelia (lldap) groups. Uriel only offers a tool if you're in a group that's allowed to use it.
After an admin changes your groups, sign out of Uriel and back in so it sees the new ones.

Every Uriel reply says where its answer came from:
- **"used search_documents"**: the answer comes from your files, with links.
- **"Nothing was looked up for this answer"**: the answer comes from the model's general knowledge. Don't
  take it as a statement about your own papers.

## Document search

**What gets read**
- PDF, Word (`.docx`), Excel (`.xlsx`), LibreOffice (`.odt`, `.ods`), text and Markdown files, and images (PNG,
  JPEG, WebP, TIFF).
- Up to 300 pages and 50 MB per file. Encrypted PDFs and other file types are skipped.
- Italian, English and French. You can ask in one language and find a document written in another: "questionario
  matrimonio" finds the English "Questionnaire – Marriage".

**Scans**
- Pages without real text are read by a vision model (Qwen3-VL). Photos and scanned PDFs count, and so do PDFs
  whose embedded text is unreadable.
- To leave the shared GPU free during the day, this happens overnight, between **01:00 and 06:00**.
- Until then Uriel tells you how many of your scans are still waiting. Ask it to read a specific one now ("read the
  car insurance scan now") and it uses `ocr_now`; the file becomes searchable a few minutes later.
- Text read from a scan can contain transcription mistakes, and Uriel says so when it quotes one.

**Keeping up to date**
- A newly shared folder is noticed within **5 minutes**, and an unshared one disappears from answers within 5
  minutes.
- A full check runs every night at **00:30**. New, changed and deleted files inside shared folders show up in
  answers by the next morning.
- Nextcloud can also notify Uriel the moment a file changes, which makes changes searchable within minutes. That
  becomes active once the webhooks are registered in the cluster deployment (not done yet).

**Links**
- Every hit links to `https://cloud.example.com/f/<id>`.
- The link opens the file in Nextcloud for anyone Nextcloud lets see it. Uriel can find a document for you that
  Nextcloud won't open for you; see "Choosing what Uriel can read" below.

**Privacy**
- Everything runs on Homelab: the models, the index and the files. Nothing leaves the house.
- Who can see a document is checked inside the database query itself, not by the model, so a clever question
  can't reveal someone else's files.

## Having Uriel edit a document
Ask in the chat: "fill in the school form with Giulia's details", "change the address in the rental letter to via
Verdi 3", "sign the delegation". Uriel **never changes your file**. It saves a new copy next to it, called
`edit_<name>` (`edit_<name> (2)` and so on for later versions), and gives you the link. Open it in Nextcloud:
ONLYOFFICE lets you check it and fix anything by hand.

**What it can do**

| Request | How well |
|---|---|
| Fill a PDF form that has fields | exactly |
| Fill a printable PDF without fields | writes beside each printed label; **check the placement** |
| Add a note | a comment in Word files, a sticky note in PDFs |
| Change text in Word, LibreOffice or text files | exactly, keeping the formatting |
| Change printed text in a PDF | best effort, in a standard font |
| Sign a PDF | places **your** signature image; see below |

Dynamic ("XFA") PDF forms can't be filled reliably; Uriel will say so, and you can fill them in ONLYOFFICE.

**What it may edit:** exactly what it may search. That means your folders shared with `uriel`, and folders shared
with a `uriel-<group>` group you belong to. A document someone shared only with you in Nextcloud isn't visible to
Uriel, so it can't edit it for you.

**Where it can save:** only in folders whose share has **Allow creating** ticked. Read-only shares still work for
search, and Uriel tells you when it can't save and whom to ask. To allow it, open the folder's sharing panel, find
`uriel` (or `uriel-family`), and tick *Allow creating*.

**Draft states:** Uriel keeps track of each copy it made, and search results show the state:
- **draft:** exactly as Uriel wrote it;
- **changed:** someone edited it after Uriel, e.g. fixed a field in ONLYOFFICE;
- **confirmed:** you told Uriel it's good ("the school form is fine").

Copies are searchable too, from the night after they're made, marked as the edited version of the original.

### Your `Uriel` folder: personal items
Create a folder named **`Uriel`** (any capitalisation) in your own files and share it with the user `uriel` (not with a group). Uriel
uses what's inside only on your behalf:
- **`firma.png`** (or `signature.png`): your handwritten signature, preferably a PNG with a transparent background.
  Sign a white sheet, photograph it, and remove the background.
- more personal items in future, such as a profile picture.

This folder is never searched and never shown to anyone else.

### Signing
Uriel only places **your signature image** on the page, like signing a printout and scanning it back. It's not a
digital signature: documents that need a *firma digitale* (CIE, SPID, a signing provider) must still be signed by
you with your own device, and Uriel can prepare the final file for that. Before signing, Uriel tells you exactly
where the signature will go and waits for your "yes" in the chat. It never uses anyone else's signature.
When a form has a box for each signer (both spouses, a parent and a child), Uriel asks which one is yours
rather than guessing.

## What Uriel remembers
Uriel keeps two short notes about each person, visible only to them:
- **USER.md:** what it knows about you: the name you like, your birthday, music, hobbies…
- **SOUL.md:** how you want it to talk to you: warm or to the point, short or detailed, which language…

Both are loaded into every conversation.
- **Tell it:** "call me Tony", "I love jazz", "keep your answers short with me". Uriel saves the fact and says so.
- **Describe a style:** "talk to me like a pirate", "like Jarvis". Uriel works out the tone, length, humour and
  what to avoid, saves them together (replacing your old style), and tells you what it saved.
- **Set it up in one go:** "let's set up my profile" (or "…how you talk to me"). Uriel asks one question at a
  time. Say "skip" to pass, and stop whenever you like: next time it carries on where you left off. The
  questions are in `config/questionnaire.yaml`.
- **See it:** "what do you know about me?" or "show my SOUL.md".
- **Change it:** "forget my birthday", "forget that I like opera". If that matches more than one note, Uriel
  asks which one.
- **Limits:** each file holds up to 20 short notes (200 characters each). When one is full, Uriel asks what to
  forget.

It only saves what you tell it, about yourself and your family (under Family). It never saves guesses.

## Uriel working on a schedule
Ask for recurring work in the chat: "every weekday at 9, give me a digest of news about electric cars", or "on
the 1st of every month, summarise what's new in my Bills folder". Uriel shows you what it will do and when (the
next three times), and schedules nothing until you say yes.
- **Results** arrive as a message from Uriel in your Nextcloud Talk chat with it. Reply there to ask about them.
- **What's scheduled:** "what are you doing for me?" lists your schedules, when each runs next and how the last
  run went. "how did the news digest go this week?" shows recent runs.
- **Change or stop:** "stop the news digest". To change one, stop it and ask for the new version.
- **In Nextcloud:** your calendar list gets **Uriel · <you>**, with each schedule as a recurring event, and
  **Uriel · done**, with a ticked task for each finished run. Deleting an event there stops that schedule.
  Editing one does nothing: Uriel puts it back, so make changes in the chat.
- **Timezone:** times are in your timezone, Toronto time unless you say otherwise ("I'm in Rome now"). Existing
  schedules keep the timezone they were made in; Uriel offers to redo them.
- **What a scheduled run can do:** only things that need nobody's confirmation: search the web, search your
  documents, read your USER.md, list your schedules. It never files reports, edits or signs anything on its own.
- If Uriel was down when something was due, a run missed by more than a short while (10 minutes for a one-time
  task, up to 2 hours for a daily one) is skipped rather than sent late. It still shows in **Uriel · done**, as
  "<title>: skipped".

These are Uriel's tasks. Your own reminders and appointments are a separate feature, still to come.

## Searching the web
Ask about news or anything recent, e.g. "what happened with the EU AI Act this week?". Uriel searches through the
family's own search engine (SearXNG), answers from what it found, and links every page it used. It doesn't open
the pages themselves, only the search results. If the search engine is down, Uriel says so instead of guessing.

## Telling Anthony something is wrong

If Uriel fails at something, gets it wrong, or can't do something you'd like, say so in the chat, e.g. "that
signature is in the wrong box, tell Anthony" or "I'd like Uriel to remind me about bills". Uriel writes a short
report and shows it to you first. Nothing is sent until you say yes, and what gets sent is exactly the draft you
saw; then you get the issue number and a link.

The report goes to the uriel-agent issue tracker with your username, labelled `kind/bug` or `kind/feature`
and `from-chat`. Uriel is told to leave document contents out of it, but read the draft before you agree.

## Choosing what Uriel can read

You decide, in the Nextcloud sharing panel, just as you share with a person. Uriel has its own Nextcloud
account, **`uriel`**, which can only read what's shared with it.

| You want… | Share the folder with |
|---|---|
| a person to have it in Nextcloud, with Uriel not involved | that person, as usual |
| Uriel to answer about it **for you only** | the user **`uriel`** |
| Uriel to answer about it **for the whole family** | the group **`uriel-family`** |
| Uriel to stop reading it | remove the share; its documents leave the index within 5 minutes |

Search results name a folder the way it appears in the `uriel` account, which can differ from your name for it:
if two people share a folder called `Documents`, the second shows up as `Documents (2)`.

**Read-only is enough.** Uriel never changes your files, so untick "Allow editing" when sharing.

**Combining shares on one folder:**
- **Family folders usually get two shares:** `family`, so everyone can open the links, and `uriel-family`, so
  everyone can ask Uriel about it. With `uriel-family` alone, the family can search it but the links won't open for
  them.
- **For you and one other person, for example your partner:**
  1. share the folder with them, with "Allow resharing" on;
  2. each of you shares it with `uriel`.

  The rule stays the same for everyone: whoever shares a folder with `uriel` can search it through Uriel.
- **For a group that recurs,** such as parents only, an admin can create the matching pair of groups: `parents` in
  lldap, and `uriel-parents` in Nextcloud with only `uriel` in it. Sharing with `uriel-parents` then makes a folder
  searchable by `parents`.

Why `uriel` isn't simply a member of `family`: every folder shared with the family for any reason would then be
indexed without anyone choosing that. `uriel-*` groups make indexing a deliberate choice.

The full design is in [specs/2026-09-28-share-based-sources-design.md](specs/2026-09-28-share-based-sources-design.md).
Until 0.3 an admin listed the folders in `config/sources.yaml` and the indexer used each owner's app password.
That list is gone; a config that still has one refuses to start and says to share instead.

## For admins
- **Running it:**
  - the [README](https://github.com/TonyHeadband/uriel-tools/blob/main/README.md) covers
    development, release and running against a real Nextcloud;
  - [contract.md](contract.md) is the interface with the Uriel gateway.
- **Service account:**
  - the Nextcloud user `uriel` is not an admin and belongs to no groups apart from `uriel-*`;
  - its app password is in Vaultwarden, folder `Homelab`, item "Nextcloud: uriel".
- **Issue reports:** `file_issue` files as the Forgejo user `uriel` (a Write collaborator on uriel-agent,
  token scope `write:issue`). It needs `URIEL_FORGEJO_URL`, `URIEL_FORGEJO_REPO` and `URIEL_FORGEJO_TOKEN`; if
  any is unset the tool says it isn't configured. The labels `kind/bug`, `kind/feature` and `from-chat` must
  exist on the repo, or the issue is filed without them. The token is in Vaultwarden, folder `Homelab`, item
  "Forgejo: uriel bot token".
- **Schedules and web search:**
  - `web_search` needs `URIEL_SEARXNG_URL` (the in-cluster SearXNG service; locally the public host).
  - The calendar mirror runs in the MCP server whenever Nextcloud credentials are set. `URIEL_MIRROR_ENABLED=false`
    turns it off, and `URIEL_MIRROR_PUSH_SECONDS` (30) and `URIEL_MIRROR_PULL_SECONDS` (60) tune it.
  - People without a timezone of their own get `URIEL_DEFAULT_TZ` (`America/Toronto`).
  - The group `uriel-internal` belongs to the gateway's runner alone: never create it in lldap or give it to a
    person.
- **Admin-only setup:** Nextcloud requires a password confirmation to create users and groups or to change
  membership, so those steps are done in the admin UI (**Administration → Users**).
