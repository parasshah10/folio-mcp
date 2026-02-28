# 📓 Folio MCP

A high-performance, Markdown-native working memory layer for AI companions, powered by a Supabase cache and bidirectional Notion sync.

**Why Folio exists:** While vector databases are great for "episodic memory" (past conversations), AI companions also need "working memory"—mutable, organized state like scratchpads, project plans, and user profiles. Folio gives your AI the ability to effortlessly read, write, and search structured Markdown notes without hallucinating paths or waiting on slow API calls.

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

## Quick Start & Setup

### Prerequisites
- Python 3.11+
- [Notion Integration](https://www.notion.so/my-integrations) (if using Notion)
- [Supabase Project](https://supabase.com/) (if using Supabase)

### 1. Install
```bash
git clone https://github.com/yourname/folio-mcp.git
cd folio-mcp
pip install -e .
```

### 2. Configure Notion (Optional but Recommended)
1. Go to [Notion Integrations](https://www.notion.so/my-integrations) and create an Internal Integration. Copy the **Internal Integration Secret**.
2. Create a new Notion Database. Add these properties: `title` (Name), `tags` (Multi-select), `folder` (Text).
3. Connect your integration to the database (via the 3-dot menu).
4. Copy the **Database ID** from the URL.

### 3. Choose Your Path (.env)

**A. Supabase + Notion Sync (Recommended)**
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

**B. Supabase Only**
High-performance Postgres storage without the Notion UI.
```env
FOLIO_BACKEND=supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=eyJ...
```

**C. Direct Notion (No Cache)**
Simplest cloud setup. Note: Tradeoff is latency (~1-2s per operation instead of ~50ms).
```env
FOLIO_BACKEND=notion
NOTION_API_KEY=ntn_...
NOTION_DATABASE_ID=abc123...
```

**D. Local Filesystem**
Simple offline markdown files backed by local Git versioning.
```env
FOLIO_BACKEND=local
FOLIO_LOCAL_ROOT=/path/to/your/notes
```

### 4. Database Setup (Supabase Paths Only)
If using Supabase, initialize the schema:
```bash
python -m folio.migrate
```
*This creates the `notes` table, `sync_state` table, and `search_notes` RPC function.*

### 5. Connect to your AI
Add to your MCP client config (e.g., `claude_desktop_config.json`, Cursor, etc.):
```json
{
  "mcpServers": {
    "folio": {
      "command": "folio-mcp",
      "env": {
        "FOLIO_BACKEND": "supabase",
        "FOLIO_SYNC": "notion",
        "SUPABASE_URL": "...",
        "SUPABASE_KEY": "...",
        "NOTION_API_KEY": "...",
        "NOTION_DATABASE_ID": "..."
      }
    }
  }
}
```

## Performance & Latency

Folio's Supabase-first architecture ensures the AI never waits for Notion.

| Operation | Latency | Details |
| :--- | :--- | :--- |
| **Read / Search** | **~50ms** | Fetched instantly from Postgres indexes. |
| **Create / Update** | **~50ms** | Saved to Supabase instantly; Notion sync runs in background. |
| **Direct Notion Read** | ~1-2s | *(Only if using the Direct Notion backend)* |

## Tool Reference

### `folio` — Read and Write Notes
Performs targeted CRUD operations.

**Examples (JSON syntax):**
```json
{
  "action": "create",
  "path": "projects/folio.md",
  "content": "# Folio\n..."
}
```

```json
{
  "action": "update",
  "path": "projects/folio.md",
  "mode": "section",
  "target": "Status",
  "content": "All tests passing."
}
```

```json
{
  "action": "read",
  "path": "projects/folio.md"
}
```

### `folio_search` — Find Notes
Search by content, tags, folder, or recency. Results are ranked by relevance.

**Examples (JSON syntax):**
```json
{
  "query": "Postgres architecture",
  "folder": "tech"
}
```

```json
{
  "query": "birthday",
  "tags": ["person"]
}
```

## Environment Variable Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FOLIO_BACKEND` | No | `local` | `local`, `notion`, or `supabase`. |
| `FOLIO_LOCAL_ROOT` | Local | `~/.folio/notes` | Directory for local markdown files. |
| `NOTION_API_KEY` | Notion | — | Notion internal integration secret. |
| `NOTION_DATABASE_ID` | Notion | — | Target Notion database ID. |
| `SUPABASE_URL` | Supabase| — | Supabase project URL. |
| `SUPABASE_KEY` | Supabase| — | Supabase Service Role or Anon key. |
| `FOLIO_SYNC` | No | `none` | Background sync target (e.g., `notion`). |
| `FOLIO_SYNC_INTERVAL` | No | `30` | Seconds between sync pulls (Default: 30, recommended: 10). |

## Notion API Budget

Folio's background sync is highly respectful of Notion's rate limits (10,800 requests/hour).
- **Pull Sync (10s interval)**: 1 call per cycle = 360 calls/hour
- **Reconcile (120s interval)**: 1 call per cycle = 30 calls/hour
- **Total Baseline**: ~390 calls/hour (**~3.6%** of Notion's hourly limit)

Updates use an optimized surgical Markdown strategy guaranteeing a constant **~5 API calls per update**, preventing rate limit exhaustion during heavy AI collaboration.

## Known Limitations
- **Undo**: The `undo` action currently only works on the `local` (Git) backend.
- **Deletion Delay**: If you delete a note in Notion, it may take up to 2 minutes (the reconcile interval) to disappear from the Folio cache.
- **Zombie ID**: If you delete a note in Notion and immediately recreate a note with the exact same name/path before the reconcile sync runs, it can occasionally cause sync oscillations or duplicate entries.

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
│   ├── notion.py         # Direct Notion API
│   └── supabase.py       # Cache-first Postgres storage
└── sync_adapters/
    └── notion.py         # Notion sync adapter for the SyncEngine
```

## License

MIT
