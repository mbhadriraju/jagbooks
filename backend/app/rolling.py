"""Immutable Storage objects with a transactional current-version pointer."""
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException

from app.extraction import extract
from app.workbooks import TEMPLATE, WORKBOOK_BUCKET, XLSX_MIME, apply_batch, batch_changes, load, summary, validate


def record(db, table, record_id):
    rows = db.rows(table, id=f"eq.{record_id}", limit=1)
    if not rows:
        raise HTTPException(404, "Record not found.")
    return rows[0]


def current(db, sheet_id):
    sheet = record(db, "finance_sheets", sheet_id)
    head = sheet.get("current_workbook_id")
    return record(db, "finance_workbook_revisions", head) if head else None


def bytes_for(db, revision):
    obj = record(db, "finance_workbook_objects", revision["object_id"])
    content = db.download(obj["object_path"], bucket=WORKBOOK_BUCKET)
    if len(content) != obj["size_bytes"] or sha256(content).hexdigest() != obj["sha256"]:
        raise HTTPException(409, "Stored workbook checksum mismatch.")
    return content


def stage(db, sheet_id, content):
    sheet = record(db, "finance_sheets", sheet_id)
    object_id = str(uuid4())
    obj = db.insert("finance_workbook_objects", {
        "id": object_id, "sheet_id": str(sheet_id), "group_id": sheet["group_id"],
        "object_path": f"{sheet['group_id']}/{sheet_id}/versions/{object_id}.xlsx",
        "sha256": sha256(content).hexdigest(), "size_bytes": len(content), "created_by": db.user["id"],
    })[0]
    db.upload(obj["object_path"], content, XLSX_MIME, bucket=WORKBOOK_BUCKET)
    return obj


def commit(db, sheet_id, expected, obj, operation, *, totals=None, source=None, restored=None):
    result = db.rpc("finance_commit_workbook", {
        "p_sheet": str(sheet_id), "p_expected": str(expected) if expected else None,
        "p_object": obj["id"] if obj else None, "p_operation": operation,
        "p_summary": totals or {}, "p_source": str(source) if source else None,
        "p_restored": str(restored) if restored else None,
    })
    if result.get("conflict"):
        raise HTTPException(409, {"message": "Workbook changed. Download current version and retry.", "current_version_id": result.get("current_id")})
    return result["revision"]


def initialize(db, sheet_id):
    head = current(db, sheet_id)
    if head:
        return head
    content = TEMPLATE.read_bytes()
    validate(content)
    obj = stage(db, sheet_id, content)
    try:
        return commit(db, sheet_id, None, obj, "initialize", totals=summary(content))
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # Two initializers can stage independently; only one wins the DB lock.
        head = current(db, sheet_id)
        if not head:
            raise
        return head


def extract_source(db, file_id):
    source = record(db, "finance_files", file_id)
    if source["status"] == "uploading":
        raise HTTPException(409, "Source upload incomplete.")
    if source["status"] != "processed":
        content = db.download(source["object_path"])
        if sha256(content).hexdigest() != source["sha256"]:
            raise HTTPException(409, "Stored source checksum mismatch.")
        rows = extract(content, source["mime_type"])
        db.rpc("finance_finish_extraction", {"p_file": str(file_id), "p_rows": rows})
    extracted = db.rows("finance_extractions", file_id=f"eq.{file_id}", limit=1)
    if not extracted:
        raise HTTPException(409, "Source extraction unavailable.")
    return source, extracted[0]["rows"]


def source_review(db, file_id):
    source, rows = extract_source(db, file_id)
    head = current(db, source["sheet_id"])
    if not head:
        raise HTTPException(409, "Initialize the workbook in Analysis before reviewing sources.")
    applied = str(file_id) in head["applied_file_ids"]
    changes, blocked = [], None
    if not applied:
        try:
            workbook = load(bytes_for(db, head))
            changes = batch_changes(workbook, rows)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            blocked = exc.detail
    return {
        "file": {key: source[key] for key in ("id", "sheet_id", "filename")},
        "rows": rows, "changes": changes, "blocked": blocked, "applied": applied,
        "version_id": head["id"],
    }


def publish_review(db, file_id, expected):
    source, rows = extract_source(db, file_id)
    sheet_id = source["sheet_id"]
    head = current(db, sheet_id)
    if head and str(file_id) in head["applied_file_ids"]:
        return head
    if (head["id"] if head else None) != (str(expected) if expected else None):
        raise HTTPException(409, "Workbook changed. Refresh the review before applying.")
    content = apply_batch(bytes_for(db, head), rows)
    obj = stage(db, sheet_id, content)
    return commit(db, sheet_id, head["id"], obj, "source", totals=summary(content), source=file_id)


def source_update(db, file_id):
    source = record(db, "finance_files", file_id)
    head = current(db, source["sheet_id"])
    if head and str(file_id) in head["applied_file_ids"]:
        return head
    _, rows = extract_source(db, file_id)
    # Rebase document additions after concurrent uploads/manual replacements/restores.
    # Extraction is reused. Each successful commit records its source exactly once.
    for attempt in range(3):
        head = initialize(db, source["sheet_id"])
        if str(file_id) in head["applied_file_ids"]:
            return head
        content = apply_batch(bytes_for(db, head), rows)
        obj = stage(db, source["sheet_id"], content)
        try:
            return commit(db, source["sheet_id"], head["id"], obj, "source", totals=summary(content), source=file_id)
        except HTTPException as exc:
            if exc.status_code != 409 or attempt == 2:
                raise
    raise HTTPException(409, "Workbook changed repeatedly. Retry processing stored source.")


def manual_replace(db, sheet_id, content, expected):
    head = current(db, sheet_id)
    head_id = head["id"] if head else None
    if head_id is not None and expected is None:
        raise HTTPException(428, "expected_version_id is required; download the latest workbook first.")
    expected_id = str(expected) if expected else None
    if expected_id != head_id:
        raise HTTPException(409, {"message": "Workbook changed. Download current version and merge edits.", "current_version_id": head_id})
    validate(content, bytes_for(db, head) if head else None)
    obj = stage(db, sheet_id, content)
    return commit(db, sheet_id, head_id, obj, "manual" if head else "initialize", totals=summary(content))


def restore(db, version_id, expected):
    target = record(db, "finance_workbook_revisions", version_id)
    return commit(db, target["sheet_id"], expected, None, "restore", restored=version_id)
