"""Supabase REST/Storage transport. Each request retains its user's JWT and RLS."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")
BUCKET = "bvw-finance-files"
MAX_FILE_BYTES = 10 * 1024 * 1024


def settings():
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or not key
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise RuntimeError("Configure SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY.")
    callback = os.getenv("SUPABASE_OAUTH_CALLBACK_URL")
    if callback and callback != oauth_callback_url(url):
        raise RuntimeError("SUPABASE_OAUTH_CALLBACK_URL must match this project's /auth/v1/callback.")
    return url, key


def oauth_callback_url(url):
    return url.rstrip("/") + "/auth/v1/callback"


class Supabase:
    def __init__(self, token: str | None = None):
        self.url, self.key = settings()
        self.token = token

    def request(self, method, path, *, data=None, raw=None, content_type="application/json"):
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        headers = {"apikey": self.key, "Content-Type": content_type, "Prefer": "return=representation"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=45) as response:
                payload = response.read()
                return json.loads(payload) if "application/json" in response.headers.get("Content-Type", "") else payload
        except HTTPError as exc:
            # Never return upstream text: it can contain credentials or financial data.
            status = exc.code
            if status in (401, 403, 404, 409, 413):
                raise HTTPException(status, "Supabase request denied or record unavailable.") from None
            raise HTTPException(502, "Supabase request failed.") from None
        except (URLError, TimeoutError):
            raise HTTPException(503, "Supabase temporarily unavailable.") from None

    def rows(self, table, **filters):
        return self.request("GET", "/rest/v1/" + table + "?" + urlencode(filters))

    def insert(self, table, data):
        return self.request("POST", "/rest/v1/" + table, data=data)

    def update(self, table, record_id, data):
        return self.request("PATCH", "/rest/v1/" + table + "?id=eq." + str(record_id), data=data)

    def rpc(self, name, data):
        return self.request("POST", "/rest/v1/rpc/" + name, data=data)

    def upload(self, path, content, mime, bucket=BUCKET):
        return self.request("POST", "/storage/v1/object/" + bucket + "/" + quote(path, safe="/"), raw=content, content_type=mime)

    def download(self, path, bucket=BUCKET):
        return self.request("GET", "/storage/v1/object/authenticated/" + bucket + "/" + quote(path, safe="/"))

    def signed_url(self, path, bucket=BUCKET):
        result = self.request("POST", "/storage/v1/object/sign/" + bucket + "/" + quote(path, safe="/"), data={"expiresIn": 300})
        return self.url + "/storage/v1" + result["signedURL"]
