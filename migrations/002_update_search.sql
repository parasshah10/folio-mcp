-- Search RPC function (called from Python via supabase.rpc)
CREATE OR REPLACE FUNCTION search_notes(
    search_term TEXT,
    filter_folder TEXT DEFAULT NULL,
    filter_tags TEXT[] DEFAULT NULL,
    filter_since TIMESTAMPTZ DEFAULT NULL,
    sort_by TEXT DEFAULT 'relevance',
    max_results INT DEFAULT 10,
    page_offset INT DEFAULT 0
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
            WHERE word != ''
                AND plainto_tsquery('english', word)::text != '';
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
        or_results AS (
            SELECT
                n.path AS r_path,
                n.title AS r_title,
                n.tags AS r_tags,
                n.updated_at AS r_updated_at,
                n.size_tokens AS r_size_tokens,
                (ts_rank(n.search_vector, query_or) * 0.3)::REAL AS r_score,
                ts_headline('english', n.content, query_or,
                    'MaxFragments=1, MaxWords=30, MinWords=10, StartSel=**, StopSel=**') AS r_snippet
            FROM notes n
            WHERE has_search AND is_multi_word
                AND query_or IS NOT NULL
                AND n.search_vector @@ query_or
                AND NOT (n.search_vector @@ query_and) -- Exclude exact matches to prevent duplicates
                AND n.sync_status != 'pending_delete'
                AND (filter_folder IS NULL OR n.folder = filter_folder)
                AND (filter_tags IS NULL OR n.tags @> filter_tags)
                AND (filter_since IS NULL OR n.updated_at > filter_since)
        )
        SELECT * FROM and_results
        UNION ALL
        SELECT * FROM or_results WHERE r_score > 0.1 -- Filter garbage OR results
    ) combined
    ORDER BY
        CASE WHEN sort_by = 'recent' THEN extract(epoch FROM combined.r_updated_at) ELSE NULL END DESC NULLS LAST,
        CASE WHEN sort_by != 'recent' THEN combined.r_score ELSE NULL END DESC NULLS LAST,
        combined.r_updated_at DESC
    LIMIT max_results
    OFFSET page_offset;
END;
$$ LANGUAGE plpgsql STABLE;
