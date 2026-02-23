# 📓 Folio MCP

Working memory for AI companions. Two tools, markdown in and out, swappable backends.

## Why

Your AI doesn't need 20 Notion API calls. It needs to create, read, update, and search
notes — in markdown, organized in folders, with tags. Folio gives it exactly that.

- **2 tools** — `folio` (CRUD) and `folio_search` (find things)
- **Markdown native** — the AI thinks in markdown, Folio speaks markdown
- **Backend-agnostic** — local files today, Notion tomorrow, same interface
- **Versioned** — local backend auto-commits to git, undo built in

## Quick Start

### 1. Install

```bash
git clone https://github.com/yourname/folio-mcp.git
cd folio-mcp
pip install -e .
```

### 2. Configure

Create a `.env` file:

```
# Local backend (default) — just set a directory
FOLIO_BACKEND=local
FOLIO_LOCAL_ROOT=~/folio-notes

# Or Notion backend — requires API key + database
# FOLIO_BACKEND=notion
# NOTION_API_KEY=ntn_...
# NOTION_DATABASE_ID=abc123...
```

### 3. Run

```bash
python -m folio_mcp
```

### 4. Connect to your AI

Add to your MCP client config (e.g. Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "folio": {
      "command": "python",
      "args": ["-m", "folio_mcp"],
      "env": {
        "FOLIO_BACKEND": "local",
        "FOLIO_LOCAL_ROOT": "/Users/you/folio-notes"
      }
    }
  }
}
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FOLIO_BACKEND` | No | `local` | Backend: `local` or `notion` |
| `FOLIO_LOCAL_ROOT` | Local | `./notes` | Directory for local markdown files |
| `NOTION_API_KEY` | Notion | — | Notion integration token |
| `NOTION_DATABASE_ID` | Notion | — | Target database ID |

## Tools

### `folio` — Read and write notes

| Action | What it does |
|--------|--------------|
| `create` | New note with path, content, tags |
| `read` | Get a note (optionally just one section) |
| `update` | Modify content — replace, append, or update one section |
| `delete` | Remove a note |
| `move` | Change a note's path |
| `list` | Browse notes in a folder |
| `undo` | Revert to previous version (local only) |

Update modes:

- **replace** — rewrite the entire note
- **append** — add to the end without touching existing content
- **section** — rewrite under one heading, leave everything else

### `folio_search` — Find notes

Search by content, tags, folder, or recency. Results ranked by relevance with content snippets.

```python
folio_search("cycling routes")
folio_search("birthday", tags=["person"])
folio_search("", tags=["pinned"])
folio_search("illustration", updated_since="7d", sort="recent")
```

## Project Structure

```
folio_mcp/
├── __init__.py          # Package init
├── server.py            # MCP server — tool definitions + routing
├── models.py            # Note data model
├── config.py            # Settings from environment
├── sections.py          # Markdown section read/replace
└── backends/
    ├── __init__.py      # Backend interface + factory
    ├── local.py         # Local filesystem + git versioning
    └── notion.py        # Notion API backend
```

## Backends

### Local (default)

Notes stored as markdown files in `FOLIO_LOCAL_ROOT`. Metadata (tags, timestamps) in YAML frontmatter. Every change auto-committed to a local git repo for versioning and undo.

```
~/folio-notes/
├── journal/
│   ├── 2026-02-23.md
│   └── 2026-02-22.md
├── people/
│   └── him❤️/
│       └── plans.md
└── projects/
    └── companion.md
```

### Notion

Notes stored as pages in a Notion database. Tags via multi-select property, folders via a `folder` property. Markdown converted to Notion blocks on write, back to markdown on read.

**Setup:**

1. Create a [Notion integration](https://www.notion.so/my-integrations)
2. Create a database with properties: `title` (title), `tags` (multi-select), `folder` (rich text)
3. Share the database with your integration
4. Set `NOTION_API_KEY` and `NOTION_DATABASE_ID`

## License

MIT
