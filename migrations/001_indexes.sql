-- 001_indexes.sql
-- Additive only: indexes to support date-ordered and month-ranged queries.
-- Safe to run against production; every statement is IF NOT EXISTS.
--
-- Apply with:
--   psql "$DATABASE_URL" -f migrations/001_indexes.sql

-- Both list views sort by `date DESC NULLS LAST, id DESC` on every page load.
CREATE INDEX IF NOT EXISTS idx_presales_date ON presales (date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_sold_date     ON sold (date DESC NULLS LAST);

-- Dashboard reads the activity feed ordered by recency.
CREATE INDEX IF NOT EXISTS idx_activities_created ON activities (created_at DESC);

-- Overdue-delivery count on the dashboard filters on this.
CREATE INDEX IF NOT EXISTS idx_sold_deli_last_date ON sold (deli_last_date)
    WHERE deli_last_date IS NOT NULL;

-- Links a sold order back to its originating lead.
CREATE INDEX IF NOT EXISTS idx_sold_presale_id ON sold (presale_id)
    WHERE presale_id IS NOT NULL;
