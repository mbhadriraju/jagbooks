from copy import deepcopy
from datetime import date
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app import rolling
from app.main import store_upload
from app.models import CATEGORIES
from app.workbooks import DESTINATIONS, TEMPLATE, WORKBOOK_BUCKET, XLSX_MIME, apply_batch, summary, validate
from test_api import FakeDB, SHEET, db, request, valid_row


def amount(db, revision=None, cell="E5", sheet="Income Statement"):
    revision = revision or rolling.current(db, SHEET["id"])
    return load_workbook(BytesIO(rolling.bytes_for(db, revision)))[sheet][cell].value


def edited(content, value, cell="E5", sheet="Income Statement"):
    wb = load_workbook(BytesIO(content))
    wb[sheet][cell] = value
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def upload_source(db, monkeypatch, content=b"%PDF-one", value=1250):
    monkeypatch.setattr("app.rolling.extract", lambda *args: [valid_row(value)])
    f = store_upload(db, SHEET, "report.pdf", "application/pdf", content)
    return f, rolling.source_update(db, f["id"])


def test_two_uploads_add_to_latest_and_keep_prior_bytes(db, monkeypatch):
    first_source, first = upload_source(db, monkeypatch)
    first_bytes = rolling.bytes_for(db, first)
    second_source, second = upload_source(db, monkeypatch, b"%PDF-two", 225)
    assert amount(db, first) == 12.5 and amount(db, second) == 14.75
    assert rolling.bytes_for(db, first) == first_bytes
    assert first_source["id"] in second["applied_file_ids"] and second_source["id"] in second["applied_file_ids"]
    assert len(db.tables["finance_workbook_revisions"]) == 3  # initialization plus two source versions
    assert rolling.source_update(db, first_source["id"])["id"] == second["id"]
    assert amount(db) == 14.75


def test_manual_upload_becomes_latest_then_next_source_adds(db, monkeypatch):
    _, first = upload_source(db, monkeypatch)
    replacement = edited(rolling.bytes_for(db, first), 80)
    manual = rolling.manual_replace(db, SHEET["id"], replacement, first["id"])
    assert rolling.bytes_for(db, manual) == replacement and amount(db) == 80
    _, next_head = upload_source(db, monkeypatch, b"%PDF-new", 225)
    assert amount(db) == 82.25 and next_head["parent_id"] == manual["id"]
    assert next_head["summary"]["monthly"][3]["revenue_cents"] == 8225


def test_restore_overrides_head_without_destroying_history_and_upload_uses_restored_base(db, monkeypatch):
    _, first = upload_source(db, monkeypatch)
    second_file, second = upload_source(db, monkeypatch, b"%PDF-second", 225)
    restored = rolling.restore(db, first["id"], second["id"])
    assert restored["version"] == second["version"] + 1
    assert restored["restored_from_id"] == first["id"] and amount(db) == 12.5
    assert amount(db, second) == 14.75
    _, next_head = upload_source(db, monkeypatch, b"%PDF-third", 500)
    assert amount(db) == 17.5
    # Reapplying an upload excluded by restore uses cached extraction, once.
    def no_reextract(*args): raise AssertionError("Repeated Groq call")
    monkeypatch.setattr("app.rolling.extract", no_reextract)
    reapplied = rolling.source_update(db, second_file["id"])
    assert amount(db) == 19.75 and second_file["id"] in reapplied["applied_file_ids"]


def test_manual_replacement_preserves_applied_source_identity(db, monkeypatch):
    source, first = upload_source(db, monkeypatch)
    manual = rolling.manual_replace(db, SHEET["id"], edited(rolling.bytes_for(db, first), 0), first["id"])
    assert rolling.source_update(db, source["id"])["id"] == manual["id"]
    assert amount(db) == 0


def test_stale_manual_edit_and_restore_cannot_clobber_latest(db, monkeypatch):
    _, first = upload_source(db, monkeypatch)
    _, second = upload_source(db, monkeypatch, b"%PDF-new", 225)
    for action in (
        lambda: rolling.manual_replace(db, SHEET["id"], edited(rolling.bytes_for(db, first), 99), first["id"]),
        lambda: rolling.restore(db, first["id"], first["id"]),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 409
    assert rolling.current(db, SHEET["id"])["id"] == second["id"] and amount(db) == 14.75
    with pytest.raises(HTTPException) as error:
        rolling.manual_replace(db, SHEET["id"], rolling.bytes_for(db, first), None)
    assert error.value.status_code == 428


def test_workbook_storage_failure_leaves_previous_head(db):
    head = rolling.initialize(db, SHEET["id"])
    db.fail_upload = True
    with pytest.raises(HTTPException):
        rolling.manual_replace(db, SHEET["id"], edited(rolling.bytes_for(db, head), 42), head["id"])
    assert rolling.current(db, SHEET["id"])["id"] == head["id"]


def test_source_rebases_when_manual_edit_wins_race(db, monkeypatch):
    head = rolling.initialize(db, SHEET["id"])
    actual_rpc = db.rpc
    raced = []
    def racing_rpc(name, data):
        if name == "finance_commit_workbook" and data["p_operation"] == "source" and not raced:
            raced.append(True)
            rolling.manual_replace(db, SHEET["id"], edited(rolling.bytes_for(db, head), 80), head["id"])
        return actual_rpc(name, data)
    db.rpc = racing_rpc
    _, result = upload_source(db, monkeypatch)
    assert raced and amount(db, result) == 92.5


def test_incomplete_extraction_or_out_of_period_source_never_changes_existing_head(db, monkeypatch):
    head = rolling.initialize(db, SHEET["id"])
    for i, row in enumerate(({**valid_row(), "date": None}, valid_row(date="2028-01-02"))):
        monkeypatch.setattr("app.rolling.extract", lambda *args, r=row: [r])
        f = store_upload(db, SHEET, "x.pdf", "application/pdf", b"%PDF-" + str(i).encode())
        with pytest.raises(HTTPException) as error:
            rolling.source_update(db, f["id"])
        assert error.value.status_code == 422
        assert rolling.current(db, SHEET["id"])["id"] == head["id"]
        assert db.rows("finance_files", id="eq." + f["id"])[0]["status"] == "processed"
        assert db.rows("finance_extractions", file_id="eq." + f["id"])


def test_upload_bad_workbook_rejects_structure_formulas_and_subcent_inputs():
    base = TEMPLATE.read_bytes()
    for content in (b"not an xlsx", edited(base, "bad"), edited(base, 0.001), edited(base, "=1+1"), edited(base, "Different label", "A5"), edited(base, None, "E10")):
        with pytest.raises(HTTPException) as error:
            validate(content, base)
        assert error.value.status_code == 422


def test_template_mapping_covers_all_categories_preserves_balances_formulas_and_styles():
    base = edited(TEMPLATE.read_bytes(), 500, cell="B4", sheet="Balance Sheet")
    base = edited(base, 2000, cell="B12", sheet="Cash Flow Statement")
    wb = load_workbook(BytesIO(base))
    for kind, categories in CATEGORIES.items():
        for category in categories:
            updated = apply_batch(base, [valid_row(125, category=category, kind=kind)])
            result = load_workbook(BytesIO(updated))
            sheet, row = DESTINATIONS[(kind, category)]
            col = 5 if sheet == "Income Statement" else 6
            assert result[sheet].cell(row, col).value == 1.25
            assert result["Balance Sheet"]["B4"].value == wb["Balance Sheet"]["B4"].value == 500
            assert result["Cash Flow Statement"]["B12"].value == 2000
            assert result["Cash Flow Statement"]["F5"].value == wb["Cash Flow Statement"]["F5"].value
            for name in wb.sheetnames:
                assert result[name].tables.keys() == wb[name].tables.keys()
                for old_row in wb[name]:
                    for cell in old_row:
                        new = result[name][cell.coordinate]
                        if cell.data_type == "f": assert new.value == cell.value
                        assert new.number_format == cell.number_format
            assert result.calculation.fullCalcOnLoad is True


def test_download_replace_restore_routes_and_workbook_summary(db, monkeypatch):
    _, first = upload_source(db, monkeypatch)
    download = request("GET", f"/sheets/{SHEET['id']}/workbook")
    assert download.status_code == 200 and download.headers["x-workbook-version"] == first["id"]
    replacement = edited(download.content, 42)
    response = request("PUT", f"/sheets/{SHEET['id']}/workbook?expected_version_id={first['id']}", content=replacement, headers={"Content-Type": XLSX_MIME})
    assert response.status_code == 200
    manual = response.json()
    totals = request("GET", f"/sheets/{SHEET['id']}/summary").json()
    assert totals["monthly"][3]["revenue_cents"] == 4200
    restore = request("POST", f"/versions/{first['id']}/restore", json={"expected_version_id": manual["id"]})
    assert restore.status_code == 201 and amount(db) == 12.5
    assert request("GET", f"/versions/{manual['id']}/download").content == replacement
    versions = request("GET", f"/sheets/{SHEET['id']}/versions").json()
    assert len(versions) == 4
    assert request("PUT", f"/sheets/{SHEET['id']}/workbook", content=replacement, headers={"Content-Type": XLSX_MIME}).status_code == 428


def test_workbook_transport_uses_separate_bucket(monkeypatch):
    from app.database import Supabase
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable-test")
    db = Supabase("user-jwt")
    paths = []
    db.request = lambda method, path, **kwargs: paths.append((method, path, kwargs))
    db.upload("group/sheet/versions/id.xlsx", b"xlsx", XLSX_MIME, bucket=WORKBOOK_BUCKET)
    db.download("group/sheet/versions/id.xlsx", bucket=WORKBOOK_BUCKET)
    assert all("/bvw-finance-workbooks/" in p[1] for p in paths)
    assert paths[0][2]["content_type"] == XLSX_MIME


def test_review_stages_without_applying_and_rejects_stale_confirmation(db, monkeypatch):
    initial = rolling.initialize(db, SHEET["id"])
    source = store_upload(db, SHEET, "report.pdf", "application/pdf", b"%PDF-reviewed")
    monkeypatch.setattr("app.rolling.extract", lambda *args: [valid_row(1250)])
    preview = rolling.source_review(db, source["id"])
    assert not preview["applied"] and not preview["blocked"]
    assert preview["changes"][0]["after_cents"] == 1250
    assert rolling.current(db, SHEET["id"])["id"] == initial["id"]

    manual = rolling.manual_replace(db, SHEET["id"], edited(rolling.bytes_for(db, initial), 5), initial["id"])
    with pytest.raises(HTTPException) as conflict:
        rolling.publish_review(db, source["id"], preview["version_id"])
    assert conflict.value.status_code == 409 and amount(db) == 5

    preview = rolling.source_review(db, source["id"])
    assert preview["changes"][0]["before_cents"] == 500
    assert preview["changes"][0]["after_cents"] == 1750
    applied = rolling.publish_review(db, source["id"], manual["id"])
    assert amount(db) == 17.5
    assert rolling.publish_review(db, source["id"], manual["id"])["id"] == applied["id"]
    assert rolling.source_review(db, source["id"])["applied"]


def test_dashboard_uses_cash_statement_inputs_and_flags_custom_formula():
    from app.workbooks import dashboard

    content = apply_batch(TEMPLATE.read_bytes(), [valid_row(1250, category="bank_deposits", kind="cash_flow")])
    preview = dashboard(content)
    assert preview["monthly"][3]["cash_flow_cents"] == 1250
    assert preview["monthly"][3]["net_cents"] == 0
    assert preview["tabs"][2]["computed"]["F11"] == 1250
    assert dashboard(edited(content, "=1", "F11", "Cash Flow Statement"))["monthly"][3]["cash_flow_cents"] is None


def test_review_http_workflow_stores_source_before_extraction(db, monkeypatch):
    from app.auth import get_google_db
    from app.main import app

    app.dependency_overrides[get_google_db] = lambda: db
    head = rolling.initialize(db, SHEET["id"])
    calls = []
    def extract(content, mime):
        calls.append((content, mime))
        return [valid_row(1250)]

    monkeypatch.setattr("app.rolling.extract", extract)
    endpoint = f"/sheets/{SHEET['id']}/files?review=true&filename=receipt.pdf"
    content = b"%PDF-review-test"
    staged = request("POST", endpoint, content=content, headers={"content-type": "application/pdf"})
    assert staged.status_code == 201 and not calls
    file_id = staged.json()["file"]["id"]
    source = db.rows("finance_files", id=f"eq.{file_id}")[0]
    assert db.objects[source["object_path"]] == content
    assert source["status"] == "uploaded"

    review_url = f"/files/{file_id}/review"
    reviewed = request("POST", review_url)
    assert reviewed.status_code == 200 and calls == [(content, "application/pdf")]
    assert reviewed.json()["version_id"] == head["id"]
    assert rolling.current(db, SHEET["id"])["id"] == head["id"]
    published = request("PUT", review_url, json={"expected_version_id": head["id"]})
    assert published.status_code == 200 and published.json()["version"] == 2
    assert amount(db) == 12.5
