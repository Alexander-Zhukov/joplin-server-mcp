# Joplin Server MCP

[Model Context Protocol](https://modelcontextprotocol.io/) server for [Joplin Server](https://github.com/laurent22/joplin/tree/dev/packages/server). Gives LLMs full access to your notes, notebooks, tags, and attachments via the Joplin Server REST API.

## What is Joplin Server?

[Joplin](https://joplinapp.org/) is an open-source note-taking app with Markdown support, end-to-end encryption, and sync across devices. [Joplin Server](https://github.com/laurent22/joplin/tree/dev/packages/server) is the sync backend that stores and syncs your data. You can run it yourself using the official [Docker image](https://hub.docker.com/r/joplin/server) or use the managed [Joplin Cloud](https://joplincloud.com/) service.

> **Note:** This MCP server connects to **Joplin Server** REST API, not the Joplin Desktop Web Clipper.
>
> **Joplin Cloud:** Should work with `JOPLIN_SERVER_URL=https://joplincloud.com`, but this has not been tested. If you try it — please open an issue with your results.

## Tools

| Tool | Description |
|---|---|
| `ping_joplin` | Check server connectivity |
| `list_notebooks` | List all notebooks |
| `get_notebook` | Get notebook details with notes and sub-notebooks |
| `create_notebook` | Create a new notebook |
| `delete_notebook` | Delete a notebook (with optional force for non-empty) |
| `list_notes` | List notes, optionally filtered by notebook |
| `search_notes` | Search notes by text in title or body |
| `get_note` | Get note text (resource references replaced with names) |
| `get_note_full` | Get note with all resources embedded as base64 |
| `create_note` | Create a new note |
| `export_note` | Export note as markdown with resources as named base64 blocks |
| `update_note` | Update note title, body, or move to another notebook |
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

The server authenticates with Joplin Server via email/password sessions and builds an in-memory index of all items (notes, notebooks, tags, resources) with a 2-minute TTL cache. Items are fetched in parallel batches for performance.

Joplin's internal serialization format (title + markdown body + metadata block) is parsed and presented as clean structured output.

## License

[MIT](LICENSE)
