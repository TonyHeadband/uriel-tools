# Opt in by sharing with `uriel`

Status: implemented in 0.3.0 (2026-09-28), a breaking config change. Sections marked *Amended* changed during
implementation. Replaces the `folders:` list in `sources.yaml` and the per-person app passwords from
[2026-09-27-document-rag-design.md](2026-09-27-document-rag-design.md).

## Why
Today a folder is opted in by editing `sources.yaml`. The indexer then reads it with the owner's app password,
which grants access to that person's whole account. Two problems follow:
- only someone with access to the deployment can opt a folder in or out;
- the indexer holds full-account credentials for every family member.

Nextcloud already has a consent gesture everyone knows: **sharing**. If a folder is shared with a service account,
the owner decides in the Nextcloud UI what Uriel may read, and can revoke it the same way. The indexer then needs
exactly one credential, and that credential can only read what was shared with it.

## Verified facts (2026-09-28, cloud.example.com)
- **Service account:**
  - the Nextcloud user `uriel` exists; it is not an admin and belongs to no groups;
  - its login password and an app password ("app password (uriel-tools indexer)") are in Vaultwarden, folder
    `Homelab`, item "Nextcloud: uriel".
- **First share:** `/Documents` is shared by `anthony-headband` with `uriel`, read-only (share id 5, `permissions: 1`).
- **Share API:**
  - Auto-accept is on. `GET /ocs/v2.php/apps/files_sharing/api/v1/shares?shared_with_me=true` as `uriel` lists
    the share immediately, and `shares/pending` is empty.
  - The listing has `id`, `share_type` (0 user, 1 group), `share_with`, `uid_owner`, `uid_file_owner`,
    `displayname_owner`, `path` / `file_target` (the mount point in `uriel`'s tree), `item_source` (the shared
    folder's file id) and `permissions`.
- **Mount names:**
  - The mount name is not the owner's folder name when it clashes. New accounts get starter files including
    their own `Documents/`, so the share mounted as `/Documents (2)`.
  - Two owners sharing folders with the same name will also clash.
- **File ids:** they are the same through the share. In `uriel`'s tree, `/Documents (2)/Marriage/SJ-217A….pdf` has
  `oc:fileid` 1099, so `https://cloud.example.com/f/1099` links keep working.
- **Per-file WebDAV properties:**
  - `oc:owner-id` is the owner (`anthony-headband`);
  - `oc:permissions` is `SG` (shared, readable; no write).
- **Groups:**
  - sociallogin now copies the Authelia `groups` claim into Nextcloud groups at each login;
  - the group `family` exists, and `anthony-headband` (a local, non-sociallogin account) was added to it by hand.
- **Admin API limits:** admin provisioning calls (create a user or group, change membership) with an app password
  fail with `403 Password confirmation is required`. Those steps stay manual in the admin UI, and the indexer
  never needs them.

## Design

### Opting in
| Owner action in Nextcloud | Who can search it in Uriel |
|---|---|
| Share a folder with the user **`uriel`** | the owner only |
| Share a folder with the group **`uriel-<group>`** (e.g. `uriel-family`) | the owner plus every member of the Authelia group `<group>` |
| Unshare | nobody; its documents are deleted from the index |

- `uriel-<group>` groups contain **only `uriel`**. An admin creates them in the Nextcloud admin UI (`uriel-family`
  first).
- `uriel` is deliberately **not** added to `family` or any other real group. Otherwise every folder shared with the
  family for unrelated reasons would be indexed silently, and indexing must stay a deliberate act.
- The group prefix is configurable (`sources.yaml: share_group_prefix`, default `uriel-`). A group share whose
  name lacks the prefix is ignored and logged once.
- **Links and group shares:** a document found through `uriel-family` is searchable by the family, but its `/f/`
  link only opens for people Nextcloud itself lets in. To make the links open too, the owner also shares the folder
  with `family`. The `search_documents` description tells the model this, so it does not promise a link will open.
- **Reshares:** when someone reshares a folder they received, the sharer (`uid_owner`) is the owner for
  visibility. Nextcloud already let them read it and reshare it, so Uriel grants nothing Nextcloud didn't.

### Identity: from Nextcloud user to Uriel user
The `folders.owner` column stores the Nextcloud uid (`uid_owner`). The search filter matches the caller against a
set of ids:
- **The caller's username** (`_meta.user`, the Authelia `preferred_username`). Local Nextcloud accounts such as
  `anthony-headband` match directly.
- **`authelia-<sub>`.** sociallogin names the accounts it creates after the Authelia OIDC subject (Lil-B's is
  `authelia-19e1429e-…`). The gateway already receives `sub` in the ID token and will forward it as the new
  optional **`_meta.sub`** (an additive contract change).

```sql
WHERE f.owner = ANY(%(ids)s) OR f.share_with && %(groups)s::text[]
```

Authelia gives every client the same subject by default. Pairwise subjects are not configured for the `uriel` or
`nextcloud` clients, so the two `sub` values should match; this is verified on Lil-B's first Uriel login (see
Rollout). If they ever differ, `sources.yaml: aliases: {authelia-19e1…: lil-b}` is the fallback, but it is not built
unless needed.

### Discovery
- **Polling:**
  - the indexer polls `shares?shared_with_me=true` every `share_poll_minutes` (default 5), plus once at startup;
  - it also accepts anything in `shares/pending`, in case auto-accept is ever turned off, since a share with `uriel`
    is itself the consent.
- **Reacting to changes:**
  - a new share adds a folder row and a reconcile job;
  - a changed share (permissions, `share_with`) updates the row;
  - a share that disappears gets a delete job for all its documents, and its folder row is then removed.
- **Why polling is enough:** revocation takes effect within one poll interval (5 min). Nextcloud's share webhooks
  (`ShareCreatedEvent` / `ShareDeletedEvent`) could trigger an immediate poll later, but polling alone is correct
  and simple.
- **Starter files:** `uriel`'s own starter files are never indexed, because only share mounts are walked. They are
  deleted once anyway (Rollout), so fewer mounts get a ` (2)` suffix.

### File access
- **One credential:** `URIEL_NC_USER=uriel` and `URIEL_NC_APP_PASSWORD`. The per-person
  `URIEL_NC_APP_PASSWORD_<USER>` variables and `sources.py`'s `password_env` / `app_password` are removed.
- **Paths:** document paths are `uriel`-side, e.g. `/Documents (2)/Marriage/x.pdf`. *Amended:* a search hit's
  `folder` is the mount path as is (it is also what the `folder` filter takes), and `shared_by` names the sharers
  when the caller is not one of them. Stripping the ` (n)` suffix would make two owners' `Documents` look alike.
- **Folder state:** `auth_failed` stays, with a new meaning. A 401 means the `uriel` app password was revoked, which
  pauses every folder, and it is logged loudly.

### Webhooks: re-check the sharer's folders (*Amended*)
- **The problem:** Nextcloud's node events carry the owner's path (`/<owner>/files/Documents/…`), which doesn't
  exist in `uriel`'s tree.
- **Planned:** resolve `node.id` to `uriel`'s path with a WebDAV `SEARCH`, and look deletions up by
  `documents.fileid`.
- **Built instead:** an event from an owner who shares folders with `uriel` queues a reconcile of each of those
  folders. The reconcile finds what changed by etag, covering writes, deletions and renames with the code that
  already handles missed events. Events from anyone else produce nothing.
- **Why:**
  - the events process needs no Nextcloud credential and no per-event request;
  - there is no second path that could disagree with the reconcile.
- **Cost:** a full walk of the sharer's folders per burst. The reconcile job is queued `URIEL_EVENTS_SETTLE_S`
  (default 60) in the future, and pending jobs are unique per folder, so an autosaving editor produces one walk a
  minute, not one per save.
- **Revisit:** if shared trees grow to thousands of directories, prune the walk by directory etag or add the
  `SEARCH` path.
- **Unchanged:** the nightly reconcile stays the safety net for missed events.

### Configuration after the change
```yaml
# sources.yaml
ocr_window: { start: "01:00", end: "06:00", tz: Europe/Rome }
reconcile_at: "00:30"
share_poll_minutes: 5
share_group_prefix: uriel-
```
- **Env:** `URIEL_NC_URL`, `URIEL_NC_PUBLIC_URL`, `URIEL_NC_USER` (default `uriel`), `URIEL_NC_APP_PASSWORD`,
  `URIEL_EVENTS_SETTLE_S`. A leftover `folders:` key in `sources.yaml` fails at startup with a pointer to sharing.
- **Cluster:** the app password becomes one SealedSecret key.
- **Local compose:** the app password goes in `.env`, and `config/sources.local.yaml` is no longer needed.

### Data model: migration `0002_shares.sql` (*Amended*)
- **Folders are keyed by mount,** not by share: `mount text UNIQUE`, `owners text[]` (Nextcloud uids),
  `shared_by text` (display names), `share_with text[]` and `state`. `owner`, `nextcloud_user` and `path` are
  dropped.
- **Why:** Nextcloud mounts a node once even when it reaches `uriel` through several shares (to `uriel` and to
  `uriel-family`, or from two people after a reshare). The listing then has several shares with the same path, and
  they merge into one folder with the union of owners and groups.
- **Documents** need no `fileid` index, since webhooks no longer look files up by id.
- **Existing rows** are deleted, so documents, chunks and jobs cascade away, and everything is re-indexed from the
  shares. There is no cluster index yet. Locally the re-index takes about 10 minutes of embedding, plus OCR in the
  next night window.

### Contract changes (docs/contract.md)
- **Additive:** `_meta.sub` (optional string, the caller's OIDC subject). uriel-agent sends it from the ID token.
- **Changed wording:** `search_documents` visibility becomes "folders you shared with Uriel, and folders shared with
  a `uriel-<group>` group you belong to". `ocr_now(path)` takes the path exactly as returned in a hit.
- **Version:** 0.3.0. The tool interface doesn't break, but the deployment config does (removed `folders:` and
  per-user env).

## Security
- **What leaks if the account is compromised:** everything shared with `uriel`, read-only. Today it's every
  configured person's full account. This is the main reason for the change.
- **Account setup:**
  - `uriel` has no groups apart from `uriel-*` and is not an admin;
  - its login password is used only to mint the app password and is not deployed;
  - its app password can be revoked in its Security settings without touching anyone else.
- **Access checks:** the search filter still runs in SQL. `_meta.sub` is trusted for the same reason `_meta.user`
  is: the API key plus the NetworkPolicy.

## Testing
- **Unit:**
  - share listing to folder rows (user share, `uriel-` group share, unprefixed group ignored, reshare);
  - owner id matching with and without `sub`;
  - stripping the ` (n)` suffix from mount names.
- **Fakes:** the fake WebDAV server gains the OCS `shares` / `shares/pending` endpoints and `SEARCH` by file id.
- **DB:**
  - a share removed means its documents are deleted;
  - an event resolved by file id means an index job for the right folder;
  - a delete event is found through `documents.fileid`.
- **e2e:** two owners share folders with the same name, one through a `uriel-family` share. The family member
  sees both, and a non-member sees only their own.
- **Live:** the real share 5 (`/Documents (2)`) indexes fully, and "questionario matrimonio" as `anthony-headband`
  still returns `/f/1099`.

## Rollout
1. Implement on a branch in uriel-tools and release 0.3.0.
2. uriel-agent:
   - forward `sub` in `_meta`;
   - pin 0.3.0;
   - compose uses `URIEL_NC_USER` / `URIEL_NC_APP_PASSWORD`;
   - remove `config/sources.local.yaml` and `URIEL_NC_APP_PASSWORD_ANTHONY_HEADBAND` from `.env`.
3. The user revokes the old `anthony-headband` app password in Nextcloud → Settings → Security.
4. Admin UI:
   - create the group `uriel-family` and add `uriel`;
   - delete `uriel`'s starter files, which needs one login as `uriel` (password in Vaultwarden) or a WebDAV DELETE
     with its app password.
5. Verify Lil-B's `sub`: after her first Uriel login, check that the gateway's `sub` equals `19e1429e-…`.
   Then she shares a folder with `uriel` and searches for something in it.

## Not in this change
- Nextcloud share webhooks for instant opt-in (polling covers it).
- Walking only changed directories during reconcile.
- Renaming Lil-B's Nextcloud account to `lil-b` (not needed; see the identity section).
