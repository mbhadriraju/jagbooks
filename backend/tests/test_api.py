import asyncio
import json
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile
from xml.etree import ElementTree

import httpx
import httpx2
import pytest
from fastapi import HTTPException
from groq import Groq
from pydantic import ValidationError

from app.auth import get_db
from app.database import MAX_FILE_BYTES
from app.main import app, store_upload
from app.schemas import TransactionInput
from app.spreadsheet import workbook

SHEET = {"id": str(uuid4()), "group_id": str(uuid4()), "name": "Debate"}
FILE = str(uuid4())


class FakeDB:
    user = {"id": str(uuid4()), "email": "coach@example.test"}

    def __init__(self):
        self.tables = {"finance_sheets": [dict(SHEET)], "finance_files": [], "finance_transactions": [], "finance_sheet_versions": [], "finance_extractions": [], "finance_workbook_objects": [], "finance_workbook_revisions": []}
        self.objects = {}
        self.calls = []
        self.fail_upload = False

    def rows(self, table, **filters):
        rows = list(self.tables[table])
        for key, val in filters.items():
            if isinstance(val, str) and val.startswith("eq."):
                rows = [r for r in rows if str(r.get(key)) == val[3:]]
        return rows[int(filters.get("offset", 0)):int(filters.get("offset", 0))+int(filters.get("limit", 1000))]

    def insert(self, table, data):
        row = {"id": str(uuid4()), **data}
        if table == "finance_files":
            row.setdefault("status", "uploading")
        self.tables[table].append(row)
        return [dict(row)]

    def update(self, table, record_id, data):
        for row in self.tables[table]:
            if row["id"] == str(record_id):
                row.update(data)
                return [dict(row)]
        return []

    def upload(self, path, content, mime, bucket=None):
        if self.fail_upload:
            raise HTTPException(503, "Storage unavailable")
        assert path not in self.objects
        self.objects[path] = content

    def download(self, path, bucket=None):
        if path not in self.objects:
            raise HTTPException(404, "Missing")
        return self.objects[path]

    def signed_url(self, path):
        return "https://example.supabase.co/signed?token=test"

    def rpc(self, name, data):
        self.calls.append((name, data))
        if name == "finance_finish_extraction":
            source = self.rows("finance_files", id="eq." + data["p_file"])[0]
            if source["status"] != "processed":
                self.insert("finance_extractions", {"file_id": source["id"], "rows": data["p_rows"]})
                self.update("finance_files", source["id"], {"status": "processed"})
            return None
        if name == "finance_commit_workbook":
            sheet = self.rows("finance_sheets", id="eq." + data["p_sheet"])[0]
            head = next((r for r in self.tables["finance_workbook_revisions"] if r["id"] == sheet.get("current_workbook_id")), None)
            applied = list(head["applied_file_ids"]) if head else []
            if data["p_operation"] == "source" and data["p_source"] in applied:
                return {"revision": head, "already_applied": True}
            if sheet.get("current_workbook_id") != data["p_expected"]:
                return {"conflict": True, "current_id": sheet.get("current_workbook_id")}
            obj = data["p_object"]
            totals = data["p_summary"]
            if data["p_operation"] == "restore":
                target = self.rows("finance_workbook_revisions", id="eq." + data["p_restored"])[0]
                obj, totals, applied = target["object_id"], target["summary"], list(target["applied_file_ids"])
            if data["p_operation"] == "checkpoint":
                obj, totals = head["object_id"], head["summary"]
            if data["p_operation"] == "source":
                applied.append(data["p_source"])
            revision = self.insert("finance_workbook_revisions", {
                "sheet_id": sheet["id"], "object_id": obj, "version": len(self.tables["finance_workbook_revisions"]) + 1,
                "operation": data["p_operation"], "parent_id": sheet.get("current_workbook_id"),
                "restored_from_id": data["p_restored"], "applied_file_ids": applied, "summary": totals,
            })[0]
            self.update("finance_sheets", sheet["id"], {"current_workbook_id": revision["id"]})
            return {"revision": revision}


def request(method, path, **kwargs):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://backend.test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.fixture
def db():
    db = FakeDB()
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.clear()


def test_anonymous_cannot_access_financial_routes():
    assert request("GET", "/sheets").status_code == 401
    assert request("POST", f"/sheets/{SHEET['id']}/files?filename=x.pdf", content=b"%PDF-1.7", headers={"content-type": "application/pdf"}).status_code == 401


def test_upload_download_and_duplicate_retry(db, monkeypatch):
    monkeypatch.setattr("app.rolling.extract", lambda *args: [valid_row()])
    path = f"/sheets/{SHEET['id']}/files?filename=receipt.pdf"
    content = b"%PDF-1.7 example"
    first = request("POST", path, content=content, headers={"content-type": "application/pdf"})
    assert first.status_code == 201
    f = first.json()["file"]
    assert f["status"] == "processed" and db.objects[f["object_path"]] == content
    second = request("POST", path, content=content, headers={"content-type": "application/pdf"})
    assert second.json()["file"]["id"] == f["id"] and len(db.tables["finance_files"]) == 1
    signed = request("GET", f"/files/{f['id']}/download")
    assert signed.json()["expires_in"] == 300


def test_upload_rejects_type_spoofing_and_oversize(db):
    path = f"/sheets/{SHEET['id']}/files?filename=x.pdf"
    assert request("POST", path, content=b"not a pdf", headers={"content-type": "application/pdf"}).status_code == 415
    assert request("POST", path, content=b"x" * (MAX_FILE_BYTES + 1)).status_code == 413
    assert not db.tables["finance_files"]


def test_storage_failure_retry_and_metadata_reconciliation(db):
    db.fail_upload = True
    with pytest.raises(HTTPException):
        store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-x")
    f = db.tables["finance_files"][0]
    assert f["status"] == "uploading"
    db.fail_upload = False
    recovered = store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-x")
    assert recovered["status"] == "uploaded" and len(db.tables["finance_files"]) == 1
    db.tables["finance_files"][0]["status"] = "uploading"
    assert store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-x")["status"] == "uploaded"


def test_missing_or_unauthorized_file_returns_404(db):
    assert request("GET", f"/files/{FILE}/download").status_code == 404
    assert request("GET", f"/sheets/{uuid4()}/files").status_code == 404


def test_extraction_failure_never_posts_rows_and_preserves_source(db, monkeypatch):
    f = store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-x")
    def fail(*args):
        raise HTTPException(502, "Extraction failed")
    monkeypatch.setattr("app.rolling.extract", fail)
    assert request("POST", f"/files/{f['id']}/process").status_code == 502
    assert not db.calls and db.objects and db.tables["finance_files"][0]["status"] == "uploaded"


def test_extraction_commits_through_atomic_rpc(db, monkeypatch):
    f = store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-x")
    monkeypatch.setattr("app.rolling.extract", lambda *args: [valid_row()])
    assert request("POST", f"/files/{f['id']}/process").status_code == 200
    assert db.calls[0] == ("finance_finish_extraction", {"p_file": f["id"], "p_rows": [valid_row()]})
    assert db.tables["finance_sheets"][0]["current_workbook_id"]


def test_approval_requires_complete_valid_values():
    with pytest.raises(ValidationError):
        TransactionInput(review_status="approved")
    with pytest.raises(ValidationError):
        TransactionInput(amount_cents=12.5)
    with pytest.raises(ValidationError):
        TransactionInput(financial_class="revenue", category="travel_expenses")
    assert TransactionInput(amount_cents=0).amount_cents == 0


def test_xlsx_escapes_formulas_and_preserves_exact_money():
    rows = [{"date": "2026-11-16", "description": "=HYPERLINK(\"unsafe\") & <note>", "amount_cents": 12345, "financial_class": "revenue", "category": "student_dues"}]
    with ZipFile(BytesIO(workbook(rows))) as archive:
        for name in archive.namelist():
            ElementTree.fromstring(archive.read(name))
        ledger = archive.read("xl/worksheets/sheet1.xml").decode()
        assert "<v>123.45</v>" in ledger and '<f>' not in ledger and '&lt;note&gt;' in ledger


def test_summary_reads_current_workbook_snapshot(db):
    from app.rolling import initialize
    head = initialize(db, SHEET["id"])
    response = request("GET", f"/sheets/{SHEET['id']}/summary")
    assert response.status_code == 200
    assert response.json()["version_id"] == head["id"]
    assert response.json()["basis"] == "current_workbook_inputs"


def test_review_rejects_incomplete_approval(db):
    response = request("PUT", f"/transactions/{uuid4()}", json={"review_status": "approved"})
    assert response.status_code == 422


def test_supabase_transport_preserves_jwt_and_redacts_upstream_errors(monkeypatch):
    from urllib.error import HTTPError
    from app.database import Supabase
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-test")
    seen = []
    def fail(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 403, "private financial data", {}, BytesIO(b'private data'))
    monkeypatch.setattr("app.database.urlopen", fail)
    db = Supabase("user-jwt")
    with pytest.raises(HTTPException) as error:
        db.rows("finance_files", sheet_id="eq." + SHEET["id"])
    assert error.value.status_code == 403 and "private" not in error.value.detail
    assert seen[0].get_header("Authorization") == "Bearer user-jwt"
    assert seen[0].get_header("Apikey") == "publishable-test"


def test_expired_auth_token_rejected(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials
    def reject(*args, **kwargs):
        raise HTTPException(401, "Expired")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-test")
    monkeypatch.setattr("app.auth.Supabase.request", reject)
    with pytest.raises(HTTPException) as error:
        get_db(HTTPAuthorizationCredentials(scheme="Bearer", credentials="expired"))
    assert error.value.status_code == 401


@pytest.fixture
def groq_transport(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    def install(respond):
        def client(**kwargs):
            return Groq(http_client=httpx.Client(transport=httpx.MockTransport(respond)), **kwargs)

        monkeypatch.setattr("app.extraction.Groq", client)

    return install


def test_model_output_always_becomes_draft(monkeypatch, groq_transport, capsys):
    from app.extraction import extract
    monkeypatch.setenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
    rows = [
        {"transaction_id": "PO-123", "date": "2026-11-16", "description": "Outstanding tournament purchase order",
         "amount": 12.50, "financial_class": "purchase_order", "category": "outstanding_purchase_orders"},
        {"transaction_id": None, "date": None, "description": "Explicit reversal of an opening balance",
         "amount": -0.29, "financial_class": "cash_flow", "category": "opening_balance_adjustment"},
        {"transaction_id": None, "date": None, "description": None,
         "amount": None, "financial_class": None, "category": None},
    ]
    choice = {"finish_reason": "stop"}

    def respond(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
        choice["message"] = {"content": json.dumps({"transactions": rows})}
        return httpx.Response(200, json={"choices": [choice]})

    groq_transport(respond)
    result = extract(b"image", "image/png")
    assert result[0]["reference"] == "PO-123"
    assert (result[0]["financial_class"], result[0]["category"]) == ("purchase_order", "outstanding_purchase_orders")
    assert [row["amount_cents"] for row in result] == [1250, -29, None]
    assert all(row["review_status"] == "draft" for row in result)
    invalid_updates = (
        {"category": "student_dues"},
        {"category": "cash_deposits"},
        {"date": "2026-11-31"},
        {"amount": 12.345},
        {"review_status": "approved"},
    )
    for invalid_fields in invalid_updates:
        with monkeypatch.context() as patch:
            for key, value in invalid_fields.items():
                patch.setitem(rows[0], key, value)
            with pytest.raises(HTTPException) as error:
                extract(b"image", "image/png")
            assert error.value.status_code == 502
            assert "PO-123" not in error.value.detail
    choice["finish_reason"] = "length"
    with pytest.raises(HTTPException, match="Extraction failed"):
        extract(b"image", "image/png")
    assert "PO-123" not in capsys.readouterr().out


@pytest.mark.parametrize("status_code", [200, 502])
def test_groq_html_response_returns_safe_error(groq_transport, status_code):
    from app.extraction import extract

    def respond(request):
        return httpx.Response(
            status_code,
            text="<html>private upstream error</html>",
            headers={"content-type": "text/html"},
        )

    groq_transport(respond)
    with pytest.raises(HTTPException) as error:
        extract(b"image", "image/png")
    assert error.value.status_code == 502
    assert error.value.detail == "Extraction failed. Source retained; retry processing."


def test_real_pdf_parser_and_extraction_input(monkeypatch, groq_transport):
    pymupdf = pytest.importorskip("pymupdf", reason="PyPI network unavailable in local environment")
    from app.extraction import extract
    from app.models import CATEGORIES
    monkeypatch.setenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Student dues $12.50 November 16, 2026")
    content = doc.tobytes()
    doc.close()
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": '{"transactions":[]}'}}],
        })

    groq_transport(respond)
    assert extract(content, "application/pdf") == []
    assert "Student dues" in seen[0]["messages"][1]["content"]
    response_format = seen[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    fields = schema["$defs"]["ExtractedTransaction"]["properties"]
    assert set(fields) == {"transaction_id", "amount", "date", "description", "financial_class", "category"}
    assert fields["financial_class"]["anyOf"][0]["enum"] == list(CATEGORIES)
    assert fields["category"]["anyOf"][0]["enum"] == [category for values in CATEGORIES.values() for category in values]
    monkeypatch.setenv("GROQ_TEXT_MODEL", "custom-model")
    assert extract(content, "application/pdf") == []
    assert seen[1]["response_format"] == {"type": "json_object"}
    assert json.dumps(schema) in seen[1]["messages"][0]["content"]


def valid_row(amount=1250, date="2026-11-16", category="student_dues", kind="revenue"):
    return {"reference": None, "date": date, "description": "Dues", "amount_cents": amount, "financial_class": kind, "category": category, "review_status": "draft"}
