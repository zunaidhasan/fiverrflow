-- 000_baseline.sql
-- Reverse-engineered from the LIVE Supabase database on 2026-08-04.
--
-- This documents existing state so the schema is reproducible from scratch.
-- DO NOT run this against the production database — it already has these
-- objects and holds real data. Every statement is guarded with IF NOT EXISTS
-- so an accidental run is a no-op rather than a loss.
--
-- Apply order: 000_baseline.sql -> 001_*.sql -> 002_*.sql ...

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    color      TEXT DEFAULT '#6b7280',
    is_won     BOOLEAN DEFAULT FALSE,
    is_lost    BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS presales (
    id                BIGSERIAL PRIMARY KEY,
    date              DATE,
    shift             TEXT,
    source            TEXT,
    url               TEXT,
    client_username   TEXT,
    profile_name      TEXT,
    category          TEXT,
    quoted_amount     NUMERIC DEFAULT 0,
    status            TEXT DEFAULT 'New',
    stage_key         TEXT DEFAULT 'lead',
    first_followup    BOOLEAN DEFAULT FALSE,
    second_followup   BOOLEAN DEFAULT FALSE,
    checked_by        TEXT,
    screenshot_reason TEXT,
    remarks           TEXT,
    custom_data       JSONB DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sold (
    id             BIGSERIAL PRIMARY KEY,
    date           DATE,
    account        TEXT,
    service_type   TEXT,
    order_id       TEXT,
    project_name   TEXT,
    client_name    TEXT,
    status         TEXT DEFAULT 'WIP',
    assign_leader  TEXT,
    developer      TEXT,
    deli_last_date DATE,
    order_amount   NUMERIC DEFAULT 0,
    bonus_amount   NUMERIC DEFAULT 0,
    sheet_link     TEXT,
    comment        TEXT,
    presale_id     BIGINT REFERENCES presales(id),
    custom_data    JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- NOTE: presale_id/sold_id carry no FK constraints in the live database.
-- Left as-is here to match reality; 002 adds them safely.
CREATE TABLE IF NOT EXISTS activities (
    id          BIGSERIAL PRIMARY KEY,
    type        TEXT,
    description TEXT,
    presale_id  BIGINT,
    sold_id     BIGINT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS custom_fields (
    id         BIGSERIAL PRIMARY KEY,
    entity     TEXT NOT NULL,
    field_key  TEXT NOT NULL,
    label      TEXT NOT NULL,
    field_type TEXT DEFAULT 'text',
    options    JSONB,
    sort_order INTEGER DEFAULT 0,
    UNIQUE (entity, field_key)
);

CREATE TABLE IF NOT EXISTS invitations (
    id         BIGSERIAL PRIMARY KEY,
    email      TEXT NOT NULL,
    token      TEXT NOT NULL UNIQUE,
    role       TEXT DEFAULT 'member',
    invited_by BIGINT REFERENCES users(id),
    used       BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

-- Indexes present in the live database at baseline time.
CREATE INDEX IF NOT EXISTS idx_presales_status ON presales (status);
CREATE INDEX IF NOT EXISTS idx_presales_stage  ON presales (stage_key);
CREATE INDEX IF NOT EXISTS idx_sold_status     ON sold (status);
CREATE INDEX IF NOT EXISTS idx_invitations_token ON invitations (token);
