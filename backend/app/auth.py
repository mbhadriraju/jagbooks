"""Supabase OAuth session verification; finance permissions stay in Postgres RLS."""
import base64
import json
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.database import Supabase, oauth_callback_url

bearer = HTTPBearer(auto_error=False)


def auth_client(token=None):
    try:
        return Supabase(token)
    except RuntimeError as e:
        raise HTTPException(503, f"Supabase authentication configuration is missing or invalid, {e}") from None
        


def provider_status():
    """Read live public Auth settings; never infer provider enablement from env alone."""
    db = auth_client()
    try:
        result = db.request("GET", "/auth/v1/settings")
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(503, "Supabase rejected the project's publishable API key.") from None
        raise
    if not isinstance(result, dict) or not isinstance(result.get("external"), dict):
        raise HTTPException(502, "Supabase Auth settings response is invalid.")
    providers = sorted(name for name, enabled in result["external"].items()
                       if enabled is True and name not in {"email", "phone", "anonymous_users"})
    return {
        "supabase_url": db.url, "provider_callback_url": oauth_callback_url(db.url),
        "enabled_oauth_providers": providers, "google_enabled": "google" in providers,
        "provider_settings_checked": True, "end_to_end_login_verified": False,
    }


def get_db(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != "bearer" or len(credentials.credentials) > 8192:
        raise HTTPException(401, "Supabase access token required.", headers={"WWW-Authenticate": "Bearer"})
    db = auth_client(credentials.credentials)
    try:
        user = db.request("GET", "/auth/v1/user")
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise HTTPException(401, "Invalid or expired Supabase access token.", headers={"WWW-Authenticate": "Bearer"}) from None
        raise
    try:
        UUID(user["id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, "Invalid Supabase access token.") from None
    if user.get("is_anonymous") is True:
        raise HTTPException(403, "Sign in with a registered Supabase account.")
    # Never trust client-supplied profile/provider/role metadata for authorization.
    db.user = user
    return db


def get_google_db(
    db: Supabase = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    # Auth has already verified this exact bearer. Read its session method only.
    try:
        part = credentials.credentials.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        oauth = any(entry.get("method") == "oauth" for entry in claims.get("amr", []))
    except (AttributeError, IndexError, TypeError, ValueError):
        oauth = False
    providers = db.user.get("app_metadata", {}).get("providers", [])
    if not oauth or "google" not in providers:
        raise HTTPException(403, "Continue with Google to use the finance workspace.")
    return db
