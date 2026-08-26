# Joplin Server MCP

[Model Context Protocol](https://modelcontextprotocol.io/) server for [Joplin Server](https://github.com/laurent22/joplin/tree/dev/packages/server). Gives LLMs full access to your notes, notebooks, tags, and attachments via the Joplin Server REST API.

## What is Joplin Server?

[Joplin](https://joplinapp.org/) is an open-source note-taking app with Markdown support, end-to-end encryption, and sync across devices. [Joplin Server](https://github.com/laurent22/joplin/tree/dev/packages/server) is the sync backend that stores and syncs your data. You can run it yourself using the official [Docker image](https://hub.docker.com/r/joplin/server) or use the managed [Joplin Cloud](https://joplincloud.com/) service.

> **Note:** This MCP server connects to **Joplin Server** REST API, not the Joplin Desktop Web Clipper.
>
> **Joplin Cloud:** Use `JOPLIN_SERVER_URL=https://api.joplincloud.com`.

## Tools

| Tool | Description |
|---|---|
| `ping_joplin` | Check server connectivity |
| `list_notebooks` | List all notebooks |
| `get_notebook` | Get notebook details with notes and sub-notebooks |
| `create_notebook` | Create a new notebook |
| `get_or_create_notebook` | Resolve a `/`-separated notebook path, creating missing levels |
| `update_notebook` | Rename or move a notebook (with circular reference check) |
| `delete_notebook` | Delete a notebook (with optional force for non-empty) |
| `list_notes` | List notes, optionally filtered by notebook, tag, and/or to-do state |
| `get_all_notes` | Get all notes with pagination, sorting (incl. `todo_due`/`todo_completed`), notebook and to-do filters |
| `search_notes` | Search notes (multi-term AND) with scope and notebook/tag filters |
| `get_note` | Get note text (resource references replaced with names, or `raw=True` for the verbatim body) |
| `get_notes_batch` | Read multiple notes at once (up to 50, parallel) |
| `get_note_full` | Get note with all resources embedded as base64 |
| `create_note` | Create a new note (optionally as a to-do with a due date) |
| `export_note` | Export note as markdown with resources as named base64 blocks |
| `update_note` | Update note title, body, or move to another notebook |
| `get_note_outline` | Map a note's headings with line numbers and section sizes |
| `append_to_note` | Add text to a note or one of its sections without resending the body |
| `replace_in_note` | Replace an exact string inside a note body |
| `replace_section` | Replace everything under a heading |
| `set_todo` | Set/clear a note's to-do state, completion, and due date |
| `delete_note` | Delete a note |
| `list_tags` | List all tags |
| `create_tag` | Create a new tag |
| `delete_tag` | Delete a tag |
| `get_note_tags` | List tags assigned to a note |
| `add_tag_to_note` | Add a tag to a note |
| `remove_tag_from_note` | Remove a tag from a note |
| `get_note_resources` | List resources attached to a note |
| `get_resource_info` | Get resource metadata |
| `download_resource` | Download a resource as base64 |

## Partial edits

`update_note` takes the complete new body, which makes it a poor fit for large
notes: adding two lines to a 70 KB note means an LLM has to reproduce all 70 KB
verbatim. That is expensive, and a single silent typo corrupts the note.

`append_to_note`, `replace_in_note` and `replace_section` resolve the edit on
the server instead. The client sends only the fragment it wants added or
changed, so untouched text — including `:/<resource-id>` links, which the read
tools render as human-readable names — is preserved byte for byte.

```
append_to_note(note_id, "| docker-2 | .43 |", section="Hosts", separator="\n")
append_to_note(note_id, "## 2026-08-26\n\nDeployed.", section="Work Log", position="start")
replace_in_note(note_id, "status: draft", "status: final")
replace_section(note_id, "## Current state", "Rewritten from scratch.")
```

Every write reports what it did — character and line deltas, heading count
before and after, and the numbered lines around the change — so the result can
be verified without re-reading the note:

```
Appended to: **01 · Work Log** (ID: `3e8ab15d2f8b40cd9c2e754a7b2db13f`)
Inserted 745 chars at the start of section '# 01 · Work Log' (line 1)
Chars 76141 -> 76887 (+746), lines 1858 -> 1885 (+27), headings 117 -> 118
```

Guard rails:

- **Sections are addressed by heading**, either bare (`"Hosts"`) or with its
  level (`"## Hosts"`), and a section ends at the next heading of the same or
  higher level. Headings inside fenced code blocks are ignored. An ambiguous or
  missing heading is refused, never guessed. `get_note_outline` lists what is
  available without reading the body.
- **`replace_in_note` demands a unique match.** A missing anchor or an
  unexpected second match is an error, with the mismatching lines reported.
  Use `get_note(raw=True)` to copy an anchor verbatim, or `replace_all=True`
  when you do mean every occurrence.
- **`dry_run=True`** reports the change and its context without writing.
- **`if_absent="marker"`** skips the append when the marker is already in the
  note, which makes a re-run a no-op.

## Setup

### Prerequisites

- A running [Joplin Server](https://github.com/laurent22/joplin/tree/dev/packages/server) instance or [Joplin Cloud](https://joplincloud.com/) account
- User credentials (email + password)

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `JOPLIN_SERVER_URL` | Yes | — | Joplin Server URL |
| `JOPLIN_EMAIL` | Yes | — | User email |
| `JOPLIN_PASSWORD` | Yes | — | User password |
| `MCP_TRANSPORT` | No | `stdio` | Transport: `stdio` or `sse` |
| `MCP_HOST` | No | `0.0.0.0` | SSE listen host |
| `MCP_PORT` | No | `8081` | SSE listen port |

### Run with Docker

```bash
docker run -d \
  -e JOPLIN_SERVER_URL=https://your-joplin-server.example.com \
  -e JOPLIN_EMAIL=your@email.com \
  -e JOPLIN_PASSWORD=your_password \
  -p 8081:8081 \
  alexfail2/joplin-mcp
```

The container defaults to SSE transport. The endpoint will be available at `http://localhost:8081/sse`.

### Run locally (stdio)

```bash
pip install mcp httpx
python app/server.py
```

### Build from source

```bash
docker build -t joplin-mcp .
```

## MCP client configuration

### SSE (Docker)

```json
{
  "mcpServers": {
    "joplin": {
      "url": "http://localhost:8081/sse"
    }
  }
}
```

### stdio (local)

```json
{
  "mcpServers": {
    "joplin": {
      "command": "python",
      "args": ["/path/to/app/server.py"],
      "env": {
        "JOPLIN_SERVER_URL": "https://your-joplin-server.example.com",
        "JOPLIN_EMAIL": "your@email.com",
        "JOPLIN_PASSWORD": "your_password"
      }
    }
  }
}
```

## How it works

The server authenticates with Joplin Server via email/password sessions and builds an in-memory index of all items (notes, notebooks, tags) with a 2-minute TTL cache. Incremental sync compares server-side `updated_time` with cached etags, fetching only changed items (typical refresh: ~5s vs ~35s full rebuild). The index is persisted to disk so container restarts are instant. Resource metadata is loaded lazily on first access. Background refresh keeps the index up to date without blocking requests. All IDs are validated (32-char hex) before API calls.

Joplin's internal serialization format (title + markdown body + metadata block) is parsed and presented as clean structured output.

## Development

The pure helpers (markdown section resolution, splicing, edit reports, to-do
state) are covered by a dependency-free test suite:

```bash
python tests/test_helpers.py
```

## License

[MIT](LICENSE)
