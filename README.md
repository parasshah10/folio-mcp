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

## Why Folio over the Official Notion MCP?

The official [Notion MCP](https://github.com/makenotion/notion-mcp-server) is an enterprise integration layer — it wraps Notion's full API so an LLM can manage workspaces, databases, comments, users, and teams. Folio is a purpose-built companion memory layer. They solve fundamentally different problems.

### Token Tax: The Hidden Cost

Every MCP tool definition is injected into the LLM's context on **every turn**. This is a permanent tax on attention and cost. We tokenized both tool surfaces using OpenAI's tokenizer:

| | Tools | Total Tokens / Turn |
|---|---|---|
| **Notion MCP** | 12 | **11,300** |
| **Folio** | 2 | **1,216** |
| **Ratio** | 6x more tools | **9.3x heavier** |

#### Cumulative cost over a conversation:

| Turns | Notion MCP | Folio | Tokens Saved |
|---|---|---|---|
| 10 | 113,000 | 12,160 | **100,840** |
| 50 | 565,000 | 60,800 | **504,200** |
| 100 | 1,130,000 | 121,600 | **~1,000,000** |

Over a 100-turn companion conversation, Folio saves roughly **one million input tokens**. Beyond cost, this matters for attention quality — an LLM holding 11,300 tokens of SQL DDL schemas, UUID formatting rules, and team permission logic on every turn is measurably worse at its actual job: being a good companion.

### Tool-by-Tool Breakdown

| Notion MCP Tool | Tokens | Folio Equivalent | Tokens |
|---|---|---|---|
| `notion-search` | 1,242 | `folio_search` | 447 |
| `notion-fetch` | 475 | `folio(action='read')` | included |
| `notion-create-pages` | 1,853 | `folio(action='create')` | included |
| `notion-update-page` | 1,856 | `folio(action='update')` | included |
| `notion-move-pages` | 664 | `folio(action='move')` | included |
| `notion-duplicate-page` | 174 | `read` → `create` (2 calls) | included |
| `notion-create-database` | 800 | N/A (folders + tags) | — |
| `notion-update-data-source` | 912 | N/A | — |
| `notion-create-comment` | 2,425 | N/A | — |
| `notion-get-comments` | 330 | N/A | — |
| `notion-get-teams` | 172 | N/A (single-user) | — |
| `notion-get-users` | 397 | N/A (single-user) | — |
| **Total** | **11,300** | **Total** | **1,216** |

Notion's `create-comment` tool alone (2,425 tokens) is **larger than Folio's entire tool surface, twice over**.

### Operational Comparison

| Operation | Notion MCP | Folio |
|---|---|---|
| Search by text | 1 call + complex JSON filters | 1 call: `query` + `tags` + `folder` |
| Filter by date/tag | 1 call + nested filter objects with UUIDs | 1 call: `updated_since='7d'`, `tags=['project']` |
| Read a document | 1 call — dumps entire content, no size awareness | 1 call — warns if over token threshold; supports `section='Heading'` for partial reads |
| Append to a log | 2 calls: `fetch` → `update` (must locate last block + craft ellipsis string) | 1 call: `mode='append'` |
| Update one section in a large doc | 2 calls + **high failure rate** (must craft exact `selection_with_ellipsis` string; breaks on duplicate text/whitespace) | 1 call: `mode='section', target='Heading'` — heading-based targeting, near-zero failure |
| Undo a mistake | ❌ Not possible via MCP | ✅ `action='undo'` — Git rollback |
| Smart error recovery | ❌ Raw API errors | ✅ Returns `available_sections`, `existing` content to save round-trips |

### What Notion MCP Can Do That Folio Can't

| Capability | Relevant to Companion Memory? |
|---|---|
| Relational databases with SQL DDL schemas | ❌ Companions don't need relational joins — tags and folders are sufficient |
| Comments & discussion threads | ❌ Companions interact via chat, not async page comments |
| Team/user management | ❌ Companion memory is single-user |
| Rich media & video transcripts | ⚠️ Niche — companion working memory is text-first |
| Strict data type validation | ⚠️ Prevents data rot, but adds friction — tradeoff |

### The Core Difference

The Notion MCP makes your AI a **Notion power user** — it can manage databases, query teams, leave comments, and navigate complex workspace hierarchies using UUIDs.

Folio makes your AI a **writer with a notebook** — it reads, writes, searches, and organizes Markdown notes using human-readable paths, with instant response times and zero schema overhead.

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
