# 📓 Folio MCP

**A Markdown-native working memory layer for AI companions.**
Instant reads. Surgical writes. Two tools. One beautiful Notion UI.

---

## Why Folio Exists

AI companions need two kinds of memory:

- **Episodic memory** — what happened in past conversations (handled by vector databases, RAG systems, tools like [Hindsight](https://github.com/vectorize-io/hindsight))
- **Working memory** — mutable, living state: project plans, user profiles, checklists, journal entries, scratchpads

Folio is the working memory layer. It gives your AI a notebook it can read, write, search, and organize — instantly, in Markdown, with zero schema overhead.

The AI writes Markdown because **Markdown is how LLMs think**. It's their native output format. No block models. No rich text objects. No database property types. Just text with structure.

Folio exposes exactly **2 tools** to the LLM: one for reading/writing (`folio`), one for searching (`folio_search`). That's 1,216 tokens of tool definitions. The LLM spends its attention on the conversation — not on learning an API.

## Why Notion?

Your AI's notes need to live somewhere you can actually see them. We evaluated every major option:

| App | API? | Content Search? | Mobile? | Self-hosted? | Verdict |
|-----|------|-----------------|---------|-------------|---------|
| **Notion** | ✅ Native Markdown API (2025) | ❌ Title-only via API | ✅ | ❌ | **Best UI + API combo** |
| Obsidian | ❌ No real API | Local only | ⚠️ Paid sync | ✅ | No programmatic access |
| Google Docs | ⚠️ Painful API | ⚠️ Slow | ✅ | ❌ | Wrong data model |
| Outline | ✅ Clean API | ✅ | ❌ | ✅ | Great fallback, tiny ecosystem |
| Apple Notes | ❌ | ❌ | ✅ | ❌ | No API at all |
| Google Keep | ❌ | ❌ | ✅ | ❌ | No API at all |
| AFFiNE | ⚠️ Immature | ⚠️ | ⚠️ | ✅ | Promising, not ready |
| Logseq | ⚠️ | Local only | ⚠️ | ✅ | Graph-only, no document model |

**Notion wins because:**

- **You already use it.** Your AI's notes appear alongside your own — no new app to adopt.
- **It's the most beautiful document renderer that exists.** Tables, code blocks, callouts, toggles — all rendered perfectly from the Markdown your AI writes.
- **Mobile access.** Read your AI's notes on your phone. Edit them on the train. Changes sync back automatically.
- **Sharing.** Send a Notion page to someone and they see exactly what your AI wrote, formatted beautifully.
- **The 2025-09-03 Native Markdown API.** Notion now accepts raw Markdown directly — no block conversion, no fidelity loss, no token waste on format translation.

**But Notion's API has two critical weaknesses** that make it unsuitable as a direct AI backend:

1. **Latency.** Every API call takes 1-2 seconds. An AI companion that pauses for 2 seconds every time it checks a note feels broken.
2. **Search is title-only.** Notion's API search endpoint only matches page titles using prefix matching. If your AI wrote "buy strawberries" inside a page called "Grocery List," searching for "strawberries" returns nothing. Searching for "Grocery" works. Searching for "List" doesn't (no suffix matching). This isn't a bug — it's a documented limitation of their search architecture.

**Folio solves both.** Supabase provides instant reads (~50ms) and real full-text content search. Notion provides the gorgeous UI. They sync bidirectionally in the background. The AI never waits. The human never compromises.

## Architecture: The Cache-First Sync Engine

```text
┌─────────────────────────────────────────────────┐
│                    Your AI                       │
│            (Claude, GPT, etc.)                   │
└──────────────────┬──────────────────────────────┘
                   │ 2 MCP tools, ~1,200 tokens
                   ▼
┌─────────────────────────────────────────────────┐
│                  Folio MCP                        │
│         Markdown in, Markdown out                │
└──────────────────┬──────────────────────────────┘
                   │ ~50ms read/write
                   ▼
┌─────────────────────────────────────────────────┐
│          Supabase (PostgreSQL)                    │
│   Instant cache · FTS with GIN indexes           │
│   Content search · websearch_to_tsquery          │
└──────────────────┬──────────────────────────────┘
                   │ Background sync (async)
          ┌────────┴────────┐
          ▼                 ▼
   Push to Notion      Pull from Notion
   (native Markdown)   (every 10 seconds)
          │                 │
          ▼                 ▼
┌─────────────────────────────────────────────────┐
│                   Notion                          │
│    Beautiful UI · Mobile · Sharing · Manual edits │
└─────────────────────────────────────────────────┘

```

**The AI never talks to Notion.** Every read, write, and search hits Supabase. Notion is the visual layer — always in sync, never in the way.

**The human never sees a database.** You open Notion and see beautifully formatted notes. Edit one on your phone. Folio picks up the change within 10 seconds.

## Folio vs the Official Notion MCP

The official [Notion MCP](https://github.com/makenotion/notion-mcp-server) wraps Notion's full API for LLMs. It's built for workspace management — databases, comments, teams, permissions. Folio is built for one thing: giving an AI companion fast, reliable working memory with a beautiful UI.

### The Numbers

Both tool surfaces were tokenized with OpenAI's `tiktoken`. These tokens are injected into the LLM's context on **every single turn** of every conversation:

| | Tools | Tokens / Turn | 100-Turn Session |
|---|---|---|---|
| **Notion MCP** | 12 | **11,300** | **1,130,000** |
| **Folio** | 2 | **1,216** | **121,600** |
| | | **9.3x heavier** | **~1M tokens wasted** |

Notion's `create-comment` tool alone (2,425 tokens) is **double** Folio's entire tool surface.

This isn't just a cost problem — it's an **attention problem**. An LLM holding 11,300 tokens of SQL DDL schemas, UUID formatting rules, team permission models, and comment threading logic on every turn is an LLM that's worse at its actual job. Research calls this "lost in the middle" — the model's attention degrades as irrelevant context grows. With Folio, 99.9% of the LLM's context window is dedicated to the conversation and the human.

### How the AI Thinks

**With the Notion MCP**, the AI thinks in UUIDs and block operations:
1. "I need to update the user's project plan"
2. Search for the page title → get UUID `f336d0bc-b841-465b-8045-024475c079dd`
3. Fetch the page by UUID → read entire content
4. Construct a `selection_with_ellipsis` string to locate the edit point: `"## Timeline\n\nQ1: Research phase...Q2: Development"`
5. Send the update with the ellipsis match
6. If the match fails (duplicate text, whitespace mismatch, Unicode issue) → retry from step 3
7. Hope it worked

**With Folio**, the AI thinks in plain English:
1. "I need to update the user's project plan"
2. `action='update', path='projects/plan.md', mode='section', target='Timeline', content='Q1: Research phase...'`
3. Done. 50ms.

The path IS the identifier. It's semantic, guessable, human-readable. If the AI constructs `journal/2026-02-28.md` from logic, it's probably right. If it constructs `f336d0bc-b841-465b-8045-024475c079dd` from logic, it's definitely wrong.

### Search: The Dealbreaker

The standard Notion API search endpoint **only matches page titles** using prefix matching. This is documented behavior, not a bug.

| Search query | Page title: "Grocery List" containing "buy strawberries" | |
|---|---|---|
| `"Grocery"` | ✅ Found (title prefix match) | |
| `"strawberries"` | ❌ Not found (body content ignored) | |
| `"List"` | ❌ Not found (no suffix matching) | |
| `"buy straw"` | ❌ Not found (not in title) | |

The official Notion MCP's only recourse is a brute-force crawl: fetch every page, scan every block, string-match manually. For a workspace with hundreds of pages, this can take **minutes** — or fail entirely.

Folio searches actual content via PostgreSQL full-text search with GIN indexes:

```json
{"query": "strawberries", "folder": "shopping"}
```

~50ms. Exact phrases. Boolean logic. Ranked by relevance. Every word in every note is indexed.

_Note: The official Notion MCP offers an `ai_search` mode for semantic body search. However, this is a **paid feature** restricted to Notion's Business/Enterprise plans with a Notion AI subscription. It is a cloud-dependent "black box" subject to external rate limits. Folio's local PostgreSQL GIN indexing is immediate, deterministic, and entirely free._

For a memory system, search that can't find content isn't search. It's decoration.

### The Update Problem

Notion's MCP uses `selection_with_ellipsis` for edits — the LLM must craft an exact substring match to locate where to edit:

```
"The quick brown fox...jumped over the lazy dog"
```

This breaks when:
*   The same sentence appears twice in the document
*   There's a whitespace or Unicode mismatch
*   The LLM miscounts characters in the ellipsis window
*   The document was edited by a human since the LLM last read it

Each failure means: re-read the full page → construct a new ellipsis string → retry. Multiple round-trips, each burning tokens and time.

Folio uses **heading-based section targeting**:

```json
{"action": "update", "mode": "section", "target": "Status", "content": "All systems go."}
```

The backend finds the heading by name, calculates the exact byte boundaries, replaces the content between headings. The LLM never reads surrounding content. It never constructs match strings. It states **semantic intent** — not string coordinates. Near-zero failure rate regardless of document size.

### Smart Errors Save Round-Trips

When something goes wrong, the difference matters:

| Scenario | Notion MCP | Folio |
| --- | --- | --- |
| Update a section that doesn't exist | Raw API error | Returns `available_sections`: `["Overview", "Timeline", "Budget"]` |
| Create a note that already exists | Error or silent overwrite | Returns the `existing` note content so the AI can decide |
| Read a note that's too large | Dumps everything, potentially overflowing context | Serves content + injects `size_warning` with token count, suggests section-level reads |

Every smart error saves a round-trip. Over a long conversation, that's dozens of wasted exchanges avoided.

### What the Notion MCP Can Do That Folio Can't

| Capability | Folio? | Relevant to companion memory? |
| --- | --- | --- |
| Relational databases with typed schemas | ❌ | ❌ — Tags and folders are sufficient for companion notes |
| Comments & discussion threads | ❌ | ❌ — Companions interact via chat, not async page comments |
| Team & user management | ❌ | ❌ — Companion memory is single-user |
| Rich media & video transcripts | ❌ | ⚠️ — Niche; companion working memory is text-first |
| Strict data type validation | ❌ | ⚠️ — Prevents data rot but adds friction |

These are enterprise collaboration features. They're valuable for teams managing workspaces. They're irrelevant — and actively harmful as context noise — for an AI companion that needs to jot down notes quickly.

### The Bottom Line

The Notion MCP makes your AI a **Notion workspace administrator**. Folio makes your AI a **writer with a fast, searchable notebook and a beautiful Notion UI**.

For a companion, it's not close.

## Features

- **2-Tool Interface** — `folio` (CRUD) and `folio_search`. 1,216 tokens total. The AI learns the entire API in one turn.
- **~50ms Everything** — Reads, writes, and searches hit the Supabase cache. The AI never waits for Notion.
- **Real Content Search** — PostgreSQL `tsvector` with GIN indexes. Exact phrases, boolean logic, ranked results. Searches what's *inside* notes, not just titles.
- **Surgical Section Updates** — `mode='section', target='Heading Name'` replaces content under a specific heading. ~5 Notion API calls regardless of note size. No string matching. No full rewrites.
- **Native Notion Markdown** — Uses the 2025-09-03 API to write raw Markdown directly to Notion. Perfect fidelity for tables, code blocks, lists, and nested formatting.
- **Smart Error Recovery** — Missing section? Returns `available_sections`. Duplicate note? Returns `existing` content. Oversized read? Injects `size_warning` with token count. Every error teaches the AI what to do next.
- **Bidirectional Sync** — Background thread pulls Notion edits every 10 seconds, reconciles deletions every 2 minutes. Edit in Notion on your phone; your AI sees the change within seconds.
- **Human-Readable Paths** — `journal/2026-02-28.md`, not `f336d0bc-b841-465b-8045-024475c079dd`. The AI constructs paths from logic, not lookups.
- **Append Mode** — `mode='append'` adds content to the end of a note without reading it. One call, zero context waste. Perfect for running logs and journals.
- **Tags, Folders, Versioning** — Organize notes with tags and directory structure. Local backend includes Git-backed undo for full state rollback.

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
  "content": "All systems go."
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
