"""End-to-end smoke test of multi-user streamable-http mode (no real Joplin).

Starts a fake Joplin API and app/server.py, then checks: 401 without the
header, 401 on a bad login, and that two users see only their own notebook.
Run: python tests/smoke_multi_user.py [port]   (exit code 0 = pass)
"""

import asyncio
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fake_joplin import FakeJoplin  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8381
FAKE_PORT = PORT + 1
fake = FakeJoplin()


class Handler(http.server.BaseHTTPRequestHandler):
    def _go(self):
        length = int(self.headers.get("content-length") or 0)
        req = httpx.Request(self.command, f"http://x{self.path}", headers=dict(self.headers),
                            content=self.rfile.read(length))
        resp = fake.handler(req)
        self.send_response(resp.status_code)
        self.send_header("content-type", "application/json" if resp.content[:1] == b"{" else "text/plain")
        self.send_header("content-length", str(len(resp.content)))
        self.end_headers()
        self.wfile.write(resp.content)

    do_GET = do_POST = do_PUT = _go

    def log_message(self, *a):
        pass


async def tool_text(creds):
    async with streamablehttp_client(f"http://127.0.0.1:{PORT}/mcp",
                                     headers={"X-Joplin-Credentials": creds}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_notebooks", {})
            return res.content[0].text


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cache = tempfile.mkdtemp(prefix="joplin-smoke-")
    env = {**os.environ, "JOPLIN_SERVER_URL": f"http://127.0.0.1:{FAKE_PORT}",
           "JOPLIN_MULTI_USER": "1", "MCP_TRANSPORT": "streamable-http", "MCP_HOST": "127.0.0.1",
           "MCP_PORT": str(PORT), "JOPLIN_CACHE_DIR": cache}
    for k in ("JOPLIN_EMAIL", "JOPLIN_PASSWORD"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "app", "server.py")], env=env)
    failures = []

    def check(cond, label):
        print(("ok    " if cond else "FAIL  ") + label)
        if not cond:
            failures.append(label)

    try:
        for _ in range(50):
            try:
                httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        url = f"http://127.0.0.1:{PORT}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        hdrs = {"accept": "application/json, text/event-stream"}
        r = httpx.post(url, json=body, headers=hdrs)
        check(r.status_code == 401, f"no header -> 401 (got {r.status_code})")
        r = httpx.post(url, json=body, headers={**hdrs, "X-Joplin-Credentials": "alice@example.com:bad"})
        check(r.status_code == 401 and "bad" not in r.text,
              f"bad login -> 401 (got {r.status_code})")
        a = asyncio.run(tool_text("alice@example.com:pw:with:colons"))
        b = asyncio.run(tool_text("bob@example.com:bob-secret"))
        check("Notebook of alice" in a and "bob" not in a, "alice sees only her notebook")
        check("Notebook of bob" in b and "alice" not in b, "bob sees only his notebook")
        modes = {oct(os.stat(os.path.join(cache, f)).st_mode & 0o777) for f in os.listdir(cache)}
        check(len(os.listdir(cache)) == 2 and modes == {"0o600"}, "one 0600 cache file per account")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("all checks passed" if not failures else f"{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
