-- 0.3.0: folders come from what is shared with the `uriel` Nextcloud account, not from sources.yaml.
-- Everything indexed so far was read with per-person app passwords under owner-side paths; it is dropped
-- and re-indexed from the shares (documents, chunks and jobs cascade).
DELETE FROM folders;

ALTER TABLE folders DROP COLUMN nextcloud_user, DROP COLUMN path, DROP COLUMN owner;
-- One row per mount in uriel's tree. Nextcloud mounts a node once even when it arrives through several
-- shares (to `uriel` and to `uriel-family`, or from two people), so owners and groups are unions.
ALTER TABLE folders
  ADD COLUMN mount text NOT NULL UNIQUE,
  ADD COLUMN owners text[] NOT NULL,
  ADD COLUMN shared_by text NOT NULL DEFAULT '';

CREATE INDEX folders_owners ON folders USING gin (owners);
