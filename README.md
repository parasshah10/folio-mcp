# 📓 Folio MCP

A high-performance, Markdown-native working memory layer for AI companions, powered by a Supabase cache and bidirectional Notion sync.

## Architecture Overview

Folio is designed to be instantly responsive for the AI while maintaining a beautiful, human-readable UI in Notion. It achieves this using a **Cache-First Sync Engine**.

```text
AI ↔ Supabase (PostgreSQL)  <-- Instant read/write (~50ms)
            ↓
     Background Push        <-- Async native Markdown update
            ↓
       Notion API           <-- Human-readable UI
            ↓
     Background Pull        <-- Syncs human edits back to cache (10s interval)
```

## Features

- **2-Tool MCP Interface**: Exposes exactly what the AI needs: `folio` (CRUD) and `folio_search`.
- **Instant Reads & Writes**: All AI interactions hit the Supabase cache for near-zero latency.
- **Native Notion Markdown**: Uses the new 2025-09-03 API to write raw Markdown directly to Notion—perfect fidelity for tables, code, and lists.
- **Surgical Section Updates**: Editing a single section always takes ~5 API calls regardless of note size, using native ellipsis-based string replacement.
- **Full-Text Search**: Powered by PostgreSQL `tsvector` and GIN indexes, supporting exact phrases and web-search syntax.
- **Smart Errors**: If an AI targets a missing section, it returns the `available_sections`. If it creates a duplicate note, it returns the `existing` content. This saves valuable LLM round-trips.
- **Bidirectional Sync**: Background thread pulls Notion edits every 10 seconds and reconciles deletions every 2 minutes.
- **Organization**: Built-in support for folders, tags, and versioning/undo (on local backend).

## Setup & Configuration

Folio supports three deployment paths. Configure your chosen path via a `.env` file or environment variables.

### A. Supabase + Notion Sync (Recommended)
Provides instant AI response times with a synced Notion UI.
```env
FOLIO_BACKEND=supabase
FOLIO_SYNC=notion
FOLIO_SYNC_INTERVAL=10
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=eyJ... (Service role or anon key)
NOTION_API_KEY=ntn_...
NOTION_DATABASE_ID=abc123...
```

### B. Supabase Only
High-performance Postgres storage without the Notion UI.
```env
FOLIO_BACKEND=supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=eyJ...
```

### C. Local Filesystem Only
Simple markdown files backed by local Git versioning.
```env
FOLIO_BACKEND=local
FOLIO_LOCAL_ROOT=/path/to/your/notes
```

## Database Setup (Supabase)

If using the Supabase backend, you must initialize the database schema. Make sure your `SUPABASE_URL` and `SUPABASE_KEY` are set, then run:

```bash
python -m folio.migrate
```

This creates:
1. `notes` table: Stores path, content, tags, folder, and sync metadata.
2. `sync_state` table: Tracks the last sync and reconcile timestamps.
3. `search_notes` RPC: A Postgres function for ranking full-text search results.

## Tool Reference

### `folio` — Read and Write Notes
Performs targeted CRUD operations.

| Action | Description |
|--------|-------------|
| `create` | New note with path, content, tags. |
| `read` | Fetch full note or a specific section. |
| `update` | Modify content via `replace`, `append`, or `section` (surgical). |
| `delete` | Trash a note. |
| `move` | Change a note's path/folder. |
| `list` | Browse notes in a folder. |
| `undo` | Revert to previous version (local backend only). |

**Examples:**
- `action='create', path='projects/folio.md', content='# Folio\n...'`
- `action='update', path='projects/folio.md', mode='section', target='Status', content='All tests passing.'`

### `folio_search` — Find Notes
Search by content, tags, folder, or recency. Results are ranked by relevance.

**Examples:**
- `query='Postgres architecture', folder='tech'`
- `query='birthday', tags=['person']`
- `query='illustration', updated_since='7d', sort='recent'`

## Notion API Budget

Folio's background sync is designed to be highly respectful of Notion's rate limits (10,800 requests/hour).

- **Pull Sync (10s interval)**: 1 call per cycle = 360 calls/hour
- **Reconcile (120s interval)**: 1 call per cycle = 30 calls/hour
- **Total Baseline**: ~390 calls/hour (**~3.6%** of Notion's hourly limit)

Updates (even to massive notes) use a highly optimized surgical Markdown strategy that guarantees a constant **~5 API calls per update**, preventing rate limit exhaustion during heavy AI collaboration.

## Project Structure

```
src/folio/
├── __init__.py
├── server.py             # FastMCP tool definitions + routing
├── models.py             # Pydantic data models
├── config.py             # Environment configuration
├── sections.py           # Markdown heading/section parser
├── sync.py               # Background SyncEngine
├── migrate.py            # Supabase schema migration script
├── backends/
│   ├── __init__.py
│   ├── local.py          # Local FS + Git versioning
│   ├── notion.py         # Direct Notion API (legacy/fallback)
│   └── supabase.py       # Cache-first Postgres storage
└── sync_adapters/
    └── notion.py         # Notion sync adapter for the SyncEngine
```

## License

MIT
