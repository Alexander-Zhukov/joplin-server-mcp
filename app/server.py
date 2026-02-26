"""MCP server for Joplin Server. REST API."""

import asyncio
import base64
from contextlib import asynccontextmanager
import datetime
import json
import os
import re
import logging
import time
import uuid
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("joplin-mcp")

JOPLIN_SERVER_URL = os.environ.get("JOPLIN_SERVER_URL", "").rstrip("/")
JOPLIN_EMAIL = os.environ.get("JOPLIN_EMAIL", "")
JOPLIN_PASSWORD = os.environ.get("JOPLIN_PASSWORD", "")

if not JOPLIN_SERVER_URL:
    raise RuntimeError("JOPLIN_SERVER_URL is required")
if not JOPLIN_EMAIL or not JOPLIN_PASSWORD:
    raise RuntimeError("JOPLIN_EMAIL and JOPLIN_PASSWORD are required")

TYPE_NOTE = 1
TYPE_FOLDER = 2
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
_resource_index: dict[str, dict] = {}
_server_etags: dict[str, str] = {}
_resource_index_ready = False
_index_lock: Optional[asyncio.Lock] = None
INDEX_TTL = 120
INDEX_CONCURRENCY = 50
MAX_RESOURCE_SIZE = 50 * 1024 * 1024
INDEX_CACHE_FILE = "/tmp/joplin_index_cache.json"


_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_id(value: str, label: str = "ID") -> Optional[str]:
    """Return error message if value is not a valid 32-char hex Joplin ID, else None."""
    if not value or not _ID_RE.match(value):
        return f"Invalid {label}: '{value}'. Must be a 32-character hex string."
    return None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")



# -- HTTP client & auth --

async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            verify=False,
            timeout=30,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=60,
            ),
        )
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
    """Make API request, re-login on 403."""
    global _session_id
    token = await _get_session()
    extra_headers = kwargs.pop("headers", {})
    headers = {"X-API-AUTH": token, **extra_headers}

    client = await _get_client()
    resp = await client.request(method, f"{JOPLIN_SERVER_URL}{path}", headers=headers, **kwargs)
    if resp.status_code == 403:
        _session_id = None
        token = await _login()
        headers["X-API-AUTH"] = token
        resp = await client.request(method, f"{JOPLIN_SERVER_URL}{path}", headers=headers, **kwargs)
    resp.raise_for_status()
    return resp


async def _put_item(item_id: str, content: str):
    await _api(
        "PUT",
        f"/api/items/root:/{item_id}.md:/content",
        content=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    parsed = _parse_joplin_item(content)
    if parsed and parsed["id"]:
        _index[parsed["id"]] = parsed


# -- Joplin item parsing --

def _parse_joplin_item(raw: str) -> dict:
    lines = raw.split("\n")

    metadata_start = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^id:\s+[0-9a-f]{32}$", line.strip()):
            metadata_start = i
            break

    metadata = {}
    for line in lines[metadata_start:]:
        line = line.strip()
        if line and ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

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

    return {
        "title": title,
        "body": "\n".join(body_lines),
        "id": metadata.get("id", ""),
        "parent_id": metadata.get("parent_id", ""),
        "type": int(metadata.get("type_", "0")),
        "is_todo": metadata.get("is_todo", "0") == "1",
        "created_time": metadata.get("created_time", ""),
        "updated_time": metadata.get("updated_time", ""),
        "metadata": metadata,
    }


def _parse_resource_metadata(raw: str) -> dict:
    lines = raw.split("\n")
    title = lines[0].strip() if lines else ""
    metadata = {}
    for line in lines:
        stripped = line.strip()
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            if re.match(r"^[a-z_]+$", key.strip()):
                metadata[key.strip()] = value.strip()

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


# -- Index --

def _save_index_cache():
    try:
        data = {
            "index": _index,
            "resource_index": _resource_index,
            "server_etags": _server_etags,
            "ts": _index_ts,
        }
        with open(INDEX_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Failed to save index cache: {e}")


def _load_index_cache() -> bool:
    global _index, _index_ts, _resource_index, _resource_index_ready, _server_etags
    try:
        with open(INDEX_CACHE_FILE) as f:
            data = json.load(f)
        _index = data.get("index", {})
        _resource_index = data.get("resource_index", {})
        _server_etags = data.get("server_etags", {})
        _index_ts = data.get("ts", 0)
        _resource_index_ready = bool(_resource_index)
        logger.info(f"Loaded index from cache: {len(_index)} items, {len(_resource_index)} resources")
        return bool(_index)
    except Exception:
        return False


async def _fetch_item_content(name: str) -> Optional[dict]:
    try:
        resp = await _api("GET", f"/api/items/root:/{name}:/content")
        parsed = _parse_joplin_item(resp.text)
        return None if parsed["type"] == TYPE_REVISION else parsed
    except Exception:
        return None


def _get_index_lock() -> asyncio.Lock:
    global _index_lock
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    return _index_lock


async def _do_build_index() -> dict[str, dict]:
    """Incremental index: only fetch new/changed items based on server updated_time."""
    global _index, _index_ts, _resource_index_ready, _server_etags

    lock = _get_index_lock()
    if lock.locked():
        return _index

    async with lock:
        t0 = time.time()
        all_items, cursor = [], ""
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await _api("GET", "/api/items/root:/:/children", params=params)
            data = resp.json()
            all_items.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", "")

        md_items = [it for it in all_items if it.get("name", "").endswith(".md")]
        server_ids = set()
        to_fetch = []

        for it in md_items:
            item_id = it["name"].replace(".md", "")
            server_ids.add(item_id)
            server_ut = str(it.get("updated_time", ""))
            if _server_etags.get(item_id) != server_ut:
                to_fetch.append((it["name"], item_id, server_ut))

        deleted = [k for k in list(_index) if k not in server_ids and _index[k].get("type") != TYPE_RESOURCE]

        if not to_fetch and not deleted:
            _index_ts = time.time()
            logger.info(f"Index up to date: {len(_index)} items ({time.time() - t0:.1f}s)")
            return _index

        logger.info(f"Syncing index: {len(to_fetch)} changed, {len(deleted)} deleted"
                     f" (of {len(md_items)} total)")

        sem = asyncio.Semaphore(INDEX_CONCURRENCY)

        async def _fetch_with_sem(name: str) -> Optional[dict]:
            async with sem:
                return await _fetch_item_content(name)

        results = await asyncio.gather(*[_fetch_with_sem(name) for name, _, _ in to_fetch])
        for (_, item_id, server_ut), parsed in zip(to_fetch, results):
            _server_etags[item_id] = server_ut
            if parsed and parsed["id"]:
                _index[parsed["id"]] = parsed

        for item_id in deleted:
            _index.pop(item_id, None)
            _server_etags.pop(item_id, None)

        _index_ts = time.time()
        _resource_index_ready = False
        _save_index_cache()
        logger.info(f"Index: {len(_index)} items, synced {len(to_fetch)} in {time.time() - t0:.1f}s")
        return _index


async def _build_index(force: bool = False) -> dict[str, dict]:
    """Return index, triggering background refresh if TTL expired."""
    if not force and _index and (time.time() - _index_ts) < INDEX_TTL:
        return _index

    if _index:
        asyncio.create_task(_do_build_index())
        return _index

    return await _do_build_index()


async def _ensure_resource_index():
    """Build resource index lazily on first access."""
    global _resource_index, _resource_index_ready
    if _resource_index_ready:
        return

    await _build_index()

    t0 = time.time()
    logger.info("Building resource index...")
    all_items, cursor = [], ""
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = await _api("GET", "/api/items/root:/:/children", params=params)
        data = resp.json()
        all_items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("cursor", "")

    resource_items = [it for it in all_items if it.get("name", "").startswith(".resource/")]
    sem = asyncio.Semaphore(INDEX_CONCURRENCY)

    async def _fetch_res_meta(name: str) -> Optional[dict]:
        async with sem:
            try:
                resp = await _api("GET", f"/api/items/root:/{name.split('/')[-1]}.md:/content")
                return _parse_resource_metadata(resp.text)
            except Exception:
                return None

    results = await asyncio.gather(*[_fetch_res_meta(it["name"]) for it in resource_items])
    new_res_index: dict[str, dict] = {}
    for parsed in results:
        if parsed and parsed["id"]:
            new_res_index[parsed["id"]] = parsed

    _resource_index = new_res_index
    _resource_index_ready = True
    _save_index_cache()
    logger.info(f"Resource index: {len(_resource_index)} resources in {time.time() - t0:.1f}s")


async def _get_items_by_type(type_id: int, force_refresh: bool = False) -> list[dict]:
    idx = await _build_index(force=force_refresh)
    return [v for v in idx.values() if v["type"] == type_id]


async def _notebook_name(parent_id: str) -> str:
    if not parent_id:
        return "(root)"
    idx = await _build_index()
    item = idx.get(parent_id)
    return item["title"] if item else parent_id[:8]


# -- Resource helpers --

def _find_resource_refs(body: str) -> list[str]:
    """Deduplicated resource IDs from markdown and HTML refs."""
    refs = re.findall(r"[(\"]:/([0-9a-f]{32})[)\"]", body)
    refs += re.findall(r'src=":/([0-9a-f]{32})"', body)
    seen, result = set(), []
    for rid in refs:
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


async def _download_resource(resource_id: str) -> tuple[bytes, str, str]:
    """Returns (bytes, mime, title)."""
    await _ensure_resource_index()
    res = _resource_index.get(resource_id)
    resp = await _api("GET", f"/api/items/root:/.resource/{resource_id}:/content")
    return resp.content, res["mime"] if res else "application/octet-stream", res["title"] if res else resource_id


def _format_note_header(parsed: dict, nb_name: str) -> str:
    todo = " [todo]" if parsed["is_todo"] else ""
    refs = _find_resource_refs(parsed["body"])
    res_info = f"\nResources: {len(refs)}" if refs else ""
    return (
        f"# {parsed['title']}{todo}\n\n"
        f"Notebook: {nb_name}\n"
        f"Updated: {parsed['updated_time']}\n"
        f"ID: `{parsed['id']}`{res_info}\n\n---\n\n"
    )


def _replace_resource_refs(body: str) -> str:
    for ref_id in _find_resource_refs(body):
        res = _resource_index.get(ref_id)
        label = f"{res['title']} ({res['mime']}, {res['size']/1024:.0f}KB)" if res else f"resource:{ref_id}"
        body = re.sub(rf'<img\s[^>]*src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = re.sub(rf'<img\s+src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = body.replace(f"(:/{ref_id})", f"({label})")
    return body


def _localize_resource_refs(body: str) -> str:
    """Replace Joplin resource refs with local filenames."""
    for ref_id in _find_resource_refs(body):
        res = _resource_index.get(ref_id)
        fname = res["title"] if res else f"{ref_id}.bin"
        body = re.sub(rf'<img\s[^>]*src=":/{ ref_id}"[^>]*/?>', f"![{fname}]({fname})", body)
        body = re.sub(rf'<img\s+src=":/{ ref_id}"[^>]*/?>', f"![{fname}]({fname})", body)
        body = body.replace(f"(:/{ref_id})", f"({fname})")
    return body


async def _download_refs(resource_refs: list[str]) -> list[tuple[str, Optional[bytes], str, str]]:
    async def _dl(rid: str):
        try:
            data, mime, title = await _download_resource(rid)
            return rid, data, mime, title
        except Exception:
            return rid, None, "", ""
    return await asyncio.gather(*[_dl(rid) for rid in resource_refs])


# -- Joplin item templates --

def _note_template(note_id: str, title: str, body: str, notebook_id: str, now: str) -> str:
    return f"""{title}

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
source_application: joplin-mcp
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


def _folder_template(folder_id: str, title: str, parent_id: str, now: str) -> str:
    return f"""{title}

id: {folder_id}
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


def _tag_template(tag_id: str, title: str, now: str) -> str:
    return f"""{title}

id: {tag_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text: 
encryption_applied: 0
is_shared: 0
parent_id: 
type_: 5"""


def _note_tag_template(nt_id: str, note_id: str, tag_id: str, tag_title: str, now: str) -> str:
    return f"""{tag_title}

id: {nt_id}
note_id: {note_id}
tag_id: {tag_id}
created_time: {now}
updated_time: {now}
user_created_time: {now}
user_updated_time: {now}
encryption_cipher_text: 
encryption_applied: 0
is_shared: 0
type_: 6"""


# ===== MCP tools =====

@asynccontextmanager
async def _lifespan(server):
    if not _index:
        cached = _load_index_cache()
        if not cached or (time.time() - _index_ts) > INDEX_TTL:
            asyncio.create_task(_do_build_index())
    yield {}

mcp = FastMCP(
    "joplin-server",
    instructions="Access notes, notebooks, and tags in Joplin Server",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8081")),
    lifespan=_lifespan,
)


# -- Connection --

@mcp.tool()
async def ping_joplin() -> str:
    """Check connectivity to Joplin Server."""
    try:
        await _get_session()
        return f"Connected to {JOPLIN_SERVER_URL}"
    except Exception as e:
        return f"Connection failed: {e}"


# -- Notebooks --

@mcp.tool()
async def list_notebooks() -> str:
    """List all notebooks."""
    notebooks = await _get_items_by_type(TYPE_FOLDER, force_refresh=True)
    if not notebooks:
        return "No notebooks found."
    lines = [f"Notebooks ({len(notebooks)})\n"]
    for nb in sorted(notebooks, key=lambda x: x["title"]):
        parent = f" (in: {await _notebook_name(nb['parent_id'])})" if nb["parent_id"] else ""
        lines.append(f"- **{nb['title']}** `{nb['id']}`{parent}")
    return "\n".join(lines)


@mcp.tool()
async def get_notebook(notebook_id: str) -> str:
    """Get notebook details with its notes and sub-notebooks.

    Args:
        notebook_id: The Joplin notebook ID
    """
    err = _validate_id(notebook_id, "notebook ID")
    if err:
        return err
    idx = await _build_index(force=True)
    nb = idx.get(notebook_id)
    if not nb or nb["type"] != TYPE_FOLDER:
        return f"Notebook {notebook_id} not found."

    parent = f"\nParent: {await _notebook_name(nb['parent_id'])}" if nb["parent_id"] else ""
    child_nbs = [v for v in idx.values() if v["type"] == TYPE_FOLDER and v["parent_id"] == notebook_id]
    notes = sorted(
        [v for v in idx.values() if v["type"] == TYPE_NOTE and v["parent_id"] == notebook_id],
        key=lambda x: x["updated_time"], reverse=True,
    )

    lines = [f"**{nb['title']}**\n", f"ID: `{nb['id']}`{parent}", f"Updated: {nb['updated_time']}"]

    if child_nbs:
        lines.append(f"\n### Sub-notebooks ({len(child_nbs)})")
        for cnb in sorted(child_nbs, key=lambda x: x["title"]):
            lines.append(f"- **{cnb['title']}** `{cnb['id']}`")

    lines.append(f"\n### Notes ({len(notes)})")
    for note in notes:
        todo = "[todo] " if note["is_todo"] else ""
        preview = note["body"][:80].replace("\n", " ") if note["body"] else ""
        lines.append(f"- {todo}**{note['title']}** `{note['id']}`\n  _{preview}{'...' if len(note['body']) > 80 else ''}_")
    if not notes:
        lines.append("_(empty)_")

    return "\n".join(lines)


@mcp.tool()
async def create_notebook(title: str, parent_id: str = "") -> str:
    """Create a new notebook (folder).

    Args:
        title: Notebook title
        parent_id: Parent notebook ID for nesting (optional)
    """
    nb_id = uuid.uuid4().hex
    await _put_item(nb_id, _folder_template(nb_id, title, parent_id, _now()))
    return f"Notebook created: **{title}** (ID: `{nb_id}`)"


@mcp.tool()
async def delete_notebook(notebook_id: str, force: bool = False) -> str:
    """Delete a notebook. Refuses if non-empty unless force=True.

    Args:
        notebook_id: The notebook ID to delete
        force: Delete with all contents
    """
    err = _validate_id(notebook_id, "notebook ID")
    if err:
        return err
    idx = await _build_index(force=True)
    nb = idx.get(notebook_id)
    if not nb or nb["type"] != TYPE_FOLDER:
        return f"Notebook {notebook_id} not found."

    children = [v for v in idx.values() if v["parent_id"] == notebook_id and v["type"] in (TYPE_NOTE, TYPE_FOLDER)]
    if children and not force:
        notes = sum(1 for c in children if c["type"] == TYPE_NOTE)
        nbs = sum(1 for c in children if c["type"] == TYPE_FOLDER)
        return f"Notebook **{nb['title']}** is not empty ({notes} notes, {nbs} sub-notebooks). Set force=True to delete."

    deleted = []
    for child in children:
        try:
            await _api("DELETE", f"/api/items/root:/{child['id']}.md:")
            deleted.append(child["title"])
        except Exception:
            pass

    await _api("DELETE", f"/api/items/root:/{notebook_id}.md:")
    for cid in [child["id"] for child in children] + [notebook_id]:
        _index.pop(cid, None)

    result = f"Notebook deleted: **{nb['title']}** (ID: `{notebook_id}`)"
    if deleted:
        result += f"\nAlso deleted {len(deleted)} item(s): {', '.join(deleted)}"
    return result


# -- Notes --

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
            f"- {todo}**{note['title']}** `{note['id']}`\n"
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
    q = query.lower()
    notes = await _get_items_by_type(TYPE_NOTE)
    results = [n for n in notes if q in n["title"].lower() or q in n["body"].lower()][:limit]

    if not results:
        return f"No notes matching '{query}'."
    lines = [f"Search '{query}' ({len(results)} results)\n"]
    for note in results:
        nb_name = await _notebook_name(note["parent_id"])
        preview = note["body"][:120].replace("\n", " ")
        lines.append(
            f"- **{note['title']}** `{note['id']}`\n"
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
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        nb_name = await _notebook_name(parsed["parent_id"])
        await _ensure_resource_index()
        return _format_note_header(parsed, nb_name) + _replace_resource_refs(parsed["body"])
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


@mcp.tool()
async def get_note_full(note_id: str) -> str:
    """Get note with all resources embedded as base64. Can be large.

    Args:
        note_id: The Joplin note ID
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        nb_name = await _notebook_name(parsed["parent_id"])
        await _ensure_resource_index()

        resource_refs = _find_resource_refs(parsed["body"])
        if not resource_refs:
            return _format_note_header(parsed, nb_name) + parsed["body"] + "\n\n_(No resources)_"

        results = await _download_refs(resource_refs)
        output = _format_note_header(parsed, nb_name) + _replace_resource_refs(parsed["body"])
        output += f"\n\n---\n\n## Resources ({len(resource_refs)})\n"
        for rid, data, mime, title in results:
            if data is None:
                output += f"\n### {rid}\nFailed to download.\n"
                continue
            b64 = base64.b64encode(data).decode("ascii")
            output += f"\n### {title or rid}\n- MIME: {mime}\n- Size: {len(data)/1024:.0f} KB\n- ID: `{rid}`\n\ndata:{mime};base64,{b64}\n"
        return output

    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


@mcp.tool()
async def export_note(note_id: str) -> str:
    """Export note as markdown with resources as named base64 blocks.

    Returns markdown body with local file references (e.g. ![](image.jpg))
    and each resource as a separate block: RESOURCE:<filename>:<mime>:<base64>.
    Use this to save a note with all attachments to the local filesystem.

    Args:
        note_id: The Joplin note ID
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        await _ensure_resource_index()

        resource_refs = _find_resource_refs(parsed["body"])
        output = f"TITLE:{parsed['title']}\n\n{_localize_resource_refs(parsed['body'])}"

        if resource_refs:
            for rid, data, mime, title in await _download_refs(resource_refs):
                if data is None:
                    continue
                b64 = base64.b64encode(data).decode("ascii")
                output += f"\n\nRESOURCE:{title or f'{rid}.bin'}:{mime}:{b64}"

        return output

    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


@mcp.tool()
async def create_note(title: str, body: str, notebook_id: str = "") -> str:
    """Create a new note.

    Args:
        title: Note title
        body: Note body in Markdown
        notebook_id: Parent notebook ID (optional)
    """
    note_id = uuid.uuid4().hex
    await _put_item(note_id, _note_template(note_id, title, body, notebook_id, _now()))
    return f"Note created: **{title}** (ID: `{note_id}`)"


@mcp.tool()
async def update_note(
    note_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    notebook_id: Optional[str] = None,
) -> str:
    """Update an existing note.

    Args:
        note_id: The note ID to update
        title: New title (optional)
        body: New body (optional)
        notebook_id: Move to another notebook (optional)
    """
    resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)
    if parsed["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    new_title = title if title is not None else parsed["title"]
    new_body = body if body is not None else parsed["body"]
    now = _now()

    meta = parsed["metadata"]
    meta["updated_time"] = now
    meta["user_updated_time"] = now
    if notebook_id is not None:
        meta["parent_id"] = notebook_id

    content = f"{new_title}\n\n{new_body}\n\n" + "\n".join(f"{k}: {v}" for k, v in meta.items())
    await _put_item(note_id, content)
    return f"Note updated: **{new_title}** (ID: `{note_id}`)"


@mcp.tool()
async def delete_note(note_id: str) -> str:
    """Delete a note by ID.

    Args:
        note_id: The note ID to delete
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
        parsed = _parse_joplin_item(resp.text)
        if parsed["type"] != TYPE_NOTE:
            return f"Item {note_id} is not a note."
        await _api("DELETE", f"/api/items/root:/{note_id}.md:")
        _index.pop(note_id, None)
        return f"Note deleted: **{parsed['title']}** (ID: `{note_id}`)"
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


@mcp.tool()
async def get_notes_batch(note_ids: list[str]) -> str:
    """Read full content of multiple notes at once.

    Args:
        note_ids: List of note IDs to read
    """
    if not note_ids:
        return "No note IDs provided."
    if len(note_ids) > 50:
        return "Too many IDs (max 50)."

    invalid = [nid for nid in note_ids if _validate_id(nid, "")]
    if invalid:
        return f"Invalid note IDs: {', '.join(invalid)}"

    await _ensure_resource_index()

    async def _read_one(nid: str) -> str:
        try:
            parsed = await _fetch_note(nid)
            nb_name = await _notebook_name(parsed["parent_id"])
            return _format_note_header(parsed, nb_name) + _replace_resource_refs(parsed["body"])
        except httpx.HTTPStatusError as e:
            return f"# {nid}\n\nNot found." if e.response.status_code == 404 else f"# {nid}\n\nError: {e}"
        except ValueError as e:
            return f"# {nid}\n\n{e}"

    results = await asyncio.gather(*[_read_one(nid) for nid in note_ids])
    header = f"# Batch read: {len(note_ids)} notes\n\n"
    return header + "\n\n---\n\n".join(results)


@mcp.tool()
async def get_all_notes(
    notebook_id: Optional[str] = None,
    order_by: str = "updated_time",
    order_dir: str = "desc",
    page: int = 1,
    limit: int = 50,
) -> str:
    """Get all notes with optional filtering, pagination, and sorting.

    Args:
        notebook_id: Filter by notebook ID (optional)
        order_by: Sort field: updated_time, created_time, title (default: updated_time)
        order_dir: Sort direction: asc or desc (default: desc)
        page: Page number starting from 1 (default: 1)
        limit: Notes per page, max 100 (default: 50)
    """
    if notebook_id:
        err = _validate_id(notebook_id, "notebook ID")
        if err:
            return err

    limit = max(1, min(limit, 100))
    page = max(1, page)
    order_dir = order_dir.lower()
    if order_dir not in ("asc", "desc"):
        return "order_dir must be 'asc' or 'desc'."
    if order_by not in ("updated_time", "created_time", "title"):
        return "order_by must be 'updated_time', 'created_time', or 'title'."

    notes = await _get_items_by_type(TYPE_NOTE)
    if notebook_id:
        idx = await _build_index()
        nb = idx.get(notebook_id)
        if not nb or nb["type"] != TYPE_FOLDER:
            return f"Notebook {notebook_id} not found."
        notes = [n for n in notes if n["parent_id"] == notebook_id]

    reverse = order_dir == "desc"
    if order_by == "title":
        notes.sort(key=lambda x: x["title"].lower(), reverse=reverse)
    else:
        notes.sort(key=lambda x: x.get(order_by, ""), reverse=reverse)

    total = len(notes)
    start = (page - 1) * limit
    page_notes = notes[start:start + limit]
    total_pages = max(1, (total + limit - 1) // limit)

    if not page_notes:
        return f"No notes found (page {page}/{total_pages})."

    scope = f" in **{(await _build_index()).get(notebook_id, {}).get('title', notebook_id)}**" if notebook_id else ""
    lines = [f"Notes{scope} ({total} total, page {page}/{total_pages})\n"]

    for i, note in enumerate(page_notes, start=start + 1):
        nb_name = await _notebook_name(note["parent_id"])
        todo = "[todo] " if note["is_todo"] else ""
        preview = note["body"][:80].replace("\n", " ") if note["body"] else ""
        lines.append(
            f"{i}. {todo}**{note['title']}** `{note['id']}`\n"
            f"   {nb_name} | {note['updated_time'][:10]}\n"
            f"   _{preview}{'...' if len(note.get('body', '')) > 80 else ''}_"
        )

    if total_pages > 1:
        lines.append(f"\n_Page {page}/{total_pages}. Sorted by {order_by} {order_dir}._")

    return "\n".join(lines)


@mcp.tool()
async def update_notebook(
    notebook_id: str,
    title: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> str:
    """Update a notebook's title or move it to a different parent.

    Args:
        notebook_id: The notebook ID to update
        title: New title (optional)
        parent_id: New parent notebook ID, empty string for root (optional)
    """
    err = _validate_id(notebook_id, "notebook ID")
    if err:
        return err
    if parent_id is not None and parent_id != "":
        err = _validate_id(parent_id, "parent notebook ID")
        if err:
            return err

    if title is None and parent_id is None:
        return "Provide at least title or parent_id to update."

    resp = await _api("GET", f"/api/items/root:/{notebook_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)
    if parsed["type"] != TYPE_FOLDER:
        return f"Item {notebook_id} is not a notebook."

    if parent_id is not None and parent_id == notebook_id:
        return "Cannot move a notebook into itself."
    if parent_id is not None:
        idx = await _build_index()
        check_id = parent_id
        while check_id:
            ancestor = idx.get(check_id)
            if not ancestor:
                break
            if ancestor["parent_id"] == notebook_id:
                return "Cannot move: would create a circular reference."
            check_id = ancestor["parent_id"]

    new_title = title if title is not None else parsed["title"]
    now = _now()
    meta = parsed["metadata"]
    meta["updated_time"] = now
    meta["user_updated_time"] = now
    if parent_id is not None:
        meta["parent_id"] = parent_id

    content = f"{new_title}\n\n" + "\n".join(f"{k}: {v}" for k, v in meta.items())
    await _put_item(notebook_id, content)

    changes = []
    if title is not None:
        changes.append(f"renamed to **{new_title}**")
    if parent_id is not None:
        dest = "root" if not parent_id else f"**{(await _notebook_name(parent_id))}**"
        changes.append(f"moved to {dest}")
    return f"Notebook updated: {', '.join(changes)} (ID: `{notebook_id}`)"


# -- Tags --

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
async def create_tag(title: str) -> str:
    """Create a new tag.

    Args:
        title: Tag title
    """
    tag_id = uuid.uuid4().hex
    await _put_item(tag_id, _tag_template(tag_id, title, _now()))
    return f"Tag created: **{title}** (ID: `{tag_id}`)"


@mcp.tool()
async def delete_tag(tag_id: str) -> str:
    """Delete a tag.

    Args:
        tag_id: The tag ID to delete
    """
    err = _validate_id(tag_id, "tag ID")
    if err:
        return err
    idx = await _build_index(force=True)
    tag = idx.get(tag_id)
    if not tag or tag["type"] != TYPE_TAG:
        return f"Tag {tag_id} not found."

    note_tags = [v for v in idx.values() if v["type"] == TYPE_NOTE_TAG and v["metadata"].get("tag_id") == tag_id]
    for nt in note_tags:
        try:
            await _api("DELETE", f"/api/items/root:/{nt['id']}.md:")
        except Exception:
            pass

    await _api("DELETE", f"/api/items/root:/{tag_id}.md:")
    for nt in note_tags:
        _index.pop(nt["id"], None)
    _index.pop(tag_id, None)
    return f"Tag deleted: **{tag['title']}** (ID: `{tag_id}`, removed from {len(note_tags)} notes)"


@mcp.tool()
async def get_note_tags(note_id: str) -> str:
    """List tags assigned to a note.

    Args:
        note_id: The Joplin note ID
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    idx = await _build_index()
    note = idx.get(note_id)
    if not note or note["type"] != TYPE_NOTE:
        return f"Note {note_id} not found."

    note_tags = [v for v in idx.values() if v["type"] == TYPE_NOTE_TAG and v["metadata"].get("note_id") == note_id]
    if not note_tags:
        return f"Note **{note['title']}** has no tags."

    lines = [f"Tags on '{note['title']}' ({len(note_tags)})\n"]
    for nt in note_tags:
        tag_id = nt["metadata"].get("tag_id", "")
        tag = idx.get(tag_id)
        lines.append(f"- **{tag['title'] if tag else tag_id[:12]}** `{tag_id}`")
    return "\n".join(lines)


@mcp.tool()
async def add_tag_to_note(tag_id: str, note_id: str) -> str:
    """Add a tag to a note.

    Args:
        tag_id: The tag ID
        note_id: The note ID
    """
    for val, label in [(tag_id, "tag ID"), (note_id, "note ID")]:
        err = _validate_id(val, label)
        if err:
            return err
    idx = await _build_index()
    tag = idx.get(tag_id)
    if not tag or tag["type"] != TYPE_TAG:
        return f"Tag {tag_id} not found."
    note = idx.get(note_id)
    if not note or note["type"] != TYPE_NOTE:
        return f"Note {note_id} not found."

    already = any(
        v["type"] == TYPE_NOTE_TAG and v["metadata"].get("note_id") == note_id and v["metadata"].get("tag_id") == tag_id
        for v in idx.values()
    )
    if already:
        return f"Tag **{tag['title']}** already on note **{note['title']}**."

    nt_id = uuid.uuid4().hex
    await _put_item(nt_id, _note_tag_template(nt_id, note_id, tag_id, tag["title"], _now()))
    return f"Tag **{tag['title']}** added to note **{note['title']}**"


@mcp.tool()
async def remove_tag_from_note(tag_id: str, note_id: str) -> str:
    """Remove a tag from a note.

    Args:
        tag_id: The tag ID
        note_id: The note ID
    """
    for val, label in [(tag_id, "tag ID"), (note_id, "note ID")]:
        err = _validate_id(val, label)
        if err:
            return err
    idx = await _build_index()
    note_tags = [
        v for v in idx.values()
        if v["type"] == TYPE_NOTE_TAG and v["metadata"].get("note_id") == note_id and v["metadata"].get("tag_id") == tag_id
    ]
    if not note_tags:
        return "Tag is not assigned to this note."

    for nt in note_tags:
        await _api("DELETE", f"/api/items/root:/{nt['id']}.md:")
        _index.pop(nt["id"], None)

    tag = idx.get(tag_id)
    note = idx.get(note_id)
    return f"Tag **{tag['title'] if tag else tag_id[:12]}** removed from note **{note['title'] if note else note_id[:12]}**"


# -- Resources --

@mcp.tool()
async def get_note_resources(note_id: str) -> str:
    """List resources (images, attachments) in a note.

    Args:
        note_id: The Joplin note ID
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    await _ensure_resource_index()
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
    err = _validate_id(resource_id, "resource ID")
    if err:
        return err
    await _ensure_resource_index()
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
    err = _validate_id(resource_id, "resource ID")
    if err:
        return err
    await _ensure_resource_index()
    res = _resource_index.get(resource_id)
    if res and res["size"] > MAX_RESOURCE_SIZE:
        return f"Resource too large ({res['size'] / 1024 / 1024:.1f} MB). Max 50 MB."

    try:
        data, mime, title = await _download_resource(resource_id)
        b64 = base64.b64encode(data).decode("ascii")
        return f"**{title}** ({mime}, {len(data)/1024:.0f} KB)\n\ndata:{mime};base64,{b64}"
    except httpx.HTTPStatusError as e:
        return f"Resource {resource_id} not found." if e.response.status_code == 404 else str(e)


# -- Entry point --

if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
