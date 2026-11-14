"""A tiny in-memory fake of the Joplin Server API, for tests and smoke runs.

Implements just what the index needs: POST /api/sessions, the root children
listing and item content. Each user owns one notebook.
"""

import json

import httpx

PASSWORDS = {"alice@example.com": "pw:with:colons", "bob@example.com": "bob-secret"}


def _item_id(email: str) -> str:
    return (email.split("@")[0] * 32).encode().hex()[:32]


def _folder(email: str, title: str) -> str:
    fid = _item_id(email)
    return (f"{title}\n\nid: {fid}\nparent_id: \ncreated_time: 2024-01-01T00:00:00.000Z\n"
            "updated_time: 2024-01-01T00:00:00.000Z\nuser_created_time: 2024-01-01T00:00:00.000Z\n"
            "user_updated_time: 2024-01-01T00:00:00.000Z\nencryption_cipher_text: \n"
            "encryption_applied: 0\nis_shared: 0\nshare_id: \nmaster_key_id: \nicon: \n"
            "deleted_time: 0\ntype_: 2")


class FakeJoplin:
    def __init__(self, passwords=None):
        self.passwords = dict(passwords or PASSWORDS)
        self.sessions = {}
        self.login_attempts = 0
        self.seen_bodies = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sessions" and request.method == "POST":
            self.login_attempts += 1
            body = json.loads(request.content)
            self.seen_bodies.append(body)
            email = body["email"].lower()
            if self.passwords.get(email) != body["password"]:
                return httpx.Response(403, json={"error": "Invalid email or password"})
            sid = f"sess-{email}-{self.login_attempts}"
            self.sessions[sid] = email
            return httpx.Response(200, json={"id": sid})
        email = self.sessions.get(request.headers.get("X-API-AUTH", ""))
        if email is None:
            return httpx.Response(403, json={"error": "Invalid session"})
        name = f"{_item_id(email)}.md"
        if path == "/api/items/root:/:/children":
            return httpx.Response(200, json={
                "items": [{"name": name, "updated_time": 1}], "has_more": False})
        if path == f"/api/items/root:/{name}:/content":
            return httpx.Response(200, text=_folder(email, f"Notebook of {email.split('@')[0]}"))
        return httpx.Response(404, json={"error": "not found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
