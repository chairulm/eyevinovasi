-- 001: core tables for cameras, health history and collector runs.
-- Plain Postgres (no extensions) so it installs anywhere; PostGIS/Timescale come later.

CREATE TABLE IF NOT EXISTS sources (
    id          text PRIMARY KEY,               -- medan | jogja | binamarga
    name        text NOT NULL,
    homepage    text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cameras (
    id            text PRIMARY KEY,             -- "<source>:<native id>", stable key
    source        text NOT NULL REFERENCES sources(id),
    name          text NOT NULL,
    lat           double precision,
    lon           double precision,
    geo_source    text NOT NULL DEFAULT 'source',   -- source | approx | none
    playback      text NOT NULL,                    -- hls | resolve | external
    stream_url    text,
    page_url      text,
    category      text NOT NULL DEFAULT '',
    area          text NOT NULL DEFAULT '',
    city          text NOT NULL DEFAULT '',
    meta          jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        text NOT NULL DEFAULT 'unknown',  -- online | offline | unknown
    last_checked  timestamptz,
    active        boolean NOT NULL DEFAULT true,    -- false = gone from source (soft delete)
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cameras_source_idx ON cameras (source) WHERE active;
CREATE INDEX IF NOT EXISTS cameras_latlon_idx ON cameras (lat, lon) WHERE active;

-- one row per health probe result change (not every probe) keeps this table small
CREATE TABLE IF NOT EXISTS camera_status_log (
    camera_id   text NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    at          timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL,
    PRIMARY KEY (camera_id, at)
);

CREATE TABLE IF NOT EXISTS collector_runs (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    mode        text,                           -- live | seed
    cameras     integer,
    added       integer,
    removed     integer,
    error       text
);
CREATE INDEX IF NOT EXISTS collector_runs_started_idx ON collector_runs (started_at DESC);

INSERT INTO sources (id, name, homepage) VALUES
  ('medan',     'ATCS Dishub Kota Medan',      'https://atcsdishub.medan.go.id/streaming'),
  ('jogja',     'Pemerintah Kota Yogyakarta',  'https://cctv.jogjakota.go.id/home'),
  ('binamarga', 'Ditjen Bina Marga PUPR',      'https://binamarga.pu.go.id/cctv-non-tol')
ON CONFLICT (id) DO NOTHING;
