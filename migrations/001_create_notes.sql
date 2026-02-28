-- Notes table
CREATE TABLE notes (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    tags            TEXT[] DEFAULT '{}',
    folder          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    size_tokens     INT DEFAULT 0,
    metadata        JSONB DEFAULT '{}',

    -- Sync fields (platform-agnostic)
    external_id         TEXT,
    external_edited_at  TIMESTAMPTZ,
    sync_status         TEXT DEFAULT 'synced',
    last_synced_at      TIMESTAMPTZ DEFAULT NOW(),

    -- Full-text search vector (auto-generated, weighted)
    search_vector   TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(array_to_string(tags, ' '), '')), 'C')
    ) STORED
);

CREATE INDEX idx_notes_search       ON notes USING GIN (search_vector);
CREATE INDEX idx_notes_path         ON notes (path);
CREATE INDEX idx_notes_folder       ON notes (folder);
CREATE INDEX idx_notes_tags         ON notes USING GIN (tags);
CREATE INDEX idx_notes_updated      ON notes (updated_at DESC);
CREATE INDEX idx_notes_sync_pending ON notes (sync_status) WHERE sync_status != 'synced';
CREATE UNIQUE INDEX idx_notes_external_id ON notes (external_id) WHERE external_id IS NOT NULL;

-- Sync state singleton
CREATE TABLE sync_state (
    id                  INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_sync_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_reconciled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO sync_state (id) VALUES (1);

-- Search RPC function (called from Python via supabase.rpc)
CREATE OR REPLACE FUNCTION search_notes(
    search_term TEXT,
    filter_folder TEXT DEFAULT NULL,
    filter_tags TEXT[] DEFAULT NULL,
    filter_since TIMESTAMPTZ DEFAULT NULL,
    sort_by TEXT DEFAULT 'relevance',
    max_results INT DEFAULT 10
)
RETURNS TABLE (
    path TEXT,
    title TEXT,
    tags TEXT[],
    updated_at TIMESTAMPTZ,
    size_tokens INT,
    score REAL,
    snippet TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        n.path,
        n.title,
        n.tags,
        n.updated_at,
        n.size_tokens,
        ts_rank(n.search_vector, q)::REAL AS score,
        ts_headline('english', n.content, q,
            'MaxFragments=1, MaxWords=30, MinWords=10, StartSel=**, StopSel=**'
        ) AS snippet
    FROM notes n,
        plainto_tsquery('english', search_term) q
    WHERE n.search_vector @@ q
        AND n.sync_status != 'pending_delete'
        AND (filter_folder IS NULL OR n.folder = filter_folder)
        AND (filter_tags IS NULL OR n.tags @> filter_tags)
        AND (filter_since IS NULL OR n.updated_at > filter_since)
    ORDER BY
        CASE WHEN sort_by = 'recent' THEN extract(epoch FROM n.updated_at) ELSE NULL END DESC NULLS LAST,
        CASE WHEN sort_by != 'recent' THEN ts_rank(n.search_vector, q) ELSE NULL END DESC NULLS LAST
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql STABLE;
