# uriel-tools

The MCP tool server behind [Uriel](https://github.com/TonyHeadband/uriel-agent), the local family
assistant. Each tool is gated by the caller's Authelia groups, which the gateway forwards as MCP `_meta`. See
[docs/contract.md](docs/contract.md).

What the tools do and how to give Uriel your documents: [docs/guide.md](docs/guide.md).

Versioned and released separately from uriel-agent. The code was imported from uriel-agent@5e476ef.

## Develop
```bash
uv sync
uv run pytest                                   # unit tests; DB tests skip without a database
docker run -d --rm --name uriel-tools-test-pg -e POSTGRES_PASSWORD=test -p 55442:5432 pgvector/pgvector:pg17
URIEL_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55442/postgres uv run pytest -m "db or not db"
uv run ruff check . && uv run ruff format --check .
```
Models come from the deployment-wide `models.yaml` in uriel-agent's `config/` (one file for every process; each
reads only its roles). The local compose mounts it from a sibling `../uriel-agent` checkout; set
`URIEL_SHARED_CONFIG` to point elsewhere.

Processes, all from one image: `python -m uriel_tools` (MCP, 8001), `python -m uriel_tools.events` (webhooks,
8002) and `python -m uriel_tools.indexer` (job worker).

**End to end** (fake Nextcloud, stub models, no GPU): see `deploy/compose.e2e.yaml` and CI's `Compose e2e` step.

**Against your real Nextcloud:**
1. Put `URIEL_NC_APP_PASSWORD=<the uriel account's app password>` in `.env`. It's in Vaultwarden, `Homelab` /
   "Nextcloud: uriel", in the "app password (uriel-tools indexer)" field.
2. Share a folder with `uriel` (or with `uriel-family`) in Nextcloud.
3. Run `docker compose -f deploy/compose.yaml up -d --build`.

The indexer lists the shares at startup and every 5 minutes. Webhooks can't reach your machine, so use
`scripts/send-test-webhook.sh <owner> <path>` (any path of a sharing owner re-checks their folders), or wait for
the next poll.

## Release
Push a bare-semver tag (`0.3.0`). CI builds and pushes `git.example.com/anthony-headband/uriel-tools:<tag>`
and prints the digest. Then bump the pin in uriel-agent's `deploy/compose.yaml` and in homelab-apps.

## Tools
| tool                     | groups | status |
|--------------------------|--------|--------|
| `homelab_status`         | admins | stub   |
| `door_camera_last_event` | family | stub   |
| `search_documents`       | family | hybrid search over opted-in Nextcloud folders |
| `ocr_now`                | family | reads one of the caller's scans now instead of overnight |
| `inspect_document`       | family | a document's form fields, signature fields, and whether edits can be saved |
| `fill_form`              | family | fills a PDF form (fields, or beside labels on flat forms) into an `edit_` copy |
| `edit_text`              | family | text changes and notes in Word/LibreOffice/text/PDF, saved as an `edit_` copy |
| `preview_signature`      | family | says where the caller's own signature image would go; saves nothing |
| `place_signature`        | family | places exactly that previewed signature, after the person's yes |
| `confirm_edit`           | family | marks an `edit_` copy as approved |
| `draft_schedule`, `create_schedule` | family | drafts recurring work for the caller, then creates it after a yes |
| `list_schedules`, `list_runs`, `cancel_schedule`, `set_timezone` | family | the caller's schedules, their runs, stopping one, their timezone |
| `web_search`             | family | SearXNG results (title, url, snippet), no page fetching |
| `claim_due_runs`, `finish_run` | uriel-internal | hidden: the gateway's runner claims and reports scheduled runs |
