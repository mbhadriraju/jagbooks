"""Read-only OAuth readiness probe. Never prints API keys, tokens, or user profiles."""
import json
import os

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth import get_db, provider_status


def check():
    result = provider_status()
    result["user_session_test"] = "not_run"
    token = os.getenv("SUPABASE_TEST_ACCESS_TOKEN")
    if token:
        db = get_db(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        groups = db.rows("finance_groups", order="name")
        result.update(user_session_test="passed", finance_group_count=len(groups), can_access_finances=bool(groups))
    return result


def main():
    try:
        result = check()
        print(json.dumps(result, indent=2))
        if not result["google_enabled"]:
            raise SystemExit("Google is not enabled in this project's live Supabase Auth settings.")
    except HTTPException as exc:
        print(json.dumps({"auth_check": "failed", "status": exc.status_code, "detail": exc.detail}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
