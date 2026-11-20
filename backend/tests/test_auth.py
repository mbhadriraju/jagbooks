import asyncio
from io import BytesIO
from uuid import uuid4

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth import get_db, provider_status
from app.database import Supabase, settings
from app.main import app
from scripts.check_auth import check

USER_ID = str(uuid4())


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://wskxghlkmkopatlxylxj.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-test")
    monkeypatch.delenv("SUPABASE_OAUTH_CALLBACK_URL", raising=False)
    monkeypatch.delenv("SUPABASE_TEST_ACCESS_TOKEN", raising=False)


def request(path, headers=None):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://backend.test") as client:
            return await client.get(path, headers=headers)
    return asyncio.run(run())


def test_google_user_verified_with_supabase_and_same_jwt_reaches_data_api(monkeypatch):
    seen = []
    def respond(self, method, path, **kwargs):
        seen.append((self.token, method, path))
        if path == "/auth/v1/user":
            return {"id": USER_ID, "email": "coach@example.test", "is_anonymous": False, "app_metadata": {"provider": "google"}}
        if path.startswith("/rest/v1/finance_groups"):
            return [{"id": str(uuid4()), "name": "BVW Debate Financial Consolidation"}]
        raise AssertionError(path)
    monkeypatch.setattr(Supabase, "request", respond)
    result = request("/auth/access", {"Authorization": "Bearer oauth-session-jwt"})
    assert result.status_code == 200 and result.json()["can_access_finances"] is True
    assert [s[1] for s in seen] == ["GET", "GET"]
    assert all(s[0] == "oauth-session-jwt" for s in seen)
    assert result.headers["cache-control"] == "private, no-store"
    me = request("/auth/me", {"Authorization": "Bearer oauth-session-jwt"})
    assert me.json() == {"id": USER_ID, "email": "coach@example.test"}
    assert "provider" not in me.text and "oauth-session-jwt" not in me.text


def test_oauth_sign_in_never_grants_group_membership_from_profile_metadata(monkeypatch):
    def respond(self, method, path, **kwargs):
        if path == "/auth/v1/user":
            return {"id": USER_ID, "user_metadata": {"is_admin": True, "role": "admin", "group_id": "fake"}}
        if path.startswith("/rest/v1/finance_groups"):
            return []
        raise AssertionError("Unexpected write or path")
    monkeypatch.setattr(Supabase, "request", respond)
    result = request("/auth/access", {"Authorization": "Bearer valid-but-uninvited"})
    assert result.status_code == 200 and result.json()["can_access_finances"] is False and result.json()["groups"] == []


@pytest.mark.parametrize("user", [{}, {"id": "not-a-uuid"}, None, {"id": USER_ID, "is_anonymous": True}])
def test_invalid_or_anonymous_auth_user_denied(monkeypatch, user):
    monkeypatch.setattr(Supabase, "request", lambda *args, **kwargs: user)
    with pytest.raises(HTTPException) as error:
        get_db(HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"))
    assert error.value.status_code in (401, 403)


def test_google_provider_status_reads_live_settings_and_claims_no_login_test(monkeypatch):
    seen = []
    def respond(self, method, path, **kwargs):
        seen.append((self.token, path))
        return {"external": {"google": True, "github": False, "email": True, "phone": True, "anonymous_users": True}}
    monkeypatch.setattr(Supabase, "request", respond)
    result = request("/auth/providers")
    assert result.status_code == 200
    assert result.json()["enabled_oauth_providers"] == ["google"]
    assert result.json()["provider_callback_url"] == "https://wskxghlkmkopatlxylxj.supabase.co/auth/v1/callback"
    assert result.json()["end_to_end_login_verified"] is False
    assert seen == [(None, "/auth/v1/settings")]
    assert "publishable-test" not in result.text
    assert result.headers["cache-control"] == "no-store"


def test_disabled_google_not_reported_as_working(monkeypatch):
    monkeypatch.setattr(Supabase, "request", lambda *args, **kwargs: {"external": {"google": False}})
    assert provider_status()["google_enabled"] is False
    result = check()
    assert result["user_session_test"] == "not_run" and result["google_enabled"] is False


@pytest.mark.parametrize("callback", [
    "https://gfmublljsjwnsnymlvqt.supabase.co/auth/v1/callback",
    "https://wskxghlkmkopatlxylxj.supabase.co/auth/v1/oauth/callback",
])
def test_wrong_project_or_oauth_server_callback_path_rejected(monkeypatch, callback):
    monkeypatch.setenv("SUPABASE_OAUTH_CALLBACK_URL", callback)
    with pytest.raises(RuntimeError, match="/auth/v1/callback"):
        settings()


def test_correct_callback_accepted_and_missing_config_is_503(monkeypatch):
    monkeypatch.setenv("SUPABASE_OAUTH_CALLBACK_URL", "https://wskxghlkmkopatlxylxj.supabase.co/auth/v1/callback")
    settings()
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY")
    assert request("/auth/providers").status_code == 503
    assert request("/auth/me", {"Authorization": "Bearer token"}).status_code == 503


def test_auth_settings_transport_sends_publishable_key_without_fake_bearer(monkeypatch):
    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
    seen = []
    def respond(req, **kwargs):
        seen.append(req)
        return Response(b'{"external":{"google":true}}')
    monkeypatch.setattr("app.database.urlopen", respond)
    assert provider_status()["google_enabled"] is True
    assert seen[0].get_header("Authorization") is None
    assert seen[0].get_header("Apikey") == "publishable-test"


def test_invalid_expired_or_other_project_token_denied_by_auth_server(monkeypatch):
    def deny(*args, **kwargs):
        raise HTTPException(403, "Upstream private data")
    monkeypatch.setattr(Supabase, "request", deny)
    result = request("/auth/me", {"Authorization": "Bearer wrong-project"})
    assert result.status_code == 401 and result.headers["www-authenticate"] == "Bearer"
    assert "Upstream" not in result.text


def test_invalid_project_key_reports_configuration_failure(monkeypatch):
    def reject(*args, **kwargs):
        raise HTTPException(401, "private upstream response")
    monkeypatch.setattr(Supabase, "request", reject)
    result = request("/auth/providers")
    assert result.status_code == 503 and "publishable" in result.json()["detail"]
    assert "private upstream" not in result.text


@pytest.mark.parametrize(("method", "providers", "status"), [
    ("oauth", ["google"], 200),
    ("password", ["google", "email"], 403),
    ("oauth", ["github"], 403),
])
def test_workspace_requires_verified_google_linked_oauth_session(monkeypatch, method, providers, status):
    import base64
    import json

    def respond(self, _method, path, **kwargs):
        if path == "/auth/v1/user":
            return {"id": USER_ID, "app_metadata": {"providers": providers}}
        if path.startswith("/rest/v1/finance_groups"):
            return []
        raise AssertionError(path)

    monkeypatch.setattr(Supabase, "request", respond)
    payload = base64.urlsafe_b64encode(json.dumps({"amr": [{"method": method}]}).encode()).decode().rstrip("=")
    result = request("/finance/access", {"Authorization": f"Bearer header.{payload}.signature"})
    assert result.status_code == status


def test_browser_config_exposes_only_the_publishable_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    result = request("/auth/browser-config")
    assert result.status_code == 200
    assert result.json() == {
        "url": "https://wskxghlkmkopatlxylxj.supabase.co",
        "key": "sb_publishable_test",
    }
    assert result.headers["cache-control"] == "no-store"

    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_secret_test")
    rejected = request("/auth/browser-config")
    assert rejected.status_code == 503
    assert "sb_secret_test" not in rejected.text
