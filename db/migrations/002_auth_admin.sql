-- 002: user accounts, sessions, manual camera overrides, audit log.

CREATE TABLE IF NOT EXISTS users (
    id             serial PRIMARY KEY,
    username       text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9._-]{3,32}$'),
    password_hash  text NOT NULL,                    -- scrypt$N$r$p$salt$hash
    role           text NOT NULL CHECK (role IN ('viewer', 'admin')),
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login     timestamptz
);

-- server-side sessions: the cookie holds a random token, the DB only its SHA-256
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  text PRIMARY KEY,
    user_id     integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    ip          text,
    user_agent  text
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions (expires_at);

-- admin corrections that survive every scrape (applied when cameras are read)
CREATE TABLE IF NOT EXISTS camera_overrides (
    camera_id   text PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
    hidden      boolean NOT NULL DEFAULT false,
    name        text,
    lat         double precision,
    lon         double precision,
    note        text,
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((lat IS NULL) = (lon IS NULL))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    username  text,                 -- kept as text so history survives user deletion
    action    text NOT NULL,        -- login, login_failed, logout, user_create, ...
    target    text,
    detail    jsonb NOT NULL DEFAULT '{}'::jsonb,
    ip        text
);
CREATE INDEX IF NOT EXISTS audit_at_idx ON audit_log (at DESC);
