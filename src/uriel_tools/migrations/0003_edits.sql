-- 0.4.0: Uriel saves edits as new edit_ files next to the original, never changing an existing file.

-- From the share permissions: saving an edit_ file needs the create bit (4) on at least one share of the mount.
ALTER TABLE folders ADD COLUMN can_create boolean NOT NULL DEFAULT false;
-- A person's `Uriel` folder, shared privately: personal items (signature, ...) used only for its owner, never indexed.
ALTER TABLE folders ADD COLUMN personal boolean NOT NULL DEFAULT false;

CREATE TABLE edits (
  id bigserial PRIMARY KEY,
  folder_id bigint NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
  source_path text NOT NULL,
  output_path text NOT NULL,
  fileid bigint,
  etag text NOT NULL,  -- as Uriel wrote it; a different etag later means a person changed the draft
  tool text NOT NULL,
  caller text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'changed', 'confirmed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (folder_id, output_path)
);

-- One row per value placed or change made, so a later check (or a person's corrections) can be compared against it.
CREATE TABLE edit_items (
  id bigserial PRIMARY KEY,
  edit_id bigint NOT NULL REFERENCES edits (id) ON DELETE CASCADE,
  item text NOT NULL,  -- field name, label, anchor or the text that was replaced
  value text NOT NULL,
  page int,
  rect double precision[],  -- x0, y0, x1, y1 in PDF points, when placed on a page
  approximate boolean NOT NULL DEFAULT false
);
CREATE INDEX edit_items_edit ON edit_items (edit_id);
