"""Tests for multi-user mode (per-request credentials). Run with `pytest tests/`.

The Joplin API is replaced by tests/fake_joplin.py through an httpx MockTransport.
"""

import asyncio
import os
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)
os.environ.setdefault("JOPLIN_SERVER_URL", "http://joplin.test")
os.environ.setdefault("JOPLIN_EMAIL", "alice@example.com")
os.environ.setdefault("JOPLIN_PASSWORD", "pw:with:colons")

import server as S  # noqa: E402
from fake_joplin import FakeJoplin  # noqa: E402

ALICE = "alice@example.com:pw:with:colons"
BOB = "bob@example.com:bob-secret"


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeJoplin()
    monkeypatch.setattr(S, "_client", fake.client())
    monkeypatch.setattr(S, "MULTI_USER", True)
    monkeypatch.setattr(S, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(S, "DISK_CACHE", True)
    monkeypatch.setattr(S, "_accounts", {})
    monkeypatch.setattr(S, "_login_failures", {})
    monkeypatch.setattr(S, "_default_account", None)
    monkeypatch.setattr(S, "_warned_header_ignored", False)
    return fake


async def call(headers, inner=None):
    """Drive the middleware with a fake HTTP request; return (status, body, inner_result)."""
    sent, result = [], {}

    async def app(scope, receive, send):
        result["acct"] = S._current_account.get()
        if inner:
            result["out"] = await inner()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    await S.CredentialsMiddleware(app)(scope, None, send)
    return sent[0]["status"], sent[1]["body"], result


def run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            S._sweeper and S._sweeper.cancel()
            for a in list(S._accounts.values()):
                a.close()
    return asyncio.run(wrapper())


# -- header parsing ---------------------------------------------------------

def test_parse_splits_at_first_colon():
    assert S.parse_credentials("a@b.c:pa:ss:word") == ("a@b.c", "pa:ss:word")


@pytest.mark.parametrize("value", [None, "", "   ", "no-colon", ":pw", "a@b.c:", "a@b.c: "[:6]])
def test_parse_rejects_bad_values(value):
    with pytest.raises(S.AuthError):
        S.parse_credentials(value)


# -- HTTP behaviour -----------------------------------------------------------

def test_missing_header_is_401(env):
    status, body, _ = run(call({}))
    assert status == 401 and b"X-Joplin-Credentials" in body
    assert env.login_attempts == 0


def test_malformed_header_is_401(env):
    status, _, _ = run(call({"X-Joplin-Credentials": "nocolon"}))
    assert status == 401


def test_failed_login_is_401_and_not_leaked(env):
    status, body, _ = run(call({"X-Joplin-Credentials": "alice@example.com:wrong-pass-123"}))
    assert status == 401
    assert b"wrong-pass-123" not in body
    assert not S._accounts


def test_good_login_binds_account(env):
    status, _, res = run(call({"X-Joplin-Credentials": ALICE}))
    assert status == 200 and res["acct"].email == "alice@example.com"


def test_custom_header_name(env, monkeypatch):
    monkeypatch.setattr(S, "CREDENTIALS_HEADER", "X-Mine")
    assert run(call({"X-Joplin-Credentials": ALICE}))[0] == 401
    assert run(call({"X-Mine": ALICE}))[0] == 200


def test_session_reused_between_requests(env):
    async def two():
        await call({"X-Joplin-Credentials": ALICE})
        await call({"X-Joplin-Credentials": ALICE})
    run(two())
    assert env.login_attempts == 1


def test_rate_limit_after_five_failures(env):
    bad = {"X-Joplin-Credentials": "alice@example.com:nope-nope"}

    async def go():
        return [(await call(bad))[0] for _ in range(7)]
    statuses = run(go())
    assert statuses == [401] * 5 + [429] * 2
    assert env.login_attempts == 5
    # a different account is not affected
    assert run(call({"X-Joplin-Credentials": BOB}))[0] == 200


def test_rate_limit_window_expires(env, monkeypatch):
    key = S._account_key("alice@example.com")
    S._login_failures[key] = S.collections.deque([S.time.time() - 301] * 9)
    assert run(call({"X-Joplin-Credentials": ALICE}))[0] == 200


# -- isolation ------------------------------------------------------------------

def test_two_accounts_never_see_each_others_index(env):
    async def go():
        out = {}
        for name, hdr in (("alice", ALICE), ("bob", BOB)):
            _, _, res = await call({"X-Joplin-Credentials": hdr}, inner=S.list_notebooks)
            out[name] = res["out"]
        # interleaved concurrent requests
        a, b = await asyncio.gather(
            call({"X-Joplin-Credentials": ALICE}, inner=S.list_notebooks),
            call({"X-Joplin-Credentials": BOB}, inner=S.list_notebooks))
        return out, a[2]["out"], b[2]["out"]
    out, a2, b2 = run(go())
    assert "Notebook of alice" in out["alice"] and "bob" not in out["alice"]
    assert "Notebook of bob" in out["bob"] and "alice" not in out["bob"]
    assert "Notebook of alice" in a2 and "Notebook of bob" not in a2
    assert "Notebook of bob" in b2 and "Notebook of alice" not in b2
    ka, kb = (S._account_key(e) for e in ("alice@example.com", "bob@example.com"))
    assert S._accounts[ka].index.keys().isdisjoint(S._accounts[kb].index.keys())


def test_account_key_ignores_email_case_and_depends_on_server(monkeypatch):
    assert S._account_key("Alice@Example.com") == S._account_key("alice@example.com")
    k = S._account_key("alice@example.com")
    monkeypatch.setattr(S, "JOPLIN_SERVER_URL", "http://other.test")
    assert S._account_key("alice@example.com") != k


def test_password_change_needs_new_password(env):
    async def go():
        await call({"X-Joplin-Credentials": ALICE})
        env.passwords["alice@example.com"] = "new"
        env.sessions.clear()  # Joplin drops the sessions when the password changes
        # the cached session is dead: re-login with the old password fails ...
        with pytest.raises(S.AuthError):
            await call({"X-Joplin-Credentials": ALICE}, inner=S.list_notebooks)
        # ... and a request carrying the new password logs in again
        return (await call({"X-Joplin-Credentials": "alice@example.com:new"}))[0]
    assert run(go()) == 200


def test_relogin_on_403_uses_stored_password(env):
    async def go():
        await call({"X-Joplin-Credentials": ALICE})
        env.sessions.clear()  # server drops the session

        async def inner():
            return (await S._api("GET", "/api/items/root:/:/children")).status_code
        _, _, res = await call({"X-Joplin-Credentials": ALICE}, inner=inner)
        return res["out"]
    assert run(go()) == 200


# -- eviction --------------------------------------------------------------------

def test_eviction_cancels_background_tasks(env):
    async def go():
        _, _, res = await call({"X-Joplin-Credentials": ALICE})
        acct = res["acct"]
        task = acct.spawn(asyncio.sleep(3600))
        fresh = (await call({"X-Joplin-Credentials": BOB}))[2]["acct"]
        acct.last_used -= S.ACCOUNT_IDLE_SECONDS + 1
        assert S.evict_idle_accounts() == 1
        await asyncio.sleep(0)
        return acct, task, fresh
    acct, task, fresh = run(go())
    assert task.cancelled() and acct.password == "" and acct.session_id is None
    assert S._account_key("bob@example.com") in S._accounts or fresh


def test_eviction_default_is_30_minutes():
    assert S.ACCOUNT_IDLE_SECONDS == 30 * 60


# -- disk cache --------------------------------------------------------------------

def test_cache_file_per_account_mode_0600(env, tmp_path):
    async def go():
        await call({"X-Joplin-Credentials": ALICE}, inner=S.list_notebooks)
        await call({"X-Joplin-Credentials": BOB}, inner=S.list_notebooks)
    run(go())
    cache = tmp_path / "cache"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    names = sorted(p.name for p in cache.iterdir())
    assert names == sorted(S._account_key(e) for e in ("alice@example.com", "bob@example.com"))
    for p in cache.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert "pw:with:colons" not in p.read_text() and "bob-secret" not in p.read_text()


def test_cache_loaded_after_login_only(env):
    async def go():
        await call({"X-Joplin-Credentials": ALICE}, inner=S.list_notebooks)
        S._accounts.pop(S._account_key("alice@example.com")).close()
        bad = await call({"X-Joplin-Credentials": "alice@example.com:wrong"})
        good = await call({"X-Joplin-Credentials": ALICE})
        return bad[0], good[2]["acct"].index
    status, idx = run(go())
    assert status == 401 and idx  # cached index restored after a valid login


def test_disk_cache_can_be_disabled(env, monkeypatch, tmp_path):
    monkeypatch.setattr(S, "DISK_CACHE", False)
    run(call({"X-Joplin-Credentials": ALICE}, inner=S.list_notebooks))
    assert not (tmp_path / "cache").exists()


# -- no credentials in errors --------------------------------------------------------

def test_login_network_error_is_scrubbed(monkeypatch):
    import httpx

    class Boom(httpx.AsyncClient):
        async def post(self, *a, **k):
            raise httpx.ConnectError(f"boom {k['json']['password']}")
    monkeypatch.setattr(S, "_client", Boom())
    acct = S.Account("alice@example.com", "s3cret")
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(S._login(acct))
    assert "s3cret" not in str(ei.value) and "s3cret" not in repr(ei.value.__cause__)


# -- single-user mode ------------------------------------------------------------------

def test_single_user_unchanged(monkeypatch):
    fake = FakeJoplin()
    monkeypatch.setattr(S, "_client", fake.client())
    monkeypatch.setattr(S, "JOPLIN_EMAIL", "alice@example.com")
    monkeypatch.setattr(S, "JOPLIN_PASSWORD", "pw:with:colons")
    monkeypatch.setattr(S, "MULTI_USER", False)
    monkeypatch.setattr(S, "DISK_CACHE", False)
    monkeypatch.setattr(S, "_default_account", None)
    monkeypatch.setattr(S, "_warned_header_ignored", False)
    # no header needed; env credentials are used
    out = asyncio.run(S.list_notebooks())
    assert "Notebook of alice" in out
    assert S._acct().cache_path() is None


def test_single_user_ignores_header(monkeypatch, caplog):
    fake = FakeJoplin()
    monkeypatch.setattr(S, "_client", fake.client())
    monkeypatch.setattr(S, "JOPLIN_EMAIL", "alice@example.com")
    monkeypatch.setattr(S, "JOPLIN_PASSWORD", "pw:with:colons")
    monkeypatch.setattr(S, "MULTI_USER", False)
    monkeypatch.setattr(S, "DISK_CACHE", False)
    monkeypatch.setattr(S, "_default_account", None)
    monkeypatch.setattr(S, "_warned_header_ignored", False)

    async def go():
        r1 = await call({"X-Joplin-Credentials": BOB}, inner=S.list_notebooks)
        r2 = await call({"X-Joplin-Credentials": BOB})
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert r1[0] == 200 and "Notebook of alice" in r1[2]["out"]  # env account, not bob
    assert sum("ignored" in r.message for r in caplog.records) == 1  # warned once
    assert all("bob-secret" not in r.message for r in caplog.records)


def test_single_user_cache_path_is_legacy(monkeypatch):
    monkeypatch.setattr(S, "MULTI_USER", False)
    monkeypatch.setattr(S, "DISK_CACHE", True)
    assert S.Account("a@b.c", "x").cache_path() == S.INDEX_CACHE_FILE
