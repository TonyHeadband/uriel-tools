-- 0.9.0: people's own Nextcloud calendars. Which one "mine" means, and what Uriel just added (for undo).
ALTER TABLE person_settings ADD COLUMN default_calendar text;  -- a calendar href in uriel's home, or NULL

CREATE TABLE calendar_created (
  owner text NOT NULL,  -- _meta.user
  item_path text NOT NULL,  -- the item's href
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner, item_path)
);
