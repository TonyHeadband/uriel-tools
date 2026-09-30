-- 0.8.0: work Uriel runs for a person on a schedule, and its Nextcloud calendar mirror. Instants are UTC.
CREATE TABLE person_settings (
  owner text PRIMARY KEY,  -- _meta.user
  tz text NOT NULL DEFAULT 'America/Toronto',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE schedules (
  id bigserial PRIMARY KEY,
  owner text NOT NULL,  -- _meta.user, which is also the Nextcloud uid
  owner_sub text,
  title text NOT NULL,
  prompt text NOT NULL,
  rrule text NOT NULL,  -- RFC 5545, without DTSTART
  dtstart timestamp NOT NULL,  -- wall-clock, in tz
  tz text NOT NULL,
  tools text[] NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'finished')),
  next_run_at timestamptz,  -- null once finished or cancelled
  cancelled_by text CHECK (cancelled_by IN ('chat', 'nextcloud')),
  caldav_uid text UNIQUE NOT NULL,
  caldav_etag text,  -- the ETag Nextcloud gave Uriel's last PUT of the event: tells its own echo from an edit
  mirror_dirty boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX schedules_due ON schedules (next_run_at) WHERE status = 'active';

CREATE TABLE schedule_runs (
  id bigserial PRIMARY KEY,
  schedule_id bigint NOT NULL REFERENCES schedules(id),
  due_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'claimed'
    CHECK (status IN ('claimed', 'ok', 'failed', 'undelivered', 'skipped')),
  lease_until timestamptz,
  summary text,
  error text,
  talk_message_id bigint,
  finished_at timestamptz,
  mirror_dirty boolean NOT NULL DEFAULT false,
  UNIQUE (schedule_id, due_at)  -- one occurrence runs at most once
);

CREATE TABLE caldav_sync (
  owner text PRIMARY KEY,
  calendar_href text NOT NULL,
  tasks_href text NOT NULL,
  sync_token text
);
