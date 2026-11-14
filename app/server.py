"""MCP server for Joplin Server. REST API."""

import asyncio
import base64
import collections
import contextvars
import hashlib
import hmac
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


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Multi-user mode: every request brings its own Joplin credentials in a header.
# The env credentials are neither required nor used in this mode.
MULTI_USER = _env_flag("JOPLIN_MULTI_USER", False)
CREDENTIALS_HEADER = os.environ.get("JOPLIN_CREDENTIALS_HEADER", "X-Joplin-Credentials").strip() \
    or "X-Joplin-Credentials"
ACCOUNT_IDLE_SECONDS = float(os.environ.get("JOPLIN_ACCOUNT_IDLE_MINUTES", "30")) * 60
DISK_CACHE = _env_flag("JOPLIN_DISK_CACHE", True)
CACHE_DIR = os.environ.get("JOPLIN_CACHE_DIR", "/tmp/joplin-cache")
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW = 300.0

if not JOPLIN_SERVER_URL:
    raise RuntimeError("JOPLIN_SERVER_URL is required")
if not MULTI_USER and (not JOPLIN_EMAIL or not JOPLIN_PASSWORD):
    raise RuntimeError("JOPLIN_EMAIL and JOPLIN_PASSWORD are required "
                       "(or set JOPLIN_MULTI_USER=1 to take credentials from a request header)")

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
INDEX_TTL = 120
INDEX_CONCURRENCY = 50
MAX_RESOURCE_SIZE = 50 * 1024 * 1024
# Single-user mode keeps its historical cache file; multi-user mode uses one
# file per account under CACHE_DIR (see Account.cache_path).
INDEX_CACHE_FILE = "/tmp/joplin_index_cache.json"


# -- Accounts --

class AuthError(Exception):
    """Joplin refused the credentials (or none were supplied)."""


class RateLimited(Exception):
    """Too many failed logins for this account."""


def _account_key(email: str) -> str:
    return hashlib.sha256(f"{JOPLIN_SERVER_URL}\n{email.strip().lower()}".encode("utf-8")).hexdigest()


class Account:
    """All per-user state: credentials, session, index and background tasks."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password  # memory only; needed to log in again on 403
        self.key = _account_key(email)
        self.session_id: Optional[str] = None
        self.index: dict[str, dict] = {}
        self.index_ts: float = 0
        self.resource_index: dict[str, dict] = {}
        self.resource_index_ready = False
        self.server_etags: dict[str, str] = {}
        self.pending_puts: set[str] = set()
        self.index_lock = asyncio.Lock()
        self.login_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.last_used = time.time()
        self.cache_loaded = False

    def touch(self) -> None:
        self.last_used = time.time()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        self.tasks.clear()
        self.session_id = None
        self.password = ""

    def cache_path(self) -> Optional[str]:
        if not DISK_CACHE:
            return None
        if not MULTI_USER:
            return INDEX_CACHE_FILE
        return os.path.join(CACHE_DIR, self.key)

    def save_cache(self) -> None:
        path = self.cache_path()
        if not path:
            return
        try:
            data = {
                "index": self.index,
                "resource_index": self.resource_index,
                "server_etags": self.server_etags,
                "ts": self.index_ts,
            }
            if MULTI_USER:
                os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
                os.chmod(CACHE_DIR, 0o700)
            tmp = f"{path}.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"Failed to save index cache: {type(e).__name__}")

    def load_cache(self) -> bool:
        path = self.cache_path()
        if not path:
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            self.index = data.get("index", {})
            self.resource_index = data.get("resource_index", {})
            self.server_etags = data.get("server_etags", {})
            self.index_ts = data.get("ts", 0)
            self.resource_index_ready = bool(self.resource_index)
            logger.info(f"Loaded index from cache: {len(self.index)} items, "
                        f"{len(self.resource_index)} resources")
            return bool(self.index)
        except Exception:
            return False


_current_account: contextvars.ContextVar[Optional[Account]] = contextvars.ContextVar(
    "joplin_account", default=None)
_default_account: Optional[Account] = None
_accounts: dict[str, Account] = {}
_login_failures: dict[str, collections.deque] = {}
_sweeper: Optional[asyncio.Task] = None


def _acct() -> Account:
    """The account of the current request (or the env account in single-user mode)."""
    global _default_account
    acct = _current_account.get()
    if acct is not None:
        return acct
    if MULTI_USER:
        raise AuthError(f"Missing {CREDENTIALS_HEADER} header")
    if _default_account is None:
        _default_account = Account(JOPLIN_EMAIL, JOPLIN_PASSWORD)
    return _default_account


def parse_credentials(value: Optional[str]) -> tuple[str, str]:
    """Split 'email:password' at the first colon (passwords may contain colons)."""
    if not value or not value.strip():
        raise AuthError(f"Missing {CREDENTIALS_HEADER} header")
    email, sep, password = value.strip().partition(":")
    email = email.strip()
    if not sep or not email or not password:
        raise AuthError(f"{CREDENTIALS_HEADER} must be 'email:password'")
    return email, password


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _recent_failures(key: str) -> collections.deque:
    q = _login_failures.setdefault(key, collections.deque())
    cutoff = time.time() - LOGIN_FAILURE_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        _login_failures.pop(key, None)
    return q


async def authenticate(email: str, password: str) -> Account:
    """Return a logged-in Account for these credentials (multi-user mode).

    Raises RateLimited after too many failed logins, AuthError when Joplin
    refuses the credentials.
    """
    key = _account_key(email)
    if len(_recent_failures(key)) >= LOGIN_MAX_FAILURES:
        raise RateLimited("Too many failed logins for this account; try again in a few minutes")
    acct = _accounts.get(key)
    if acct is None:
        acct = _accounts[key] = Account(email, password)
    acct.touch()
    same = hmac.compare_digest(acct.password.encode("utf-8"), password.encode("utf-8"))
    if acct.session_id and same:
        return acct
    async with acct.login_lock:
        if acct.session_id and hmac.compare_digest(acct.password.encode("utf-8"), password.encode("utf-8")):
            return acct
        try:
            await _login(acct, password)
        except AuthError:
            _login_failures.setdefault(key, collections.deque()).append(time.time())
            if not acct.session_id:
                _accounts.pop(key, None)
                acct.close()
            raise
    _login_failures.pop(key, None)
    if not acct.cache_loaded:
        acct.cache_loaded = True
        if not acct.index:
            acct.load_cache()
    return acct


def evict_idle_accounts(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    stale = [k for k, a in _accounts.items() if now - a.last_used > ACCOUNT_IDLE_SECONDS]
    for k in stale:
        _accounts.pop(k).close()
    if stale:
        logger.info(f"Evicted {len(stale)} idle account(s); {len(_accounts)} active")
    return len(stale)


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(max(1.0, min(60.0, ACCOUNT_IDLE_SECONDS / 2)))
        evict_idle_accounts()


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.create_task(_sweep_loop())


_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_id(value: str, label: str = "ID") -> Optional[str]:
    """Return error message if value is not a valid 32-char hex Joplin ID, else None."""
    if not value or not _ID_RE.match(value):
        return f"Invalid {label}: '{value}'. Must be a 32-character hex string."
    return None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _now_ms() -> int:
    """Current time as integer epoch milliseconds (Joplin's raw todo_* format)."""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def _coerce_int(value, default: int = 0) -> int:
    """Best-effort int from Joplin metadata: tolerate '', None, and junk."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _ms_to_date(ms: int) -> str:
    """Render an epoch-ms value as a UTC YYYY-MM-DD date."""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


def _parse_due_ms(due: str) -> int:
    """Parse YYYY-MM-DD or a full ISO-8601 timestamp to epoch ms; '' -> 0 (unset).

    Bare dates are interpreted as UTC midnight. Raises ValueError on junk.
    """
    due = due.strip()
    if not due:
        return 0
    if re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        dt = datetime.datetime.strptime(due, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    else:
        dt = datetime.datetime.fromisoformat(due.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _todo_due_value(note: dict) -> int:
    """todo_due (epoch ms, 0 = unset), tolerant of stale caches lacking the field."""
    return _coerce_int(note.get("todo_due", note.get("metadata", {}).get("todo_due", 0)))


def _todo_completed_value(note: dict) -> int:
    """todo_completed (epoch ms, 0 = open), tolerant of stale caches lacking the field."""
    return _coerce_int(note.get("todo_completed", note.get("metadata", {}).get("todo_completed", 0)))


def _todo_marker(note: dict) -> str:
    """Plain-ASCII to-do marker: '' | '[todo]' [ (due DATE)] [ OVERDUE] | '[done DATE]'."""
    if not note.get("is_todo"):
        return ""
    completed = _todo_completed_value(note)
    if completed:
        return f"[done {_ms_to_date(completed)}]"
    marker = "[todo]"
    due = _todo_due_value(note)
    if due:
        marker += f" (due {_ms_to_date(due)})"
        if due < _now_ms():
            marker += " OVERDUE"
    return marker


def _filter_by_todo(notes: list[dict], todo: Optional[str]) -> tuple[list[dict], Optional[str]]:
    """Filter notes by to-do state. Returns (filtered, error); error is None on success.

    todo: None (no filter), 'all' (any to-do), 'open' (uncompleted), 'done' (completed).
    """
    if todo is None:
        return notes, None
    key = todo.lower()
    if key not in ("all", "open", "done"):
        return notes, "todo must be 'all', 'open', or 'done'."
    notes = [n for n in notes if n["is_todo"]]
    if key == "open":
        notes = [n for n in notes if not _todo_completed_value(n)]
    elif key == "done":
        notes = [n for n in notes if _todo_completed_value(n)]
    return notes, None



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


async def _login(acct: Optional[Account] = None, password: Optional[str] = None) -> str:
    """Log in to Joplin Server. Errors never carry the credentials."""
    acct = acct or _acct()
    pw = acct.password if password is None else password
    client = await _get_client()
    try:
        resp = await client.post(
            f"{JOPLIN_SERVER_URL}/api/sessions",
            json={"email": acct.email, "password": pw},
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach Joplin Server ({type(e).__name__})") from None
    if resp.status_code in (400, 401, 403):
        raise AuthError("Joplin rejected the credentials")
    if resp.status_code >= 400:
        raise RuntimeError(f"Joplin login failed (HTTP {resp.status_code})")
    acct.session_id = resp.json()["id"]
    acct.password = pw
    logger.info("Authenticated with Joplin Server")
    return acct.session_id


async def _get_session() -> str:
    acct = _acct()
    if acct.session_id:
        return acct.session_id
    return await _login(acct)


async def _api(method: str, path: str, **kwargs) -> httpx.Response:
    """Make API request, re-login on 403."""
    acct = _acct()
    acct.touch()
    token = await _get_session()
    extra_headers = kwargs.pop("headers", {})
    headers = {"X-API-AUTH": token, **extra_headers}

    client = await _get_client()
    resp = await client.request(method, f"{JOPLIN_SERVER_URL}{path}", headers=headers, **kwargs)
    if resp.status_code == 403:
        acct.session_id = None
        token = await _login(acct)
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
        _acct().index[parsed["id"]] = parsed
        _acct().pending_puts.add(parsed["id"])


async def _put_note(note_id: str, title: str, body: str, meta: dict) -> None:
    """Write a note back with refreshed timestamps.

    Metadata key order is preserved, so the serialized item stays identical
    apart from the fields that actually changed.
    """
    now = _now()
    meta["updated_time"] = now
    meta["user_updated_time"] = now
    content = f"{title}\n\n{body}\n\n" + "\n".join(f"{k}: {v}" for k, v in meta.items())
    await _put_item(note_id, content)


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
        "todo_due": _coerce_int(metadata.get("todo_due", 0)),
        "todo_completed": _coerce_int(metadata.get("todo_completed", 0)),
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
    _acct().save_cache()


def _load_index_cache() -> bool:
    return _acct().load_cache()


async def _fetch_item_content(name: str) -> Optional[dict]:
    try:
        resp = await _api("GET", f"/api/items/root:/{name}:/content")
        parsed = _parse_joplin_item(resp.text)
        return None if parsed["type"] == TYPE_REVISION else parsed
    except Exception:
        return None


async def _do_build_index() -> dict[str, dict]:
    """Incremental index: only fetch new/changed items based on server updated_time."""
    a = _acct()

    lock = a.index_lock
    if lock.locked():
        return a.index

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
            if a.server_etags.get(item_id) != server_ut:
                to_fetch.append((it["name"], item_id, server_ut))

        a.pending_puts.intersection_update(a.index)
        a.pending_puts.difference_update(server_ids)
        deleted = [k for k in list(a.index) if k not in server_ids
                   and k not in a.pending_puts and a.index[k].get("type") != TYPE_RESOURCE]

        if not to_fetch and not deleted:
            a.index_ts = time.time()
            logger.info(f"Index up to date: {len(a.index)} items ({time.time() - t0:.1f}s)")
            return a.index

        logger.info(f"Syncing index: {len(to_fetch)} changed, {len(deleted)} deleted"
                     f" (of {len(md_items)} total)")

        sem = asyncio.Semaphore(INDEX_CONCURRENCY)

        async def _fetch_with_sem(name: str) -> Optional[dict]:
            async with sem:
                return await _fetch_item_content(name)

        results = await asyncio.gather(*[_fetch_with_sem(name) for name, _, _ in to_fetch])
        for (_, item_id, server_ut), parsed in zip(to_fetch, results):
            a.server_etags[item_id] = server_ut
            if parsed and parsed["id"]:
                a.index[parsed["id"]] = parsed

        for item_id in deleted:
            a.index.pop(item_id, None)
            a.server_etags.pop(item_id, None)

        a.index_ts = time.time()
        a.resource_index_ready = False
        _save_index_cache()
        logger.info(f"Index: {len(a.index)} items, synced {len(to_fetch)} in {time.time() - t0:.1f}s")
        return a.index


async def _build_index(force: bool = False) -> dict[str, dict]:
    """Return index, triggering background refresh if TTL expired."""
    a = _acct()
    if not force and a.index and (time.time() - a.index_ts) < INDEX_TTL:
        return a.index

    if a.index:
        a.spawn(_do_build_index())
        return a.index

    return await _do_build_index()


async def _ensure_resource_index():
    """Build resource index lazily on first access."""
    a = _acct()
    if a.resource_index_ready:
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

    a.resource_index = new_res_index
    a.resource_index_ready = True
    _save_index_cache()
    logger.info(f"Resource index: {len(a.resource_index)} resources in {time.time() - t0:.1f}s")


async def _get_items_by_type(type_id: int, force_refresh: bool = False) -> list[dict]:
    idx = await _build_index(force=force_refresh)
    return [v for v in idx.values() if v["type"] == type_id]


async def _notebook_name(parent_id: str) -> str:
    if not parent_id:
        return "(root)"
    idx = await _build_index()
    item = idx.get(parent_id)
    return item["title"] if item else parent_id[:8]


async def _note_tags_map() -> dict[str, list[str]]:
    idx = await _build_index()
    result: dict[str, list[str]] = {}
    for item in idx.values():
        if item["type"] != TYPE_NOTE_TAG:
            continue
        note_id = item["metadata"].get("note_id")
        tag = idx.get(item["metadata"].get("tag_id"))
        if note_id and tag:
            result.setdefault(note_id, []).append(tag["title"])
    return result


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
    res = _acct().resource_index.get(resource_id)
    resp = await _api("GET", f"/api/items/root:/.resource/{resource_id}:/content")
    return resp.content, res["mime"] if res else "application/octet-stream", res["title"] if res else resource_id


def _format_note_header(parsed: dict, nb_name: str) -> str:
    marker = _todo_marker(parsed)
    todo = f" {marker}" if marker else ""
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
        res = _acct().resource_index.get(ref_id)
        label = f"{res['title']} ({res['mime']}, {res['size']/1024:.0f}KB)" if res else f"resource:{ref_id}"
        body = re.sub(rf'<img\s[^>]*src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = re.sub(rf'<img\s+src=":/{ ref_id}"[^>]*/?>', f"[{label}]", body)
        body = body.replace(f"(:/{ref_id})", f"({label})")
    return body


def _localize_resource_refs(body: str) -> str:
    """Replace Joplin resource refs with local filenames."""
    for ref_id in _find_resource_refs(body):
        res = _acct().resource_index.get(ref_id)
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


# -- Note body editing --
#
# Partial edits are resolved server-side: a caller sends only the fragment it
# wants added or changed, never the surrounding body. That keeps large notes
# cheap to edit and makes it impossible to corrupt untouched text (including
# `:/resource` references, which the read tools render as human-readable names).

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$")


def _headings(body: str) -> list[dict]:
    """Markdown headings outside fenced code blocks, in document order.

    Each entry carries level, title, line (1-based), start (offset of the
    heading line) and content_start (offset just past that line).
    """
    result: list[dict] = []
    offset = 0
    fence = ""
    for lineno, line in enumerate(body.split("\n"), start=1):
        stripped = line.strip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
        elif stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
        else:
            m = _HEADING_RE.match(line)
            if m:
                result.append({
                    "level": len(m.group(1)),
                    "title": m.group(2).strip(),
                    "line": lineno,
                    "start": offset,
                    "content_start": min(offset + len(line) + 1, len(body)),
                })
        offset += len(line) + 1
    return result


def _section_end(heads: list[dict], index: int, body_len: int) -> int:
    """Offset where a section ends: at the next heading of the same or higher level."""
    level = heads[index]["level"]
    for nxt in heads[index + 1:]:
        if nxt["level"] <= level:
            return nxt["start"]
    return body_len


def _outline(body: str) -> list[dict]:
    """Headings annotated with the size of the span they own (subsections included)."""
    heads = _headings(body)
    for i, head in enumerate(heads):
        head["chars"] = _section_end(heads, i, len(body)) - head["start"]
    return heads


def _find_section(body: str, section: str) -> dict:
    """Resolve a section reference to a span, or raise ValueError explaining why not.

    Accepts bare heading text ("Hosts") or the full line ("## Hosts"); when the
    hashes are given the level must match too. Matching is exact, then
    case-insensitive, then a unique case-insensitive substring.
    """
    heads = _headings(body)
    if not heads:
        raise ValueError("Note has no markdown headings; omit `section` to edit the whole body.")

    wanted = section.strip()
    level = 0
    m = re.match(r"^(#{1,6})[ \t]*", wanted)
    if m:
        level = len(m.group(1))
        wanted = wanted[m.end():].strip()
    if not wanted:
        raise ValueError(f"Invalid section reference: '{section}' (expected e.g. 'Hosts' or '## Hosts').")

    def pick(match) -> list[int]:
        return [i for i, h in enumerate(heads)
                if (not level or h["level"] == level) and match(h["title"])]

    found = (pick(lambda t: t == wanted)
             or pick(lambda t: t.lower() == wanted.lower())
             or pick(lambda t: wanted.lower() in t.lower()))

    if not found:
        listing = ", ".join(f"'{h['title']}'" for h in heads[:30])
        more = f" (+{len(heads) - 30} more, see get_note_outline)" if len(heads) > 30 else ""
        raise ValueError(f"Section '{section}' not found. Headings: {listing}{more}.")
    if len(found) > 1:
        where = "; ".join(f"line {heads[i]['line']}: {'#' * heads[i]['level']} {heads[i]['title']}"
                         for i in found[:10])
        raise ValueError(f"Section '{section}' is ambiguous ({len(found)} matches): {where}. "
                         "Give the full heading text with its '#' prefix, or use replace_in_note.")

    head = dict(heads[found[0]])
    head["end"] = _section_end(heads, found[0], len(body))
    return head


def _section_label(head: dict) -> str:
    return f"'{'#' * head['level']} {head['title']}' (line {head['line']})"


def _splice(body: str, start: int, end: int, text: str,
            position: str, separator: str) -> tuple[str, int]:
    """Insert `text` at the start or end of the body[start:end] span.

    Returns the new body and the offset the inserted text landed at. Blank-line
    padding is normalised inside the touched span only.
    """
    before, segment, after = body[:start], body[start:end], body[end:]
    text, inner = text.strip("\n"), segment.strip("\n")
    lead = "\n" if before and not before.endswith("\n\n") else ""
    if position == "start":
        merged = f"{text}{separator}{inner}" if inner else text
        at = len(before) + len(lead)
    else:
        merged = f"{inner}{separator}{text}" if inner else text
        at = len(before) + len(lead) + len(merged) - len(text)
    tail = "\n\n" if after else ""
    return f"{before}{lead}{merged}{tail}{after}", at


def _insert_block(body: str, cut: int, text: str) -> tuple[str, int]:
    """Insert `text` as a standalone block at offset `cut`.

    Strictly additive: nothing already in the body is rewritten, and blank lines
    are added only where the join would otherwise glue the block to its
    neighbour. This is what `before`/`after` use, and it is why they are safer
    than `start`/`end` — those normalise blank-line padding inside the span they
    edit.
    """
    before, after = body[:cut], body[cut:]
    text = text.strip("\n")
    if not before or before.endswith("\n\n"):
        lead = ""
    elif before.endswith("\n"):
        lead = "\n"
    else:
        lead = "\n\n"
    tail = "\n\n" if after else ""
    return f"{before}{lead}{text}{tail}{after}", len(before) + len(lead)


def _line_of(body: str, offset: int) -> int:
    return body.count("\n", 0, max(offset, 0)) + 1


def _context_lines(body: str, offset: int, radius: int = 4) -> str:
    """Numbered lines around `offset`, so a caller can see what a write actually did."""
    lines = body.split("\n")
    target = _line_of(body, offset) - 1
    lo, hi = max(0, target - radius), min(len(lines), target + radius + 1)
    width = len(str(hi))
    out = []
    for i in range(lo, hi):
        text = lines[i] if len(lines[i]) <= 200 else lines[i][:197] + "..."
        out.append(f"{str(i + 1).rjust(width)} | {text}")
    return "\n".join(out)


def _edit_report(action: str, parsed: dict, old_body: str, new_body: str,
                 focus: int, detail: str, dry_run: bool) -> str:
    """Confirm a body edit: before/after counts plus the changed region."""
    old_lines, new_lines = old_body.count("\n") + 1, new_body.count("\n") + 1
    prefix = "DRY RUN (nothing written) — " if dry_run else ""
    return (
        f"{prefix}{action}: **{parsed['title']}** (ID: `{parsed['id']}`)\n"
        f"{detail}\n"
        f"Chars {len(old_body)} -> {len(new_body)} ({len(new_body) - len(old_body):+}), "
        f"lines {old_lines} -> {new_lines} ({new_lines - old_lines:+}), "
        f"headings {len(_headings(old_body))} -> {len(_headings(new_body))}\n\n"
        f"Context around line {_line_of(new_body, focus)}:\n"
        f"```\n{_context_lines(new_body, focus)}\n```"
    )


async def _apply_body_edit(note_id: str, action: str, transform, dry_run: bool) -> str:
    """Fetch a note, run transform(body) -> (new_body, focus, detail), write, report.

    `transform` raises ValueError to reject the edit (bad anchor, ambiguous
    section); returning the body unchanged reports a no-op instead of writing.
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        old_body = parsed["body"]
        new_body, focus, detail = transform(old_body)
        if new_body == old_body:
            return f"No change ({detail}): **{parsed['title']}** (ID: `{note_id}`)"
        if not dry_run:
            await _put_note(note_id, parsed["title"], new_body, parsed["metadata"])
        return _edit_report(action, parsed, old_body, new_body, focus, detail, dry_run)
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


# -- Joplin item templates --

async def _parent_share_id(parent_id: str) -> str:
    """Return the share_id of the parent notebook, or '' if no parent / not shared / missing.

    Joplin propagates a notebook's share_id to all descendants; new notes and
    sub-notebooks must inherit it or they will not appear for other participants
    of the share.
    """
    if not parent_id:
        return ""
    try:
        resp = await _api("GET", f"/api/items/root:/{parent_id}.md:/content")
        parsed = _parse_joplin_item(resp.text)
        return parsed.get("metadata", {}).get("share_id", "") or ""
    except Exception:
        return ""


def _note_template(note_id: str, title: str, body: str, notebook_id: str, now: str,
                   share_id: str = "", is_todo: bool = False, due_ms: int = 0) -> str:
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
is_todo: {1 if is_todo else 0}
todo_due: {due_ms}
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
share_id: {share_id}
conflict_original_id: 
master_key_id: 
user_data: 
deleted_time: 0
type_: 1"""


def _folder_template(folder_id: str, title: str, parent_id: str, now: str, share_id: str = "") -> str:
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
share_id: {share_id}
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
    if not MULTI_USER:
        acct = _acct()
        if not acct.index:
            cached = acct.load_cache()
            if not cached or (time.time() - acct.index_ts) > INDEX_TTL:
                acct.spawn(_do_build_index())
    yield {}

mcp = FastMCP(
    "joplin-server",
    instructions="Access notes, notebooks, and tags in Joplin Server",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8081")),
    lifespan=_lifespan,
    # Multi-user: no server-side MCP sessions, so every request is executed with
    # the credentials of that very request (and none can be replayed by session id).
    stateless_http=MULTI_USER,
)


# -- Connection --

@mcp.tool()
async def ping_joplin() -> str:
    """Check connectivity to Joplin Server."""
    try:
        await _get_session()
        return f"Connected to {JOPLIN_SERVER_URL}"
    except Exception as e:
        return f"Connection failed: {_scrub(str(e), _acct().password)}"


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
        marker = _todo_marker(note)
        todo = f"{marker} " if marker else ""
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
    share_id = await _parent_share_id(parent_id)
    await _put_item(nb_id, _folder_template(nb_id, title, parent_id, _now(), share_id))
    return f"Notebook created: **{title}** (ID: `{nb_id}`)"


@mcp.tool()
async def get_or_create_notebook(path: str) -> str:
    """Resolve a "/"-separated notebook path, creating any missing levels.

    Args:
        path: Notebook path, e.g. "Work/Projects/Website"
    """
    await _build_index(force=True)
    parent_id = ""
    created = []
    for name in [p.strip() for p in path.split("/") if p.strip()]:
        match = next((v for v in _acct().index.values() if v["type"] == TYPE_FOLDER
                      and v["parent_id"] == parent_id and v["title"] == name), None)
        if match:
            parent_id = match["id"]
            continue
        nb_id = uuid.uuid4().hex
        share_id = await _parent_share_id(parent_id)
        await _put_item(nb_id, _folder_template(nb_id, name, parent_id, _now(), share_id))
        parent_id = nb_id
        created.append(name)
    suffix = f" — created: {', '.join(created)}" if created else " — all existed"
    return f"Notebook path **{path}** ready (leaf ID: `{parent_id}`){suffix}"


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
        _acct().index.pop(cid, None)

    result = f"Notebook deleted: **{nb['title']}** (ID: `{notebook_id}`)"
    if deleted:
        result += f"\nAlso deleted {len(deleted)} item(s): {', '.join(deleted)}"
    return result


# -- Notes --

@mcp.tool()
async def list_notes(notebook_id: Optional[str] = None, limit: int = 50, tag: Optional[str] = None,
                     todo: Optional[str] = None) -> str:
    """List notes, optionally filtered by notebook and/or tag.

    Args:
        notebook_id: Filter by notebook ID (optional)
        limit: Max notes to return (default 50)
        tag: Filter by tag name (optional)
        todo: Filter by to-do state: 'all' (any to-do), 'open' (uncompleted),
            'done' (completed), or omit for no filtering (optional)
    """
    notes = await _get_items_by_type(TYPE_NOTE)
    if notebook_id:
        notes = [n for n in notes if n["parent_id"] == notebook_id]
    note_tags = await _note_tags_map()
    if tag:
        wanted = tag.lower()
        notes = [n for n in notes if any(t.lower() == wanted for t in note_tags.get(n["id"], []))]
    notes, err = _filter_by_todo(notes, todo)
    if err:
        return err
    notes.sort(key=lambda x: x["updated_time"], reverse=True)
    notes = notes[:limit]

    if not notes:
        return "No notes found."
    lines = [f"Notes ({len(notes)})\n"]
    for note in notes:
        nb_name = await _notebook_name(note["parent_id"])
        tags = note_tags.get(note["id"], [])
        tagstr = f" [{', '.join('#' + t for t in tags)}]" if tags else ""
        marker = _todo_marker(note)
        todo = f"{marker} " if marker else ""
        preview = note["body"][:80].replace("\n", " ") if note["body"] else ""
        lines.append(
            f"- {todo}**{note['title']}** `{note['id']}`\n"
            f"  {nb_name} | {note['updated_time'][:10]}{tagstr}\n"
            f"  _{preview}{'...' if len(note['body']) > 80 else ''}_"
        )
    return "\n".join(lines)


@mcp.tool()
async def search_notes(query: str, limit: int = 20, scope: str = "all",
                       notebook_id: Optional[str] = None, tag: Optional[str] = None) -> str:
    """Search notes by text. All query terms must match (AND).

    Args:
        query: Space-separated terms; a note must contain all of them
        limit: Max results (default 20)
        scope: Where to match — "title", "body", or "all" (default)
        notebook_id: Restrict to a notebook (optional)
        tag: Restrict to notes carrying this tag name (optional)
    """
    terms = query.lower().split()
    notes = await _get_items_by_type(TYPE_NOTE)
    if notebook_id:
        notes = [n for n in notes if n["parent_id"] == notebook_id]

    note_tags = await _note_tags_map()
    if tag:
        wanted = tag.lower()
        notes = [n for n in notes if any(t.lower() == wanted for t in note_tags.get(n["id"], []))]

    def fields(n):
        title, body = n["title"].lower(), (n["body"] or "").lower()
        if scope == "title":
            return title, ""
        if scope == "body":
            return "", body
        return title, body

    def score(n):
        title, body = fields(n)
        return sum(3 if t in title else 1 for t in terms if t in title or t in body)

    results = [n for n in notes if all(t in " ".join(fields(n)) for t in terms)]
    results.sort(key=lambda n: (score(n), n["updated_time"]), reverse=True)
    results = results[:limit]

    if not results:
        return f"No notes matching '{query}'."
    lines = [f"Search '{query}' ({len(results)} results)\n"]
    for note in results:
        nb_name = await _notebook_name(note["parent_id"])
        tags = note_tags.get(note["id"], [])
        tagstr = f" [{', '.join('#' + t for t in tags)}]" if tags else ""
        preview = (note["body"] or "")[:120].replace("\n", " ")
        lines.append(
            f"- **{note['title']}** `{note['id']}`\n"
            f"  {nb_name} | {note['updated_time'][:10]}{tagstr}\n"
            f"  _{preview}{'...' if len(note['body'] or '') > 120 else ''}_"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_note(note_id: str, raw: bool = False) -> str:
    """Get note text content (without resource files). Use get_note_full for embedded resources.

    Args:
        note_id: The Joplin note ID
        raw: Return the body verbatim — no header, `:/<id>` resource references
            left intact — so it can be copied as an exact anchor for replace_in_note
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        if raw:
            return parsed["body"]
        nb_name = await _notebook_name(parsed["parent_id"])
        await _ensure_resource_index()
        return _format_note_header(parsed, nb_name) + _replace_resource_refs(parsed["body"])
    except ValueError as e:
        return str(e)
    except httpx.HTTPStatusError as e:
        return f"Note {note_id} not found." if e.response.status_code == 404 else str(e)


@mcp.tool()
async def get_note_outline(note_id: str) -> str:
    """Map a note's structure: headings with line numbers and sizes.

    A cheap way to navigate a large note before editing it — pick a heading from
    here and hand it to append_to_note or replace_section instead of reading the
    whole body. Sizes cover each heading's span, subsections included.

    Args:
        note_id: The Joplin note ID
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    try:
        parsed = await _fetch_note(note_id)
        body = parsed["body"]
        heads = _outline(body)
        nlines = body.count("\n") + 1
        nb_name = await _notebook_name(parsed["parent_id"])
        lines = [
            f"# {parsed['title']} — outline",
            f"Notebook: {nb_name} | Updated: {parsed['updated_time']} | ID: `{parsed['id']}`",
            f"Body: {len(body)} chars, {nlines} lines, {len(heads)} headings",
        ]
        if not heads:
            return "\n".join(lines) + "\n\nNo markdown headings — edit it with append_to_note or replace_in_note."

        width = max(len(str(h["line"])) for h in heads)
        lines.append("\n```")
        lines.append(f"{'line'.rjust(width)}  {'chars'.rjust(7)}  heading")
        for h in heads:
            indent = "  " * (h["level"] - 1)
            lines.append(f"{str(h['line']).rjust(width)}  {str(h['chars']).rjust(7)}  "
                         f"{indent}{'#' * h['level']} {h['title']}")
        lines.append("```")
        return "\n".join(lines)
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
async def create_note(title: str, body: str, notebook_id: str = "",
                      is_todo: bool = False, due: Optional[str] = None) -> str:
    """Create a new note.

    Args:
        title: Note title
        body: Note body in Markdown
        notebook_id: Parent notebook ID (optional)
        is_todo: Create the note as a to-do (optional, default False)
        due: Due date as YYYY-MM-DD or a full ISO-8601 timestamp; setting it
            implies is_todo. Bare dates are UTC midnight (optional)
    """
    due_ms = 0
    if due:
        try:
            due_ms = _parse_due_ms(due)
        except ValueError:
            return f"Invalid due date: '{due}'. Use YYYY-MM-DD or an ISO-8601 timestamp."
    is_todo = is_todo or bool(due_ms)
    note_id = uuid.uuid4().hex
    share_id = await _parent_share_id(notebook_id)
    await _put_item(note_id, _note_template(note_id, title, body, notebook_id, _now(),
                                            share_id, is_todo=is_todo, due_ms=due_ms))
    suffix = " (to-do)" if is_todo else ""
    return f"Note created: **{title}**{suffix} (ID: `{note_id}`)"


@mcp.tool()
async def update_note(
    note_id: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    notebook_id: Optional[str] = None,
) -> str:
    """Update an existing note, replacing the whole body.

    `body` must be the complete new text, so for anything short of a full
    rewrite prefer append_to_note, replace_in_note or replace_section: they
    splice server-side and never make you reproduce existing content.

    Args:
        note_id: The note ID to update
        title: New title (optional)
        body: New body — replaces the entire body (optional)
        notebook_id: Move to another notebook (optional)
    """
    resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)
    if parsed["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    new_title = title if title is not None else parsed["title"]
    new_body = body if body is not None else parsed["body"]

    meta = parsed["metadata"]
    if notebook_id is not None:
        meta["parent_id"] = notebook_id
        meta["share_id"] = await _parent_share_id(notebook_id)

    await _put_note(note_id, new_title, new_body, meta)
    return f"Note updated: **{new_title}** (ID: `{note_id}`)"


@mcp.tool()
async def append_to_note(
    note_id: str,
    text: str,
    section: Optional[str] = None,
    position: str = "end",
    separator: str = "\n\n",
    if_absent: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """Add text to a note without resending its existing body.

    The note is read and spliced server-side, so everything already there —
    including resource links — is preserved byte for byte. Prefer this over
    update_note whenever you are only adding content: update_note replaces the
    whole body and therefore requires you to reproduce it exactly.

    Args:
        note_id: The note ID
        text: Markdown to insert
        section: Heading to insert under, e.g. "Hosts" or "## Hosts". Applies to
            the whole note when omitted (optional)
        position: Where the text goes.
            Inside the section (or note): "end" (default) or "start".
            Beside a named section, as a sibling: "before" its heading, or
            "after" its whole span — subsections included. These two require
            `section`, ignore `separator`, and never rewrite an existing byte,
            which makes "before" the way to put a new entry at the top of a
            newest-first log that opens with a preamble.
        separator: Text placed between the existing content and the insert for
            "start"/"end" (default: a blank line; pass "\\n" for table rows or
            list items). Ignored by "before"/"after"
        if_absent: Skip the write when this string is already in the body — an
            idempotency guard for re-runs (optional)
        dry_run: Report the change and its context without writing
    """
    if position not in ("end", "start", "before", "after"):
        return "position must be 'end', 'start', 'before' or 'after'."
    if position in ("before", "after") and section is None:
        return (f"position '{position}' needs a `section` to sit beside — "
                "for the whole note use 'start' or 'end'.")
    if not text:
        return "Nothing to append: `text` is empty."

    def transform(body: str):
        if if_absent and if_absent in body:
            return body, 0, f"'{if_absent[:60]}' already present"
        if position in ("before", "after"):
            head = _find_section(body, section)
            cut = head["start"] if position == "before" else head["end"]
            new_body, at = _insert_block(body, cut, text)
            return (new_body, at,
                    f"Inserted {len(text)} chars {position} section {_section_label(head)}")
        if section is None:
            start, end, where = 0, len(body), "the note"
        else:
            head = _find_section(body, section)
            start, end, where = head["content_start"], head["end"], f"section {_section_label(head)}"
        new_body, at = _splice(body, start, end, text, position, separator)
        return new_body, at, f"Inserted {len(text)} chars at the {position} of {where}"

    return await _apply_body_edit(note_id, "Appended to", transform, dry_run)


@mcp.tool()
async def replace_in_note(
    note_id: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    dry_run: bool = False,
) -> str:
    """Replace an exact string inside a note body — a surgical edit.

    Only the fragment crosses the wire; the rest of the note is never rewritten.
    Refuses to act when `old_text` is missing, or matches more than once without
    replace_all, so a bad anchor cannot silently mangle a note. Pass an empty
    `new_text` to delete the fragment.

    Args:
        note_id: The note ID
        old_text: Exact text to find — copy it verbatim from get_note(raw=True)
        new_text: Replacement text ("" deletes the anchor)
        replace_all: Replace every occurrence instead of demanding a unique match
        dry_run: Report the change and its context without writing
    """
    if not old_text:
        return "`old_text` must not be empty — use append_to_note to add content."

    def transform(body: str):
        count = body.count(old_text)
        if count == 0:
            hint = ""
            if old_text.strip() and old_text.strip() in body:
                hint = (" A whitespace-trimmed version does match, so leading or trailing"
                        " spaces/newlines differ.")
            elif "\n" in old_text:
                first = old_text.strip().split("\n")[0].strip()
                if first and first in body:
                    hint = (f" Its first line ('{first[:60]}') is present, so the lines"
                            " after it differ.")
            raise ValueError(f"old_text not found in note {note_id}.{hint}"
                             " Read the note with get_note(raw=True) and copy the anchor verbatim.")
        if count > 1 and not replace_all:
            at, lines = -1, []
            for _ in range(min(count, 10)):
                at = body.find(old_text, at + 1)
                lines.append(str(_line_of(body, at)))
            raise ValueError(f"old_text matches {count} times (lines {', '.join(lines)}). "
                             "Extend the anchor to make it unique, or pass replace_all=True.")

        at = body.find(old_text)
        if replace_all:
            new_body = body.replace(old_text, new_text)
        else:
            new_body = body[:at] + new_text + body[at + len(old_text):]
        verb = "Deleted" if not new_text else "Replaced"
        hits = count if replace_all else 1
        return new_body, at, f"{verb} {hits} occurrence(s) of a {len(old_text)}-char anchor"

    return await _apply_body_edit(note_id, "Edited", transform, dry_run)


@mcp.tool()
async def replace_section(note_id: str, section: str, text: str, dry_run: bool = False) -> str:
    """Replace everything under a heading, keeping the heading line itself.

    Rewrites one section of a large note without resending — or even reading —
    the rest of it. Pass an empty `text` to empty the section.

    Args:
        note_id: The note ID
        section: Heading whose content to replace, e.g. "Hosts" or "## Hosts"
        text: New Markdown content for that section
        dry_run: Report the change and its context without writing
    """
    def transform(body: str):
        head = _find_section(body, section)
        before, after = body[:head["content_start"]], body[head["end"]:]
        old_len = len(body[head["content_start"]:head["end"]].strip("\n"))
        inner = text.strip("\n")
        if inner:
            lead = "\n" if before and not before.endswith("\n\n") else ""
            tail = "\n\n" if after else ""
            new_body = f"{before}{lead}{inner}{tail}{after}"
            at = len(before) + len(lead)
        else:
            gap = "\n" if after else ""
            new_body, at = f"{before}{gap}{after}", len(before)
        return (new_body, at,
                f"Replaced section {_section_label(head)}: {old_len} -> {len(inner)} chars")

    return await _apply_body_edit(note_id, "Section replaced in", transform, dry_run)


@mcp.tool()
async def set_todo(
    note_id: str,
    is_todo: Optional[bool] = None,
    completed: Optional[bool] = None,
    due: Optional[str] = None,
) -> str:
    """Set or clear a note's to-do state, completion, and due date.

    Args:
        note_id: The note ID to modify
        is_todo: Make the note a to-do (True) or a plain note (False). Setting
            False also clears the due date and completion. (optional)
        completed: Mark the to-do done (True) or reopen it (False). Completing a
            note also forces it to be a to-do. (optional)
        due: Due date as YYYY-MM-DD or a full ISO-8601 timestamp; "" clears it.
            Setting a due date also forces the note to be a to-do. Bare dates are
            interpreted as UTC midnight. (optional)
    """
    err = _validate_id(note_id, "note ID")
    if err:
        return err
    if is_todo is None and completed is None and due is None:
        return "Provide at least one of is_todo, completed, or due to change."

    # Parse due up front so an invalid value fails before any write.
    due_ms = None
    if due is not None:
        try:
            due_ms = _parse_due_ms(due)
        except ValueError:
            return f"Invalid due date: '{due}'. Use YYYY-MM-DD, an ISO-8601 timestamp, or '' to clear."

    resp = await _api("GET", f"/api/items/root:/{note_id}.md:/content")
    parsed = _parse_joplin_item(resp.text)
    if parsed["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    meta = parsed["metadata"]
    changes = []
    if due is not None:
        meta["todo_due"] = due_ms
        if due_ms:
            meta["is_todo"] = 1
            changes.append(f"due set to {_ms_to_date(due_ms)}")
        else:
            changes.append("due cleared")

    if completed is not None:
        if completed:
            done_ms = _now_ms()
            meta["todo_completed"] = done_ms
            meta["is_todo"] = 1
            changes.append(f"marked done ({_ms_to_date(done_ms)})")
        else:
            meta["todo_completed"] = 0
            changes.append("reopened")

    # Applied last: converting to a plain note overrides the flags above.
    if is_todo is not None:
        if is_todo:
            meta["is_todo"] = 1
            changes.append("marked as to-do")
        else:
            meta["is_todo"] = 0
            meta["todo_due"] = 0
            meta["todo_completed"] = 0
            changes.append("converted to plain note")

    await _put_note(note_id, parsed["title"], parsed["body"], meta)
    return f"Note updated: **{parsed['title']}** — {', '.join(changes)} (ID: `{note_id}`)"


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
        _acct().index.pop(note_id, None)
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
    todo: Optional[str] = None,
) -> str:
    """Get all notes with optional filtering, pagination, and sorting.

    Args:
        notebook_id: Filter by notebook ID (optional)
        order_by: Sort field: updated_time, created_time, title, todo_due,
            todo_completed (default: updated_time)
        order_dir: Sort direction: asc or desc (default: desc)
        page: Page number starting from 1 (default: 1)
        limit: Notes per page, max 100 (default: 50)
        todo: Filter by to-do state: 'all' (any to-do), 'open' (uncompleted),
            'done' (completed), or omit for no filtering (optional)
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
    if order_by not in ("updated_time", "created_time", "title", "todo_due", "todo_completed"):
        return ("order_by must be 'updated_time', 'created_time', 'title', "
                "'todo_due', or 'todo_completed'.")

    notes = await _get_items_by_type(TYPE_NOTE)
    if notebook_id:
        idx = await _build_index()
        nb = idx.get(notebook_id)
        if not nb or nb["type"] != TYPE_FOLDER:
            return f"Notebook {notebook_id} not found."
        notes = [n for n in notes if n["parent_id"] == notebook_id]

    notes, err = _filter_by_todo(notes, todo)
    if err:
        return err

    reverse = order_dir == "desc"
    if order_by == "title":
        notes.sort(key=lambda x: x["title"].lower(), reverse=reverse)
    elif order_by in ("todo_due", "todo_completed"):
        valfn = _todo_due_value if order_by == "todo_due" else _todo_completed_value
        # Unset (0) values always sort last, regardless of direction.
        present = sorted((n for n in notes if valfn(n) != 0), key=valfn, reverse=reverse)
        absent = [n for n in notes if valfn(n) == 0]
        notes = present + absent
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
        marker = _todo_marker(note)
        todo = f"{marker} " if marker else ""
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
        meta["share_id"] = await _parent_share_id(parent_id)

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
        _acct().index.pop(nt["id"], None)
    _acct().index.pop(tag_id, None)
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
        _acct().index.pop(nt["id"], None)

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
    note = _acct().index.get(note_id)
    if not note:
        return f"Note {note_id} not found."
    if note["type"] != TYPE_NOTE:
        return f"Item {note_id} is not a note."

    refs = _find_resource_refs(note["body"])
    if not refs:
        return f"Note **{note['title']}** has no resources."

    lines = [f"Resources in '{note['title']}' ({len(refs)})\n"]
    for ref_id in refs:
        res = _acct().resource_index.get(ref_id)
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
    res = _acct().resource_index.get(resource_id)
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
    res = _acct().resource_index.get(resource_id)
    if res and res["size"] > MAX_RESOURCE_SIZE:
        return f"Resource too large ({res['size'] / 1024 / 1024:.1f} MB). Max 50 MB."

    try:
        data, mime, title = await _download_resource(resource_id)
        b64 = base64.b64encode(data).decode("ascii")
        return f"**{title}** ({mime}, {len(data)/1024:.0f} KB)\n\ndata:{mime};base64,{b64}"
    except httpx.HTTPStatusError as e:
        return f"Resource {resource_id} not found." if e.response.status_code == 404 else str(e)


# -- HTTP authentication (multi-user mode) --

_warned_header_ignored = False


def _plain_response(status: int, message: str, extra: Optional[list] = None):
    body = json.dumps({"error": message}).encode("utf-8")
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())] + (extra or [])
    return status, headers, body


class CredentialsMiddleware:
    """ASGI middleware: reads the credentials header and binds the account.

    Multi-user mode: a missing/invalid header or a failed login is answered with
    HTTP 401 (429 when rate limited) before the request reaches the MCP layer.
    Single-user mode: the header is ignored (a warning is logged once).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        raw = None
        want = CREDENTIALS_HEADER.lower().encode("latin-1")
        for name, value in scope.get("headers", []):
            if name == want:
                raw = value.decode("utf-8", "replace")
                break

        if not MULTI_USER:
            global _warned_header_ignored
            if raw is not None and not _warned_header_ignored:
                _warned_header_ignored = True
                logger.warning(f"{CREDENTIALS_HEADER} header ignored: not in multi-user mode "
                               "(set JOPLIN_MULTI_USER=1 to use it)")
            return await self.app(scope, receive, send)

        _ensure_sweeper()
        evict_idle_accounts()
        try:
            email, password = parse_credentials(raw)
            acct = await authenticate(email, password)
        except AuthError as e:
            status, headers, body = _plain_response(
                401, f"Unauthorized: {e}",
                [(b"www-authenticate", f'Bearer realm="joplin"'.encode())])
        except RateLimited as e:
            status, headers, body = _plain_response(429, str(e), [(b"retry-after", b"300")])
        except Exception as e:
            logger.warning(f"Login error: {type(e).__name__}")
            status, headers, body = _plain_response(502, "Could not log in to Joplin Server")
        else:
            token = _current_account.set(acct)
            try:
                return await self.app(scope, receive, send)
            finally:
                _current_account.reset(token)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _build_http_app(transport: str):
    app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
    app.add_middleware(CredentialsMiddleware)
    return app


# -- Entry point --

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        if MULTI_USER:
            raise SystemExit("JOPLIN_MULTI_USER requires an HTTP transport (sse or streamable-http)")
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(_build_http_app(transport), host=mcp.settings.host, port=mcp.settings.port,
                    log_level=mcp.settings.log_level.lower())
