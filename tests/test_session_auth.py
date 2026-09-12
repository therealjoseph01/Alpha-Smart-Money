"""PRD 49/54 - dashboard sessions.

The property being protected: the API token must never be readable by page JavaScript,
and access must expire on its own.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from asm.config import Settings, settings
from asm.services.api.main import app


@pytest.fixture
def token():
    return Settings().api_token


@pytest.fixture
async def client(redis):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_unauthenticated_is_refused(client):
    assert (await client.get("/auth/check")).status_code == 401
    assert (await client.post("/system/start")).status_code == 401


async def test_wrong_password_refused(client):
    assert (await client.post("/auth/login", json={"password": "nope"})).status_code == 401


async def test_login_issues_an_httponly_cookie(client, token):
    """httpOnly is the whole point - a cookie JavaScript can read is no safer than
    localStorage, because the same XSS steals either one."""
    r = await client.post("/auth/login", json={"password": token})
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie, "cookie must be unreadable from JavaScript"
    assert "samesite=lax" in cookie.lower(), "needs CSRF protection"
    assert "max-age" in cookie.lower(), "session must expire"


async def test_session_grants_access_then_logout_revokes_it(client, token):
    await client.post("/auth/login", json={"password": token})
    assert (await client.get("/auth/check")).status_code == 200
    assert (await client.post("/system/start")).status_code == 200

    await client.post("/auth/logout")
    assert (await client.get("/auth/check")).status_code == 401
    assert (await client.post("/system/start")).status_code == 401


async def test_bearer_token_still_works_for_scripts(client, token):
    """The CLI has a token, not a browser - it must keep working."""
    r = await client.post("/system/stop", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_forged_session_id_rejected(client):
    client.cookies.set("asm_session", "made-up-value")
    assert (await client.get("/auth/check")).status_code == 401


async def test_revoke_all_kills_existing_sessions(client, token):
    await client.post("/auth/login", json={"password": token})
    assert (await client.get("/auth/check")).status_code == 200

    await client.post("/auth/revoke-all", headers={"Authorization": f"Bearer {token}"})
    assert (await client.get("/auth/check")).status_code == 401


async def test_session_has_a_finite_lifetime(redis, token):

    assert settings.session_ttl_seconds > 0
    assert settings.session_ttl_seconds <= 86_400, "a session should not outlive a day"


async def test_placeholder_password_is_never_accepted(client, monkeypatch):
    """A default everyone knows is the same as no password."""
    from asm.services.api import deps

    monkeypatch.setattr(deps.settings, "api_token", "dev-token-change-me")
    r = await client.post("/auth/login", json={"password": "dev-token-change-me"})
    assert r.status_code == 401


def test_dashboard_never_stores_the_token():
    """Guards against a regression back to localStorage."""
    from pathlib import Path

    html = Path("src/asm/services/api/static/index.html").read_text()
    js = html[html.index("<script>"):]
    for forbidden in ("localStorage", "sessionStorage", "asm_token", "Bearer "):
        assert forbidden not in js, f"dashboard must not use {forbidden}"


# ------------------------------------------------- Secure flag, decided per request
async def test_cookie_is_not_secure_on_plain_localhost(client, token):
    """Marking it Secure on http would stop the browser sending it at all."""
    r = await client.post("/auth/login", json={"password": token})
    assert "Secure" not in r.headers.get("set-cookie", "")


async def test_cookie_is_secure_over_https(redis, token):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="https://example.com") as c:
        r = await c.post("/auth/login", json={"password": token})
        assert "Secure" in r.headers.get("set-cookie", "")


async def test_cookie_is_secure_behind_a_tls_proxy(redis, token):
    """The app sees plain http behind nginx/Caddy; X-Forwarded-Proto is the signal."""
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://example.com") as c:
        r = await c.post("/auth/login", json={"password": token},
                         headers={"x-forwarded-proto": "https"})
        assert "Secure" in r.headers.get("set-cookie", "")
