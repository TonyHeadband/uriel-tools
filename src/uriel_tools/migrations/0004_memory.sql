-- 0.6.0: what Uriel knows about each person (USER.md) and how they want it to talk (SOUL.md), one row per note.
CREATE TABLE memory_notes (
  id bigserial PRIMARY KEY,
  owner text NOT NULL,  -- _meta.user
  file text NOT NULL CHECK (file IN ('user', 'soul')),
  section text NOT NULL,
  note text,  -- NULL on a skip row
  skipped boolean NOT NULL DEFAULT false,  -- the person skipped this section's questionnaire question
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (skipped = (note IS NULL))
);
CREATE INDEX memory_notes_owner ON memory_notes (owner, file);
