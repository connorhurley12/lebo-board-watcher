-- ============================================================
-- 001_initial_schema.sql
-- Supabase schema for Lebo Board Watch persistence layer.
-- Run in Supabase SQL Editor or via `psql`.
-- ============================================================

-- Officials: commissioners, directors, board members
CREATE TABLE IF NOT EXISTS officials (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name        TEXT NOT NULL,
    body        TEXT NOT NULL,
    role        TEXT DEFAULT 'member',
    first_seen  TIMESTAMPTZ DEFAULT now(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(name, body)
);

-- Meetings: one row per meeting processed by the pipeline
CREATE TABLE IF NOT EXISTS meetings (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    meeting_date    DATE NOT NULL,
    body            TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'transcript',
    youtube_url     TEXT,
    extract_text    TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(meeting_date, body)
);

CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(meeting_date DESC);

-- Votes: one row per formal vote taken
CREATE TABLE IF NOT EXISTS votes (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    meeting_id      UUID NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    motion          TEXT NOT NULL,
    result          TEXT NOT NULL,
    unanimous       BOOLEAN NOT NULL DEFAULT true,
    yes_names       TEXT[] DEFAULT '{}',
    no_names        TEXT[] DEFAULT '{}',
    abstain_names   TEXT[] DEFAULT '{}',
    context         TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_votes_meeting ON votes(meeting_id);
CREATE INDEX IF NOT EXISTS idx_votes_unanimous ON votes(unanimous);

-- Spending items: one row per expenditure, contract, or bill list entry
CREATE TABLE IF NOT EXISTS spending_items (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    meeting_id      UUID NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL,
    amount          NUMERIC(14,2) NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'routine',
    project         TEXT,
    budget_line     TEXT,
    fiscal_year     INTEGER,
    contract_term   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spending_meeting ON spending_items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_spending_vendor ON spending_items(vendor);
CREATE INDEX IF NOT EXISTS idx_spending_amount ON spending_items(amount DESC);

-- Newsletters: one row per published weekly digest
CREATE TABLE IF NOT EXISTS newsletters (
    id               UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    week_of          DATE NOT NULL,
    title            TEXT NOT NULL,
    markdown_content TEXT NOT NULL,
    ghost_post_id    TEXT,
    ghost_post_url   TEXT,
    meeting_ids      UUID[] DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(week_of)
);

CREATE INDEX IF NOT EXISTS idx_newsletters_week ON newsletters(week_of DESC);
