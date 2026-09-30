-- In the cluster and compose the extension is created by the superuser init script; this is a no-op there.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE folders (
  id bigserial PRIMARY KEY,
  owner text NOT NULL,
  nextcloud_user text NOT NULL,
  path text NOT NULL,
  share_with text[] NOT NULL DEFAULT '{}',
  state text NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'auth_failed')),
  UNIQUE (nextcloud_user, path)
);

CREATE TABLE documents (
  id bigserial PRIMARY KEY,
  folder_id bigint NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
  path text NOT NULL,
  fileid bigint,
  etag text NOT NULL DEFAULT '',
  mime text NOT NULL DEFAULT '',
  size bigint NOT NULL DEFAULT 0,
  modified timestamptz,
  status text NOT NULL CHECK (status IN ('indexed', 'awaiting_ocr', 'failed', 'skipped')),
  error text,
  indexed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (folder_id, path)
);

CREATE TABLE chunks (
  id bigserial PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
  ord int NOT NULL,
  page int NOT NULL,
  source text NOT NULL CHECK (source IN ('text', 'ocr')),
  text text NOT NULL,
  embedding vector(1024) NOT NULL,
  embed_model text NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);
CREATE INDEX chunks_document ON chunks (document_id);
CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('index', 'ocr', 'delete', 'reconcile')),
  folder_id bigint NOT NULL REFERENCES folders (id) ON DELETE CASCADE,
  path text NOT NULL DEFAULT '',
  forced boolean NOT NULL DEFAULT false,
  run_after timestamptz NOT NULL DEFAULT now(),
  attempts int NOT NULL DEFAULT 0,
  last_error text,
  locked_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- Pending work is unique per file: a burst of webhooks collapses into one job.
CREATE UNIQUE INDEX jobs_pending_unique ON jobs (kind, folder_id, path) WHERE locked_until IS NULL;
CREATE INDEX jobs_due ON jobs (run_after) WHERE locked_until IS NULL;
