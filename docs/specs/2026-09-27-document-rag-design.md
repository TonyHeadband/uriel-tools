# uriel-tools: document RAG over Nextcloud

Status: draft, awaiting review · Date: 2026-09-27 · Wire contract: [docs/contract.md](../contract.md)

## Goal
`search_documents` stops being a stub. Family members ask Uriel about their own documents in Italian, English or
French: the boiler manual, the school calendar, the insurance policy. They get answers backed by the real files,
with a link to open the original.

Success means:
- **Own folders:** a file saved in an opted-in Nextcloud folder becomes searchable. A text document is searchable
  within one Nextcloud cron cycle (about 5 minutes). A scan is searchable after the next OCR window, or right away
  through `ocr_now`.
- **Privacy:** a user only ever gets hits from their own folders and from folders shared with one of their groups.
  With no identity in `_meta`, search returns nothing.
- **Degraded operation:** with Ollama down, search still works on exact terms. Missed webhooks are caught up by the
  nightly reconcile.
- **Independence:** nothing changes in uriel-agent's code. It only pins a new uriel-tools image and adds env and a
  database in its compose.

## Context
- **Nextcloud 35.0.1** runs in the `cloud` namespace. It is the family's whole cloud drive, and documents are only
  a slice of it.
  - Users log in through Authelia via **sociallogin**. User IDs are either the Authelia username (a local account
    linked to Authelia, e.g. `anthony-headband`) or `authelia-<subject-uuid>` (accounts sociallogin created, e.g.
    Lil-B).
  - **`webhook_listeners` 2.0 is enabled.** It sends webhooks from Nextcloud background jobs, so they arrive on
    the cron cycle.
- **Hardware today:** Ollama on the shared RTX 3070 (8 GB), time-sliced with Frigate, immich-ml and Jellyfin.
- **Target hardware (128 GB unified memory):**
  - Qwen3-Embedding for embeddings, with Qwen3-Reranker as the second stage;
  - Qwen3-VL for vision and OCR.

  Moving to it must be a config change only.
- **Repo:** uriel-tools was split from uriel-agent at 0.1.0 (see [contract.md](../contract.md)). This is its
  first feature.

## Decisions
| # | Decision | Rejected |
|---|---|---|
| R1 | **Opt-in folders** listed in `config/sources.yaml` | Whole drive filtered by type (indexes things never meant for the assistant); a Nextcloud tag (easy to forget) |
| R2 | **Per-owner visibility**, plus `share_with` groups for each folder | Everything visible to `family` (exposes private papers); admins only (not useful to the family) |
| R3 | **Webhooks for change notification, WebDAV for content** (one app password per owner), plus a **nightly reconcile** | A read-only mount of the Nextcloud data PV (can read everyone's drive, breaks with encryption, tied to internal layout); webhooks alone (events get lost) |
| R4 | **The generic events intake lives in uriel-tools** (`events` process, a handler per source) | In the gateway (the agent would learn about integrations, undoing the split) |
| R5 | **One image, three processes** (`mcp`, `events`, `indexer`); **Postgres for everything**: pgvector, full-text and the job queue | Qdrant and Redis (two more stateful services, not needed at family scale); one process doing everything (OCR would stall tool calls) |
| R6 | **Qwen3-Embedding at a fixed 1024 dimensions** (Matryoshka truncation) | bge-m3 (fine, but outside the target model family); native dimensions (the schema would change with the model size) |
| R7 | **OCR by Qwen3-VL** as a queued job inside a nightly window, plus `ocr_now` | Tesseract (weaker on mixed layouts and handwriting); OCR during ingestion (holds the shared GPU during the day) |
| R8 | **Keep sociallogin**; `sources.yaml` maps `owner` to `nextcloud_user` | Switching Nextcloud to user_oidc (orphans existing accounts, hashes IDs by default) |
| R9 | **Hybrid search**: vector plus `simple` full-text, merged with reciprocal-rank fusion; a rerank hook, **off** until a rerank server exists | Vector only (misses policy numbers and names); a reranker now (Ollama has no rerank endpoint) |

## Architecture
```
Nextcloud ──webhook (X-Uriel-Source-Key)──▶ events ──▶ jobs (Postgres)
                                                          │
                        indexer ◀─────────────────────────┘      nightly reconcile ──▶ jobs
                          │  WebDAV as the folder's owner
                          ▼  extract / OCR (Qwen3-VL) → chunk → embed (Qwen3-Embedding)
                      documents + chunks (pgvector, tsvector)
                          ▲
uriel-agent ──MCP──▶ mcp: search_documents, ocr_now (access filter from _meta)
```

All three processes run from the one `uriel-tools` image and share the `uriel_tools` database:
- `python -m uriel_tools` runs **mcp**, as today;
- `python -m uriel_tools.events` runs **events** on port 8002;
- `python -m uriel_tools.indexer` runs **indexer**, which has no port. Its liveness is a heartbeat file.

Source layout: the existing modules stay. New packages:
- `rag/`: `sources` (config), `store` (SQL), `webdav`, `extract`, `ocr`, `chunk`, `embed`, `search`;
- `events/`: `app`, `handlers/nextcloud`;
- `indexer/`: `worker`, `jobs`, `reconcile`;
- `db.py`: pool and migrations, following uriel-agent's `gateway/db.py` pattern: psycopg3, `autocommit`,
  `prepare_threshold=0`, and migrations applied under an advisory lock with `prepare=False`.

## Data model
**`config/sources.yaml`**, deployed through GitOps next to `tools.yaml`:

```yaml
ocr_window: { start: "01:00", end: "06:00", tz: Europe/Rome }
folders:
  - owner: anthony-headband          # Uriel user = Authelia preferred_username = _meta.user
    path: /Documents
    share_with: []
  - owner: anthony-headband
    path: /Famiglia
    share_with: [family]
  - owner: lil-b                     # example; use her real Authelia username
    nextcloud_user: authelia-19e1429e-50c1-409e-be65-b7be28b52e03   # defaults to owner
    path: /Scuola
    share_with: []
```

- **App passwords:** each owner's WebDAV app password comes from `URIEL_NC_APP_PASSWORD_<NEXTCLOUD_USER>`,
  upper-cased with non-alphanumerics replaced by `_`.
- **Startup validation:** overlapping folders for the same `nextcloud_user` are rejected. A folder whose owner has
  no password is flagged `auth_failed` and skipped, so the other folders still run.

**Tables** (`uriel_tools` database, with its own role):

| table | columns |
|---|---|
| `folders` | `id`, `owner`, `nextcloud_user`, `path`, `share_with text[]`, `state` (`ok`, `auth_failed`). Mirrors `sources.yaml` on every start, and a folder that disappears from config is deleted along with all its documents. |
| `documents` | `id`, `folder_id`, `path`, `fileid` (Nextcloud `oc:fileid`), `etag`, `mime`, `size`, `modified`, `status` (`indexed`, `awaiting_ocr`, `failed`, `skipped`), `error`, `attempts`, `indexed_at`. Unique on `(folder_id, path)`. |
| `chunks` | `id`, `document_id` (on delete cascade), `ord`, `page`, `source` (`text`, `ocr`), `text`, `embedding vector(1024)` (nullable), `embed_model`, `tsv tsvector` (generated with `to_tsvector('simple', text)`). HNSW (`vector_cosine_ops`) on `embedding`, GIN on `tsv`. |
| `jobs` | `id`, `kind` (`index`, `ocr`, `delete`, `reconcile`), `folder_id`, `path`, `run_after`, `attempts`, `last_error`, `locked_until`, `created_at`. A partial unique index on `(kind, folder_id, path)` where `locked_until is null` collapses duplicate events. |

**Embedding model changes.** `embed_model` records which model produced each vector. If the configured model
changes, the indexer re-embeds stale chunks in the background. Search keeps working meanwhile, because stale
vectors are only used while no current ones exist.

## Access control
- **Two layers.** The `tools.yaml` gate still applies: `search_documents` and `ocr_now` are allowed for `family`.
  Row-level visibility is enforced **inside every query**, never after it:
  ```sql
  JOIN folders f ON f.id = d.folder_id
  WHERE f.owner = %(user)s OR f.share_with && %(groups)s
  ```
- **No identity:** a missing `_meta.user` gives an empty result, not an error.
- **Indexer credentials:** the indexer never uses `_meta`. It reads each folder only with that folder's owner's
  app password, so it can't see beyond what that person sees in Nextcloud.
- **Logs:** log lines carry `document_id` and `folder_id`, never paths, snippets or document text.

## Ingestion
**Events intake** (`POST /events/{source}`):
- The `X-Uriel-Source-Key` header is compared in constant time with `URIEL_EVENTS_KEY_<SOURCE>`.
- Handlers are a registry keyed by source name, with `nextcloud` the only one now.
- Every response is fast. Work only happens in the job queue.

| response | when |
|---|---|
| `202` | accepted, or ignored because the path is outside every opted-in folder (nothing is stored) |
| `401` | bad or missing key |
| `404` | unknown source |
| `413` | body over 1 MB |
| `422` | payload doesn't parse |
| `503` | database down (the reconcile catches up) |

**The Nextcloud handler** takes `NodeCreatedEvent`, `NodeWrittenEvent`, `NodeDeletedEvent` and
`NodeRenamedEvent`:
- The node path `/<nextcloud_user>/files/<path>` is matched to a folder by longest prefix.
- Create and write queue `index`, delete queues `delete`, and a rename queues `delete` for the old path plus
  `index` for the new one.
- Registration is a one-off admin script, `scripts/register-nextcloud-webhooks.sh`, that runs `occ` against the
  Nextcloud pod with the header auth set. It is written in this spec's implementation but run only when the
  cluster deploy happens.

**Worker loop.** It claims one job with `FOR UPDATE SKIP LOCKED` where `run_after <= now()`. While a job runs,
`locked_until` is set to now plus a 10-minute lease, so a crashed worker's job is picked up again.

**The `index` job:**
1. `PROPFIND` the file as the owner. If the ETag equals the stored one, the job is done.
2. Check the file against the limits: the allowed MIME types (below), at most 50 MB and at most 300 pages.
   Anything else gets `status = skipped` with a reason.
3. `GET` the file and extract its text, with a 120-second timeout per document:
   - **PDF:** `pypdf`, page by page. A page with fewer than 20 characters of text is treated as needing OCR.
   - **DOCX:** `python-docx`.
   - **ODT and ODS:** `odfpy`.
   - **XLSX:** `openpyxl`, one sheet per "page", rows as tab-separated text.
   - **TXT and MD:** read directly as UTF-8.
   - **Images (PNG, JPEG, WebP, TIFF)** and any PDF with pages needing OCR: the text pages are indexed now, the
     document gets `status = awaiting_ocr`, and an `ocr` job is queued with `run_after` set to the next window
     start.
4. Chunk to about 800 tokens with a 100-token overlap, split on paragraph boundaries, keeping the page number.
   Each chunk's embedding input is prefixed with `"<file name> — <folder path>"`.
5. Embed in batches of 16 with the `embedding` role.
6. Swap in one transaction: delete the document's old chunks, insert the new ones, update `etag`, `fileid`,
   `status` and `indexed_at`.

**The other jobs:**
- **`ocr`:** runs only inside `ocr_window`, unless `run_after` was set by `ocr_now`.
  - Each page is rendered to PNG at 150 dpi with `pypdfium2`.
  - It is sent to the `ocr` role with a literal-transcription prompt ("transcribe exactly, do not correct or
    summarise, keep line breaks"), temperature 0 and `keep_alive: 0`.
  - The text goes through steps 4–6 with `source = ocr`.
- **`delete`:** deletes the document row, and its chunks cascade.
- **`reconcile`:** queued at startup and daily at 00:30. For each `ok` folder it does a recursive `PROPFIND` and
  compares the result with `documents`. It queues `index` for new or changed ETags and `delete` for missing paths.

**Retries.** A failed job is retried at `run_after = now() + 2^attempts minutes`. After 5 attempts the document is
set to `failed` with `error` kept, and it is retried only when a later reconcile sees a new ETag.
- **Ollama unreachable:** the job waits 5 minutes and doesn't use up an attempt.
- **WebDAV 401:** the folder becomes `auth_failed`, its jobs are dropped, and an error is logged once. A restart
  or config change clears it.

## Search
**`search_documents(query: str, k: int = 5, folder: str | None = None)`**, with `k` capped at 10:
1. Embed `"Instruct: Given a household question, retrieve passages that answer it\nQuery: " + query`.
2. One SQL statement:
   - vector top-40 by cosine distance, and full-text top-40 by `ts_rank` on
     `websearch_to_tsquery('simple', query)`;
   - both behind the access-control join, and optionally restricted to `folder`;
   - merged with reciprocal-rank fusion (`1 / (60 + rank)` summed per chunk);
   - at most 2 chunks per document, then the top `k`.
3. **Rerank hook:** applied only if `models.yaml` defines `rerank`. It is absent in this spec.
4. Return:
   ```json
   {"hits": [{"title": "caldaia-manuale.pdf", "folder": "/Documents", "path": "/Documents/Casa/caldaia-manuale.pdf",
              "page": 12, "source": "text", "snippet": "…", "modified": "2026-03-02",
              "url": "https://cloud.example.com/f/48213"}],
    "awaiting_ocr": 3}
   ```
   - `snippet` is the chunk text, trimmed to 600 characters.
   - `url` is built from `URIEL_NC_PUBLIC_URL` and `fileid`.
   - `awaiting_ocr` counts the caller's visible documents in that state.
   - If the query embedding fails, the full-text half runs alone and the result adds
     `"degraded": "semantic search unavailable"`.
   - The tool description tells the model to cite `title` and `url` for every hit it uses, and to mention
     `(OCR)` hits as possibly imperfect.

**`ocr_now(path: str)`** targets a document that is `awaiting_ocr` in a folder whose `owner` is the caller. It
sets its pending `ocr` job's `run_after = now()` and returns the number of OCR jobs ahead of it. Otherwise it
returns "not found among your documents waiting for OCR".

## Models
(Amended 2026-09-28: there is one deployment-wide `models.yaml`, kept in uriel-agent's `config/` and mounted into every process; uriel-tools reads only its roles and ships no copy.) **`config/models.yaml`** in uriel-tools follows uriel-agent's `ModelSpec` shape (OpenAI-compatible, `base_url`,
`model`, `timeout_s`, `params`). It has three roles:
- **`embedding`:** called through `/v1/embeddings` with `dimensions: 1024`.
- **`ocr`:** called through `/v1/chat/completions` with an image content part.
- **`rerank`:** optional, absent for now.

| role | dev (3070, `config/models.dev.yaml`) | target hardware |
|---|---|---|
| embedding | `qwen3-embedding:0.6b` | Qwen3-Embedding 4B or 8B, still at 1024 dimensions |
| ocr | `qwen3-vl:8b` | Qwen3-VL 7B or 32B |
| rerank | none | Qwen3-Reranker behind a rerank server |

**Verify first:**
- Ollama's `/v1/embeddings` honours `dimensions` for Qwen3-Embedding. If it doesn't, truncate to 1024 and
  L2-normalise client-side.
- `qwen3-vl:8b` accepts image parts on `/v1/chat/completions` in the installed Ollama version.

## Configuration (env)
| var | used by | notes |
|---|---|---|
| `URIEL_TOOLS_DATABASE_URL` | all | the `uriel_tools` database |
| `URIEL_MODELS_FILE` | mcp, indexer | defaults to `config/models.yaml` |
| `URIEL_SOURCES_FILE` | all | defaults to `config/sources.yaml` |
| `URIEL_NC_URL` | indexer | WebDAV base, e.g. `http://nextcloud-app.cloud.svc` in the cluster, the public URL locally |
| `URIEL_NC_PUBLIC_URL` | mcp | for `/f/<fileid>` links |
| `URIEL_NC_APP_PASSWORD_<USER>` | indexer | one per `nextcloud_user` |
| `URIEL_EVENTS_KEY_NEXTCLOUD` | events | the webhook header secret |
| `URIEL_MCP_*` | mcp | unchanged |

## Failure handling
| Failure | Behaviour |
|---|---|
| Nextcloud unreachable | The job retries with backoff. Search serves what's already indexed. |
| App password revoked (401) | The folder becomes `auth_failed`; its jobs are dropped and an error is logged once. |
| Ollama down | Jobs wait without using attempts. Search falls back to full-text only, with `degraded`. |
| Tools DB down | The RAG tools return a readable tool error; other tools are unaffected. `events` returns 503; the reconcile catches up. |
| Hostile or huge file | MIME allowlist, 50 MB, 300 pages, 120-second extraction timeout; otherwise `skipped` with a reason. |
| OCR misreading | Literal prompt at temperature 0; chunks are marked `source = ocr` and the link opens the original. |
| Folder removed from config | Purged on the next start. |
| Indexer crash mid-job | The lease expires and the job is re-claimed. The chunk swap is atomic, so search never sees half a document. |

## Testing
- **Unit:**
  - `sources.yaml` validation (overlaps, `nextcloud_user` default, password env naming);
  - webhook path → folder mapping, including rename;
  - each extractor against fixture files in `tests/fixtures/`: a text PDF, a scanned PDF, DOCX, ODT, XLSX and
    PNG;
  - the chunker's boundaries and page tracking;
  - reciprocal-rank fusion and the per-document cap;
  - the intake's status codes;
  - the `ocr_window` arithmetic across midnight and time zones.
- **DB tier** (`URIEL_TEST_DATABASE_URL`, pgvector container, as in uriel-agent):
  - migrations;
  - job claim, lease expiry and dedup;
  - the atomic re-index swap;
  - reconcile diffing;
  - **the access-control matrix:** own private folder, a folder shared with a group, another user's private
    folder, no user.
- **Fakes:**
  - a Starlette WebDAV fake (PROPFIND with ETags and file IDs, GET, 401 mode);
  - deterministic embeddings (a hash seeded into 1024 floats, normalised);
  - a canned OCR model.
- **e2e** (uriel-tools compose with a `fake-webdav` service): webhook → searchable over MCP as the owner and
  invisible to another user; delete → gone; with the fake OCR model, an image → `awaiting_ocr` → `ocr_now` →
  searchable.
- **Live** (opt-in, real Ollama): an Italian and a French scanned letter are OCR'd, and English questions find
  them.

## Deployment
- **Local:** uriel-tools' `deploy/compose.yaml` gains `db` (pgvector), `events`, `indexer` and, under the `e2e`
  profile, `fake-webdav`. Against the real Nextcloud, `.env` holds `URIEL_NC_URL` and the owner's app password.
  Webhooks can't reach localhost, so local runs rely on reconcile plus `scripts/send-test-webhook.sh`.
- **uriel-agent** (a separate, small change in that repo):
  - its compose `db` gets an init script creating the `uriel_tools` database and role;
  - the `mcp` service gets the new env and pins `uriel-tools:0.2.0`;
  - `events` and `indexer` are added only when running against real Nextcloud.
- **Cluster:** held along with uriel-agent's Task 12. When it happens:
  - three Deployments from one image;
  - a NetworkPolicy allowing only the `cloud` namespace to reach `events:8002`;
  - the app passwords and events key in the SealedSecret;
  - the webhook registration script run once.
- **Release:** `0.2.0`, a non-breaking contract change. `docs/contract.md` gains an "Events intake" section. The
  tool schemas describe themselves over MCP.

## Out of scope
- The rerank server (the hook exists).
- Forwarding events to the agent.
- Legacy `.doc`/`.xls`/`.ppt`, email, photos and video.
- An opt-in UI.
- Per-document sharing that differs from the folder's.
- The cluster rollout.
- A dedicated Nextcloud background worker for faster webhooks.

## To verify first in the implementation plan
1. **Webhook payload:** the `webhook_listeners` 2.0 payload shape for node events (the path field, `user.uid`) and
   its header-auth registration syntax.
2. **Embedding dimensions:** Ollama's `/v1/embeddings` with Qwen3-Embedding and `dimensions: 1024`, plus the
   client-side fallback.
3. **Vision input:** `qwen3-vl:8b` image input on Ollama's OpenAI-compatible endpoint, and its VRAM use on the
   shared 3070.
4. **File links:** that `/f/<fileid>` links open files for their owner through the Authelia login.
