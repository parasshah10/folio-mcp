-- Helper function to immutably convert text array to string for search indexing
CREATE OR REPLACE FUNCTION immutable_array_to_string(arr TEXT[])
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN array_to_string(arr, ' ');
END;
$$;

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
        setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english'::regconfig, coalesce(content, '')), 'B') ||
        setweight(to_tsvector('english'::regconfig, coalesce(immutable_array_to_string(tags), '')), 'C')
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
DECLARE
    has_search BOOLEAN := (search_term IS NOT NULL AND search_term != '');
    is_multi_word BOOLEAN := (search_term ~ '\s+');
    query_and tsquery;
    query_or tsquery;
BEGIN
    IF has_search THEN
        query_and := websearch_to_tsquery('english', search_term);
        IF is_multi_word THEN
            SELECT string_agg(plainto_tsquery('english', word)::text, ' | ')::tsquery
            INTO query_or
            FROM unnest(regexp_split_to_array(trim(search_term), '\s+')) AS word
            WHERE word != '';
        END IF;
    END IF;

    RETURN QUERY
    SELECT
        combined.r_path,
        combined.r_title,
        combined.r_tags,
        combined.r_updated_at,
        combined.r_size_tokens,
        combined.r_score,
        combined.r_snippet
    FROM (
        WITH and_results AS (
            SELECT
                n.path AS r_path,
                n.title AS r_title,
                n.tags AS r_tags,
                n.updated_at AS r_updated_at,
                n.size_tokens AS r_size_tokens,
                CASE WHEN has_search
                    THEN ts_rank(n.search_vector, query_and)
                    ELSE 0.0
                END::REAL AS r_score,
                CASE WHEN has_search
                    THEN ts_headline('english', n.content, query_and,
                        'MaxFragments=1, MaxWords=30, MinWords=10, StartSel=**, StopSel=**')
                    ELSE left(n.content, 150)
                END AS r_snippet
            FROM notes n
            WHERE (NOT has_search OR n.search_vector @@ query_and)
                AND n.sync_status != 'pending_delete'
                AND (filter_folder IS NULL OR n.folder = filter_folder)
                AND (filter_tags IS NULL OR n.tags @> filter_tags)
                AND (filter_since IS NULL OR n.updated_at > filter_since)
        ),
        count_and AS (
            SELECT count(*) as cnt FROM and_results WHERE r_score > 0 OR NOT has_search
        ),
        or_results AS (
            SELECT
                n.path AS r_path,
                n.title AS r_title,
                n.tags AS r_tags,
                n.updated_at AS r_updated_at,
                n.size_tokens AS r_size_tokens,
                (ts_rank(n.search_vector, query_or) * 0.5)::REAL AS r_score,
                ts_headline('english', n.content, query_or,
                    'MaxFragments=1, MaxWords=30, MinWords=10, StartSel=**, StopSel=**') AS r_snippet
            FROM notes n
            WHERE has_search AND is_multi_word
                AND (SELECT cnt FROM count_and) = 0
                AND n.search_vector @@ query_or
                AND n.sync_status != 'pending_delete'
                AND (filter_folder IS NULL OR n.folder = filter_folder)
                AND (filter_tags IS NULL OR n.tags @> filter_tags)
                AND (filter_since IS NULL OR n.updated_at > filter_since)
        )
        SELECT * FROM and_results
        UNION ALL
        SELECT * FROM or_results
    ) combined
    ORDER BY
        CASE WHEN sort_by = 'recent' THEN extract(epoch FROM combined.r_updated_at) ELSE NULL END DESC NULLS LAST,
        CASE WHEN sort_by != 'recent' THEN combined.r_score ELSE NULL END DESC NULLS LAST,
        combined.r_updated_at DESC
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql STABLE;
