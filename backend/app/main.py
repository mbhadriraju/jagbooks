from hashlib import sha256
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.auth import get_db, get_google_db, provider_status
from app.database import MAX_FILE_BYTES, Supabase, settings
from app.observability import RequestLoggingMiddleware
from app.schemas import RestoreInput, SheetInput, TransactionInput
from app.spreadsheet import workbook
from app import rolling
from app.workbooks import XLSX_MIME, dashboard

app = FastAPI(title="BVW Debate Financial Consolidation", version="1.0.0")
app.add_middleware(RequestLoggingMiddleware)


def record(db, table, record_id):
    rows = db.rows(table, id=f"eq.{record_id}", limit=1)
    if not rows:
        raise HTTPException(404, "Record not found.")
    return rows[0]



@app.get("/health")
def health():
    settings()
    return {"status": "ok", "service": "bvw-finance", "database": "supabase", "connectivity": "not_checked"}


@app.get("/auth/me")
def me(response: Response, db: Supabase = Depends(get_db)):
    response.headers["Cache-Control"] = "private, no-store"
    return {"id": db.user["id"], "email": db.user.get("email")}


@app.get("/auth/browser-config")
def browser_auth_config(response: Response):
    try:
        url, key = settings()
    except RuntimeError:
        raise HTTPException(503, "Browser authentication configuration unavailable.") from None
    if not key.startswith("sb_publishable_"):
        raise HTTPException(503, "Browser authentication configuration unavailable.")
    response.headers["Cache-Control"] = "no-store"
    return {"url": url, "key": key}


@app.get("/auth/providers")
def oauth_providers(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return provider_status()


@app.get("/auth/access")
def auth_access(response: Response, db: Supabase = Depends(get_db)):
    response.headers["Cache-Control"] = "private, no-store"
    groups = db.rows("finance_groups", order="name")
    return {"user_id": db.user["id"], "can_access_finances": bool(groups), "groups": groups}


@app.get("/finance/access")
def finance_access(response: Response, db: Supabase = Depends(get_google_db)):
    return auth_access(response, db)


@app.get("/groups")
def groups(db: Supabase = Depends(get_db)):
    return db.rows("finance_groups", order="name")


@app.get("/sheets")
def sheets(db: Supabase = Depends(get_db)):
    return db.rows("finance_sheets", order="created_at.desc", limit=100)


@app.post("/sheets", status_code=201)
def create_sheet(data: SheetInput, db: Supabase = Depends(get_db)):
    sheet = db.insert("finance_sheets", data.model_dump(mode="json"))[0]
    rolling.initialize(db, sheet["id"])
    return record(db, "finance_sheets", sheet["id"])


@app.get("/sheets/{sheet_id}/files")
def files(sheet_id: UUID, offset: int = Query(0, ge=0), db: Supabase = Depends(get_db)):
    record(db, "finance_sheets", sheet_id)
    head = rolling.current(db, sheet_id)
    applied = set(head["applied_file_ids"]) if head else set()
    rows = db.rows("finance_files", sheet_id=f"eq.{sheet_id}", order="created_at.desc,id", limit=100, offset=offset)
    return [{**row, "applied_to_current_workbook": row["id"] in applied} for row in rows]


def store_upload(db, sheet, filename, mime, content):
    digest = sha256(content).hexdigest()
    existing = db.rows("finance_files", sheet_id=f"eq.{sheet['id']}", sha256=f"eq.{digest}", limit=1)
    if existing:
        f = existing[0]
        if f["status"] != "uploading":
            return f  # Byte-identical retries never post twice.
        # Reconcile a previous upload whose final metadata update failed.
        try:
            stored = db.download(f["object_path"])
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            if sha256(stored).hexdigest() != digest:
                raise HTTPException(409, "Stored file checksum mismatch.")
            return db.update("finance_files", f["id"], {"status": "uploaded"})[0]
        if f["uploaded_by"] != db.user["id"]:
            raise HTTPException(409, "Original uploader must retry incomplete upload.")
    else:
        file_id = str(uuid4())
        f = db.insert("finance_files", {
            "id": file_id, "group_id": sheet["group_id"], "sheet_id": sheet["id"],
            "uploaded_by": db.user["id"], "filename": filename, "mime_type": mime,
            "size_bytes": len(content), "sha256": digest,
            "object_path": f"{sheet['group_id']}/{file_id}/source",
        })[0]
    # Immutable key; never upsert financial source files. Metadata survives failure for retry.
    db.upload(f["object_path"], content, mime)
    return db.update("finance_files", f["id"], {"status": "uploaded"})[0]


def valid_signature(content, mime):
    return {
        "application/pdf": content.startswith(b"%PDF-"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }.get(mime, False)


@app.post("/sheets/{sheet_id}/files", status_code=201)
async def upload(
    sheet_id: UUID, request: Request, filename: str = Query(min_length=1, max_length=255),
    review: bool = False, db: Supabase = Depends(get_db),
):
    sheet = await run_in_threadpool(record, db, "finance_sheets", sheet_id)
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, "Maximum file size is 10 MiB.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not valid_signature(content, mime):
        raise HTTPException(415, "Upload a PDF, PNG, JPEG, or WebP with matching Content-Type.")
    stored = await run_in_threadpool(store_upload, db, sheet, filename, mime, content)
    if review:
        return {"file": {key: stored[key] for key in ("id", "sheet_id", "filename")}}
    return await run_in_threadpool(process_stored, db, stored["id"])


@app.post("/files/{file_id}/review")
def preview_source(file_id: UUID, response: Response, db: Supabase = Depends(get_google_db)):
    response.headers["Cache-Control"] = "private, no-store"
    return rolling.source_review(db, file_id)


@app.put("/files/{file_id}/review")
def publish_source(file_id: UUID, data: RestoreInput, db: Supabase = Depends(get_google_db)):
    version = rolling.publish_review(db, file_id, data.expected_version_id)
    return {"version": version["version"]}


@app.get("/files/{file_id}/download")
def download(file_id: UUID, db: Supabase = Depends(get_db)):
    f = record(db, "finance_files", file_id)
    if f["status"] == "uploading":
        raise HTTPException(409, "Upload incomplete.")
    return {"url": db.signed_url(f["object_path"]), "expires_in": 300}


def process_stored(db, file_id):
    try:
        version = rolling.source_update(db, file_id)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, {"message": "Source retained; workbook update did not complete. Retry processing after resolving the error.", "file_id": str(file_id), "reason": exc.detail}) from None
    return {"file": record(db, "finance_files", file_id), "workbook_version": version}


@app.post("/files/{file_id}/process")
def process(file_id: UUID, db: Supabase = Depends(get_db)):
    return process_stored(db, file_id)


@app.get("/sheets/{sheet_id}/transactions")
def transactions(sheet_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), db: Supabase = Depends(get_db)):
    record(db, "finance_sheets", sheet_id)
    return db.rows("finance_transactions", sheet_id=f"eq.{sheet_id}", order="created_at,id", limit=limit, offset=offset)


@app.post("/sheets/{sheet_id}/transactions", status_code=201)
def add_transaction(sheet_id: UUID, data: TransactionInput, db: Supabase = Depends(get_db)):
    sheet = record(db, "finance_sheets", sheet_id)
    return db.insert("finance_transactions", {**data.model_dump(mode="json"), "sheet_id": str(sheet_id), "group_id": sheet["group_id"]})[0]


@app.put("/transactions/{transaction_id}")
def review(transaction_id: UUID, data: TransactionInput, db: Supabase = Depends(get_db)):
    record(db, "finance_transactions", transaction_id)
    rows = db.update("finance_transactions", transaction_id, data.model_dump(mode="json"))
    if not rows:
        raise HTTPException(404, "Transaction unavailable.")
    return rows[0]


@app.get("/sheets/{sheet_id}/summary")
def summary(sheet_id: UUID, db: Supabase = Depends(get_db)):
    record(db, "finance_sheets", sheet_id)
    head = rolling.current(db, sheet_id)
    if not head:
        raise HTTPException(404, "Workbook not initialized.")
    return {**head["summary"], "version_id": head["id"], "version": head["version"]}


@app.post("/sheets/{sheet_id}/versions", status_code=201)
def snapshot(sheet_id: UUID, db: Supabase = Depends(get_db)):
    head = rolling.initialize(db, sheet_id)
    return rolling.commit(db, sheet_id, head["id"], None, "checkpoint")


@app.get("/sheets/{sheet_id}/versions")
def versions(sheet_id: UUID, offset: int = Query(0, ge=0), db: Supabase = Depends(get_db)):
    record(db, "finance_sheets", sheet_id)
    return db.rows("finance_workbook_revisions", select="id,sheet_id,object_id,version,parent_id,operation,source_file_id,restored_from_id,created_at,created_by", sheet_id=f"eq.{sheet_id}", order="version.desc", limit=100, offset=offset)


def workbook_response(content, version):
    return Response(content, media_type=XLSX_MIME, headers={
        "Content-Disposition": f'attachment; filename="bvw-v{version["version"]}.xlsx"',
        "ETag": '"' + version["id"] + '"', "X-Workbook-Version": version["id"], "Cache-Control": "no-store",
    })


@app.get("/sheets/{sheet_id}/workbook")
def latest_workbook(sheet_id: UUID, db: Supabase = Depends(get_db)):
    head = rolling.current(db, sheet_id)
    if not head:
        raise HTTPException(404, "Workbook not initialized.")
    return workbook_response(rolling.bytes_for(db, head), head)


@app.get("/sheets/{sheet_id}/workbook/preview")
def workbook_preview(sheet_id: UUID, response: Response, db: Supabase = Depends(get_google_db)):
    head = rolling.current(db, sheet_id)
    if not head:
        raise HTTPException(404, "Workbook not initialized.")
    response.headers["Cache-Control"] = "private, no-store"
    return {**dashboard(rolling.bytes_for(db, head)), "version": head["version"]}


@app.get("/sheets/{sheet_id}/workbook/head")
def workbook_head(sheet_id: UUID, db: Supabase = Depends(get_db)):
    head = rolling.current(db, sheet_id)
    if not head:
        raise HTTPException(404, "Workbook not initialized.")
    return head


@app.post("/sheets/{sheet_id}/workbook/initialize", status_code=201)
def initialize_workbook(sheet_id: UUID, db: Supabase = Depends(get_db)):
    return rolling.initialize(db, sheet_id)


@app.put("/sheets/{sheet_id}/workbook")
async def replace_workbook(sheet_id: UUID, request: Request, expected_version_id: UUID | None = None, db: Supabase = Depends(get_db)):
    # Authenticate/authorize before accepting an uploaded workbook body.
    await run_in_threadpool(record, db, "finance_sheets", sheet_id)
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime != XLSX_MIME:
        raise HTTPException(415, "Use the XLSX Content-Type.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, "Maximum workbook size is 10 MiB.")
        chunks.append(chunk)
    return await run_in_threadpool(rolling.manual_replace, db, sheet_id, b"".join(chunks), expected_version_id)


@app.post("/versions/{version_id}/restore", status_code=201)
def restore_version(version_id: UUID, data: RestoreInput, db: Supabase = Depends(get_db)):
    return rolling.restore(db, version_id, data.expected_version_id)


@app.get("/versions/{version_id}/download")
def export(version_id: UUID, db: Supabase = Depends(get_db)):
    rows = db.rows("finance_workbook_revisions", id=f"eq.{version_id}", limit=1)
    if rows:
        return workbook_response(rolling.bytes_for(db, rows[0]), rows[0])
    # Previous transaction-only snapshots remain downloadable; they are not XLSX heads.
    old = record(db, "finance_sheet_versions", version_id)
    return workbook_response(workbook(old["snapshot"]), old)


@app.get("/sheets/{sheet_id}/transaction-snapshots")
def transaction_snapshots(sheet_id: UUID, offset: int = Query(0, ge=0), db: Supabase = Depends(get_db)):
    record(db, "finance_sheets", sheet_id)
    return db.rows("finance_sheet_versions", select="id,sheet_id,version,created_at,created_by", sheet_id=f"eq.{sheet_id}", order="version.desc", limit=100, offset=offset)
