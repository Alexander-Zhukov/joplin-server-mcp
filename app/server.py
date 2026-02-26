"""MCP server for Joplin Server (self-hosted). REST API, not Desktop Web Clipper."""

import asyncio
import base64
import os
import re
import logging
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("joplin-server-mcp")

JOPLIN_SERVER_URL = os.environ.get("JOPLIN_SERVER_URL", "").rstrip("/")
JOPLIN_EMAIL = os.environ.get("JOPLIN_EMAIL", "")
JOPLIN_PASSWORD = os.environ.get("JOPLIN_PASSWORD", "")

if not JOPLIN_SERVER_URL:
    raise RuntimeError("JOPLIN_SERVER_URL is required")
if not JOPLIN_EMAIL or not JOPLIN_PASSWORD:
    raise RuntimeError("JOPLIN_EMAIL and JOPLIN_PASSWORD are required")

TYPE_NOTE = 1
TYPE_FOLDER = 2
TYPE_SETTING = 4
TYPE_TAG = 5
TYPE_NOTE_TAG = 6
TYPE_RESOURCE = 9
TYPE_REVISION = 13

TYPE_NAMES = {
    TYPE_NOTE: "note",
    TYPE_FOLDER: "notebook",
    TYPE_TAG: "tag",
    TYPE_NOTE_TAG: "note_tag",
    TYPE_RESOURCE: "resource",
    TYPE_REVISION: "revision",
}

_client: Optional[httpx.AsyncClient] = None
_session_id: Optional[str] = None

_index: dict[str, dict] = {}
_index_ts: float = 0
INDEX_TTL = 120

_resource_index: dict[str, dict] = {}
_resource_index_ts: float = 0


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(verify=False, timeout=30)
    return _client


async def _login() -> str:
    global _session_id
    client = await _get_client()
    resp = await client.post(
        f"{JOPLIN_SERVER_URL}/api/sessions",
        json={"email": JOPLIN_EMAIL, "password": JOPLIN_PASSWORD},
    )
    resp.raise_for_status()
    _session_id = resp.json()["id"]
    logger.info("Authenticated with Joplin Server")
    return _session_id


async def _get_session() -> str:
    global _session_id
    if _session_id:
        return _session_id
    return await _login()


async def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """Make API request with auto re-login on 403."""
    global _session_id
    token = await _get_session()
    extra_headers = kwargs.pop("headers", {})
    headers = {"X-API-AUTH": token}
    headers.update(extra_headers)

    client = await _get_client()
    resp = await client.request(
        method, f"{JOPLIN_SERVER_URL}{path}", headers=headers, **kwargs
    )
    if resp.status_code == 403:
        _session_id = None
        token = await _login()
        headers["X-API-AUTH"] = token
        resp = await client.request(
            method, f"{JOPLIN_SERVER_URL}{path}", headers=headers, **kwargs
        )
    resp.raise_for_status()
    return resp


def _parse_joplin_item(raw: str) -> dict:
    """Parse Joplin serialized item (title + body + metadata block)."""
    lines = raw.split("\n")

    metadata_start = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^id:\s+[0-9a-f]{32}$", line.strip()):
            metadata_start = i
            break

    metadata = {}
    for line in lines[metadata_start:]:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

    item_type = int(metadata.get("type_", "0"))

    title = ""
    body_start = 0
    for i, line in enumerate(lines[:metadata_start]):
        if line.strip():
            title = line.strip()
            body_start = i + 1
            break

    body_lines = lines[body_start:metadata_start]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    body = "\n".join(body_lines)

    return {
        "title": title,
        "body": body,
        "id": metadata.get("id", ""),
        "parent_id": metadata.get("parent_id", ""),
        "type": item_type,
        "is_todo": metadata.get("is_todo", "0") == "1",
        "created_time": metadata.get("created_time", ""),
        "updated_time": metadata.get("updated_time", ""),
        "metadata": metadata,
    }


async def _fetch_item_content(name: str) -> Optional[dict]:
    try:
        resp = await _api("GET", f"/api/items/root:/{name}:/content")
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] == TYPE_REVISION:
            return None
        return parsed
    except Exception:
        return None


def _parse_resource_metadata(raw: str) -> dict:
    lines = raw.split("\n")
    metadata = {}
    title = lines[0].strip() if lines else ""

    for line in lines:
        stripped = line.strip()
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            if re.match(r"^[a-z_]+$", key):
                metadata[key] = value.strip()

    return {
        "title": title,
        "id": metadata.get("id", ""),
        "mime": metadata.get("mime", ""),
        "size": int(metadata.get("size", "0")),
        "file_extension": metadata.get("file_extension", ""),
        "type": int(metadata.get("type_", "0")),
        "created_time": metadata.get("created_time", ""),
        "updated_time": metadata.get("updated_time", ""),
    }


async def _build_index(force: bool = False) -> dict[str, dict]:
    """Fetch all items and resources, cache for INDEX_TTL seconds."""
    global _index, _index_ts, _resource_index, _resource_index_ts

    if not force and _index and (time.time() - _index_ts) < INDEX_TTL:
        return _index

    logger.info("Building item index...")
    all_items = []
    cursor = ""
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = await _api("GET", "/api/items/root:/:/children", params=params)
        data = resp.json()
        all_items.extend(data.get("items", []))
        if data.get("has_more"):
            cursor = data.get("cursor", "")
        else:
            break

    md_items = [it for it in all_items if it.get("name", "").endswith(".md")]
    resource_items = [it for it in all_items if it.get("name", "").startswith(".resource/")]
    logger.info(f"Found {len(md_items)} .md items, {len(resource_items)} resources")

    new_index: dict[str, dict] = {}
    batch_size = 20
    for i in range(0, len(md_items), batch_size):
        batch = md_items[i:i + batch_size]
        tasks = [_fetch_item_content(it["name"]) for it in batch]
        results = await asyncio.gather(*tasks)
        for parsed in results:
            if parsed and parsed["id"]:
                new_index[parsed["id"]] = parsed

    new_res_index: dict[str, dict] = {}

    async def _fetch_resource_meta(res_name: str) -> Optional[dict]:
        res_id = res_name.split("/")[-1]
        try:
            resp = await _api("GET", f"/api/items/root:/{res_id}.md:/content")
            return _parse_resource_metadata(resp.text)
        except Exception:
            return None

    for i in range(0, len(resource_items), batch_size):
        batch = resource_items[i:i + batch_size]
        tasks = [_fetch_resource_meta(it["name"]) for it in batch]
        results = await asyncio.gather(*tasks)
        for parsed in results:
            if parsed and parsed["id"]:
                new_res_index[parsed["id"]] = parsed

    _index = new_index
    _index_ts = time.time()
    _resource_index = new_res_index
    _resource_index_ts = time.time()
    logger.info(f"Index: {len(_index)} items, {len(_resource_index)} resources")
    return _index


async def _get_items_by_type(type_id: int, force_refresh: bool = False) -> list[dict]:
    idx = await _build_index(force=force_refresh)
    return [v for v in idx.values() if v["type"] == type_id]


async def _notebook_name(parent_id: str) -> str:
    if not parent_id:
        return "(root)"
    idx = await _build_index()
    item = idx.get(parent_id)
    return item["title"] if item else parent_id[:8]


def _find_resource_refs(body: str) -> list[str]:
    """Find deduplicated resource IDs from markdown and HTML references."""
    md_refs = re.findall(r"[(\"]:/([0-9a-f]{32})[)\"]", body)
    html_refs = re.findall(r'src=":/([0-9a-f]{32})"', body)
    seen = set()
    result = []
    for rid in md_refs + html_refs:
        if rid not in seen:
            seen.add(rid)
            result.append(rid)
    return result


async def _fetch_note(note_id: str) -> dict:
    resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)
    if parsed["type"] != TYPE_NOTE:
        raise ValueError(f"Item {note_id} is not a note (type: {TYPE_NAMES.get(parsed['type'], parsed['type'])})")
    return parsed


async def _download_resource_bytes(resource_id: str) -> tuple[bytes, str, str]:
    """Returns (bytes, mime, title)."""
    await _build_index()
    res = _resource_index.get(resource_id)
    mime = res["mime"] if res else "application/octet-stream"
    title = res["title"] if res else resource_id
    resp = await _api("GET", f"/api/items/root:/.resource/{resource_id}:/content")
    return resp.content, mime, title


def _format_note_header(parsed: dict, nb_name: str) -> str:
    todo = " [todo]" if parsed["is_todo"] else ""
    resource_refs = _find_resource_refs(parsed["body"])
    res_info = f"\nResources: {len(resource_refs)}" if resource_refs else ""
    return (
        f"# {parsed['title']}{todo}\n\n"
        f"Notebook: {nb_name}\n"
        f"Updated: {parsed['updated_time']}\n"
        f"ID: `{parsed['id']}`{res_info}\n\n"
        f"---\n\n"
    )


def _replace_resource_refs(body: str, resource_index: dict) -> str:
    """Replace resource references in body with readable labels."""
    for ref_id in _find_resource_refs(body):
        res = resource_index.get(ref_id)
        if res:
            label = f"{res['title']} ({res['mime']}, {res['size']/1024:.0f}KB)"
        else:
            label = f"resource:{ref_id}"
        body = re.sub(rf'<img\s[^>]*src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = re.sub(rf'<img\s+src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = body.replace(f"(:/{ref_id})", f"({label})")
    return body


def _invalidate_index():
    global _index_ts
    _index_ts = 0


# --- MCP tools ---

mcp = FastMCP(
    "joplin-server",
    instructions="Access notes, notebooks, and tags in Joplin Server",
)


@mcp.tool()
async def ping_joplin() -> str:
    """Check connectivity to Joplin Server."""
    try:
        await _get_session()
        return f"Connected to {JOPLIN_SERVER_URL}"
    except Exception as e:
        return f"Connection failed: {e}"


@mcp.tool()
async def list_notebooks() -> str:
    """List all notebooks."""
    notebooks = await _get_items_by_type(TYPE_FOLDER, force_refresh=True)
    if not notebooks:
        return "No notebooks found."

    lines = [f"Notebooks ({len(notebooks)})\n"]
    for nb in sorted(notebooks, key=lambda x: x["title"]):
        parent = ""
        if nb["parent_id"]:
            parent = f" (in: {await _notebook_name(nb['parent_id'])})"
        lines.append(f"- **{nb['title']}** `{nb['id']}`{parent}")
    return "\n".join(lines)


@mcp.tool()
async def get_notebook(notebook_id: str) -> str:
    """Get notebook details with its notes and sub-notebooks.

    Args:
        notebook_id: The Joplin notebook ID
    """
    idx = await _build_index(force=True)
    nb = idx.get(notebook_id)
    if not nb or nb["type"] != TYPE_FOLDER:
        return f"Notebook {notebook_id} not found."

    parent = ""
    if nb["parent_id"]:
        parent = f"\nParent: {await _notebook_name(nb['parent_id'])}"

    child_nbs = [v for v in idx.values() if v["type"] == TYPE_FOLDER and v["parent_id"] == notebook_id]
    notes = [v for v in idx.values() if v["type"] == TYPE_NOTE and v["parent_id"] == notebook_id]
    notes.sort(key=lambda x: x["updated_time"], reverse=True)

    lines = [
        f"**{nb['title']}**\n",
        f"ID: `{nb['id']}`{parent}",
        f"Updated: {nb['updated_time']}",
    ]

    if child_nbs:
        lines.append(f"\n### Sub-notebooks ({len(child_nbs)})")
        for cnb in sorted(child_nbs, key=lambda x: x["title"]):
            lines.append(f"- **{cnb['title']}** `{cnb['id']}`")

    lines.append(f"\n### Notes ({len(notes)})")
    if notes:
        for note in notes:
            todo = "[todo] " if note["is_todo"] else ""
            preview = note["body"][:80].replace("\n", " ") if note["body"] else ""
            lines.append(
                f"- {todo}**{note['title']}** `{note['id'][:12]}...`\n"
                f"  _{preview}{'...' if len(note['body']) > 80 else ''}_"
            )
    else:
        lines.append("_(empty)_")

    return "\n".join(lines)


@mcp.tool()
async def create_notebook(title: str, parent_id: str = "") -> str:
    """Create a new notebook (folder).

    Args:
        title: Notebook title
        parent_id: Parent notebook ID for nesting (optional)
    """
    import uuid
    import datetime

    nb_id = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    content = f"""{title}

id: {nb_id}
parent_id: {parent_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text: 
encryption_applied: 0
is_shared: 0
share_id: 
master_key_id: 
icon: 
deleted_time: 0
type_: 2"""

    await _api(
        "PUT",
        f"/api/items/root:/{nb_id}.md:/content",
        content=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    _invalidate_index()
    return f"Notebook created: **{title}** (ID: `{nb_id}`)"


@mcp.tool()
async def delete_notebook(notebook_id: str, force: bool = False) -> str:
    """Delete a notebook. Refuses if non-empty unless force=True.

    Args:
        notebook_id: The notebook ID to delete
        force: Delete with all contents
    """
    idx = await _build_index(force=True)
    nb = idx.get(notebook_id)
    if not nb or nb["type"] != TYPE_FOLDER:
        return f"Notebook {notebook_id} not found."

    child_notes = [v for v in idx.values() if v["type"] == TYPE_NOTE and v["parent_id"] == notebook_id]
    child_nbs = [v for v in idx.values() if v["type"] == TYPE_FOLDER and v["parent_id"] == notebook_id]

    if (child_notes or child_nbs) and not force:
        return (
            f"Notebook **{nb['title']}** is not empty "
            f"({len(child_notes)} notes, {len(child_nbs)} sub-notebooks). "
            f"Set force=True to delete with all contents."
        )

    deleted = []
    for note in child_notes:
        try:
            await _api("DELETE", f"/api/items/root:/{note['id']}.md:")
            deleted.append(note["title"])
        except Exception:
            pass

    for cnb in child_nbs:
        try:
            await _api("DELETE", f"/api/items/root:/{cnb['id']}.md:")
            deleted.append(cnb["title"])
        except Exception:
            pass

    await _api("DELETE", f"/api/items/root:/{notebook_id}.md:")
    _invalidate_index()

    result = f"Notebook deleted: **{nb['title']}** (ID: `{notebook_id}`)"
    if deleted:
        result += f"\nAlso deleted {len(deleted)} item(s): {', '.join(deleted)}"
    return result


@mcp.tool()
async def list_notes(notebook_id: Optional[str] = None, limit: int = 50) -> str:
    """List notes, optionally filtered by notebook.

    Args:
        notebook_id: Filter by notebook ID (optional)
        limit: Max notes to return (default 50)
    """
    notes = await _get_items_by_type(TYPE_NOTE)
    if notebook_id:
        notes = [n for n in notes if n["parent_id"] == notebook_id]

    notes.sort(key=lambda x: x["updated_time"], reverse=True)
    notes = notes[:limit]

    if not notes:
        return "No notes found."

    lines = [f"Notes ({len(notes)})\n"]
    for note in notes:
        nb_name = await _notebook_name(note["parent_id"])
        todo = "[todo] " if note["is_todo"] else ""
        preview = note["body"][:80].replace("\n", " ") if note["body"] else ""
        lines.append(
            f"- {todo}**{note['title']}** `{note['id'][:12]}...`\n"
            f"  {nb_name} | {note['updated_time'][:10]}\n"
            f"  _{preview}{'...' if len(note['body']) > 80 else ''}_"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_notes(query: str, limit: int = 20) -> str:
    """Search notes by text in title or body.

    Args:
        query: Search string
        limit: Max results (default 20)
    """
    query_lower = query.lower()
    notes = await _get_items_by_type(TYPE_NOTE)
    results = []

    for note in notes:
        if query_lower in note["title"].lower() or query_lower in note["body"].lower():
            results.append(note)
            if len(results) >= limit:
                break

    if not results:
        return f"No notes matching '{query}'."

    lines = [f"Search '{query}' ({len(results)} results)\n"]
    for note in results:
        nb_name = await _notebook_name(note["parent_id"])
        preview = note["body"][:120].replace("\n", " ")
        lines.append(
            f"- **{note['title']}** `{note['id'][:12]}...`\n"
            f"  {nb_name} | {note['updated_time'][:10]}\n"
            f"  _{preview}{'...' if len(note['body']) > 120 else ''}_"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_note(note_id: str) -> str:
    """Get note text content (without resource files). Use get_note_full for embedded resources.

    Args:
        note_id: The Joplin note ID
    """
    try:
        parsed = await _fetch_note(note_id)
        nb_name = await _notebook_name(parsed["parent_id"])
        await _build_index()
        body = _replace_resource_refs(parsed["body"], _resource_index)
        return _format_note_header(parsed, nb_name) + body
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Note {note_id} not found."
        raise


@mcp.tool()
async def get_note_full(note_id: str) -> str:
    """Get note with all resources embedded as base64. Can be large.

    Args:
        note_id: The Joplin note ID
    """
    try:
        parsed = await _fetch_note(note_id)
        nb_name = await _notebook_name(parsed["parent_id"])
        await _build_index()

        body = parsed["body"]
        resource_refs = _find_resource_refs(body)

        if not resource_refs:
            return _format_note_header(parsed, nb_name) + body + "\n\n_(No resources)_"

        async def _dl(rid: str) -> tuple[str, Optional[bytes], str, str]:
            try:
                data, mime, title = await _download_resource_bytes(rid)
                return rid, data, mime, title
            except Exception:
                return rid, None, "", ""

        results = await asyncio.gather(*[_dl(rid) for rid in resource_refs])
        body = _replace_resource_refs(body, _resource_index)

        output = _format_note_header(parsed, nb_name) + body
        output += f"\n\n---\n\n## Resources ({len(resource_refs)})\n"
        for rid, data, mime, title in results:
            if data is None:
                output += f"\n### {rid}\nFailed to download.\n"
                continue
            b64 = base64.b64encode(data).decode("ascii")
            output += (
                f"\n### {title or rid}\n"
                f"- MIME: {mime}\n"
                f"- Size: {len(data)/1024:.0f} KB\n"
                f"- ID: `{rid}`\n\n"
                f"data:{mime};base64,{b64}\n"
            )
        return output

    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Note {note_id} not found."
        raise


@mcp.tool()
async def create_note(title: str, body: str, notebook_id: str = "") -> str:
    """Create a new note.

    Args:
        title: Note title
        body: Note body in Markdown
        notebook_id: Parent notebook ID (optional)
    """
    import uuid
    import datetime

    note_id = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    content = f"""{title}

{body}

id: {note_id}
parent_id: {notebook_id}
created_time: {now}
updated_time: {now}
is_conflict: 0
latitude: 0.00000000
longitude: 0.00000000
altitude: 0.0000
author: 
source_url: 
is_todo: 0
todo_due: 0
todo_completed: 0
source: joplin-mcp
source_application: net.runetree.joplin-mcp
application_data: 
order: 0
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text: 
encryption_applied: 0
markup_language: 1
is_shared: 0
share_id: 
conflict_original_id: 
master_key_id: 
user_data: 
deleted_time: 0
type_: 1"""

    await _api(
        "PUT",
        f"/api/items/root:/{note_id}.md:/content",
        content=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    _invalidate_index()
    return f"Note created: **{title}** (ID: `{note_id}`)"


@mcp.tool()
async def update_note(note_id: str, title: Optional[str] = None, body: Optional[str] = None) -> str:
    """Update an existing note.

    Args:
        note_id: The note ID to update
        title: New title (optional)
        body: New body (optional)
    """
    import datetime

    resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)

    if parsed["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    new_title = title if title is not None else parsed["title"]
    new_body = body if body is not None else parsed["body"]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    meta = parsed["metadata"]
    meta["updated_time"] = now
    meta["user_updated_time"] = now

    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items())
    content = f"{new_title}\n\n{new_body}\n\n{meta_lines}"

    await _api(
        "PUT",
        f"/api/items/root:/{note_id}.md:/content",
        content=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    _invalidate_index()
    return f"Note updated: **{new_title}** (ID: `{note_id}`)"


@mcp.tool()
async def delete_note(note_id: str) -> str:
    """Delete a note by ID.

    Args:
        note_id: The note ID to delete
    """
    try:
        resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return f"Item {note_id} is not a note."

        await _api("DELETE", f"/api/items/root:/{note_id}.md:")
        _invalidate_index()
        return f"Note deleted: **{parsed['title']}** (ID: `{note_id}`)"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Note {note_id} not found."
        raise


@mcp.tool()
async def list_tags() -> str:
    """List all tags."""
    tags = await _get_items_by_type(TYPE_TAG)
    if not tags:
        return "No tags found."

    lines = [f"Tags ({len(tags)})\n"]
    for tag in sorted(tags, key=lambda x: x["title"]):
        lines.append(f"- **{tag['title']}** `{tag['id']}`")
    return "\n".join(lines)


@mcp.tool()
async def get_note_resources(note_id: str) -> str:
    """List resources (images, attachments) in a note.

    Args:
        note_id: The Joplin note ID
    """
    await _build_index()

    note = _index.get(note_id)
    if not note:
        return f"Note {note_id} not found."
    if note["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    refs = _find_resource_refs(note["body"])
    if not refs:
        return f"Note **{note['title']}** has no resources."

    lines = [f"Resources in '{note['title']}' ({len(refs)})\n"]
    for ref_id in refs:
        res = _resource_index.get(ref_id)
        if res:
            lines.append(f"- **{res['title']}** `{ref_id}` — {res['mime']}, {res['size']/1024:.0f} KB")
        else:
            lines.append(f"- `{ref_id}` (metadata not found)")
    return "\n".join(lines)


@mcp.tool()
async def get_resource_info(resource_id: str) -> str:
    """Get metadata for a resource.

    Args:
        resource_id: The Joplin resource ID (32-char hex)
    """
    await _build_index()

    res = _resource_index.get(resource_id)
    if not res:
        try:
            resp = await _api("GET", f"/api/items/root:/{resource_id}.md:/content")
            res = _parse_resource_metadata(resp.text)
        except Exception:
            return f"Resource {resource_id} not found."

    return (
        f"**{res['title']}**\n\n"
        f"- ID: `{res['id']}`\n"
        f"- MIME: {res['mime']}\n"
        f"- Size: {res['size']/1024:.1f} KB ({res['size']} bytes)\n"
        f"- Extension: {res['file_extension']}\n"
        f"- Created: {res['created_time']}\n"
        f"- Updated: {res['updated_time']}"
    )


@mcp.tool()
async def download_resource(resource_id: str) -> str:
    """Download a resource as base64. Max 50 MB.

    Args:
        resource_id: The Joplin resource ID (32-char hex)
    """
    await _build_index()

    res = _resource_index.get(resource_id)
    size = res["size"] if res else 0

    if size > 50 * 1024 * 1024:
        return f"Resource too large ({size / 1024 / 1024:.1f} MB). Max 50 MB."

    try:
        data, mime, title = await _download_resource_bytes(resource_id)
        b64 = base64.b64encode(data).decode("ascii")
        return f"**{title}** ({mime}, {len(data)/1024:.0f} KB)\n\ndata:{mime};base64,{b64}"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Resource {resource_id} not found."
        raise


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.run(
            transport="sse",
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8081")),
        )
    else:
        mcp.run(transport="stdio")
