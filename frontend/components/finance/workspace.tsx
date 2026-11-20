"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { api, LoginNotice, useSession, XLSX } from "./session";
import { Charts, WorkbookGrid, type Workbook } from "./workbook";

const maxUploadMiB = process.env.NODE_ENV === "development" ? 10 : 4;

type Sheet = { id: string; name: string; current_workbook_id: string | null };
type Download = { id: string; filename: string };
type Group = { id: string; name: string };
type Source = { id: string; filename: string; status: string; applied_to_current_workbook: boolean };

export function FinanceWorkspace({ preview = false }: { preview?: boolean }) {
  const session = useSession();
  if (!session) return preview ? (
    <section className="section">
      <div className="section-head"><h2>Latest financial statement</h2><span className="muted">Private workbook</span></div>
      <WorkbookGrid preview />
      <div className="preview-note">{session === undefined ? <p>Checking your session…</p> : <LoginNotice />}</div>
    </section>
  ) : <div className="notice">{session === undefined ? <p>Checking your session…</p> : <LoginNotice />}</div>;
  return <Workspace key={session.user.id} token={session.access_token} userId={session.user.id} preview={preview} />;
}

function Workspace({ token, userId, preview }: { token: string; userId: string; preview: boolean }) {
  const router = useRouter();
  const [groups, setGroups] = useState<Group[]>([]);
  const [sheets, setSheets] = useState<Sheet[]>([]);
  const [sheetId, setSheetId] = useState("");
  const [downloadState, setDownloadState] = useState<{ key: string; version: Download | null }>();
  const [workbookState, setWorkbookState] = useState<{ id: string; workbook: Workbook }>();
  const [sourceState, setSourceState] = useState<{ key: string; sources: Source[] }>();
  const [sourceOffset, setSourceOffset] = useState(0);
  const [groupId, setGroupId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const sheet = sheets.find((item) => item.id === sheetId);
  const downloadKey = `bvw-xlsx:${userId}:${sheetId}`;
  const versionKey = sheet?.current_workbook_id;
  const workbook = versionKey && workbookState?.id === versionKey ? workbookState.workbook : undefined;
  const sourceKey = `${sheetId}:${versionKey}:${sourceOffset}`;
  const sources = sourceState?.key === sourceKey ? sourceState.sources : [];
  const version = downloadState?.key === downloadKey ? downloadState.version : null;

  useEffect(() => {
    const request = new AbortController();
    const options = { signal: request.signal };
    api("/finance/access", token, options).then(async (response) => {
      const access: { groups: Group[] } = await response.json();
      const items: Sheet[] = access.groups.length ? await (await api("/sheets", token, options)).json() : [];
      if (request.signal.aborted) return;
      setGroups(access.groups);
      setGroupId((id) => access.groups.some((group) => group.id === id) ? id : access.groups[0]?.id ?? "");
      setSheets(items);
      setSheetId((id) => items.some((sheet) => sheet.id === id) ? id : items.find((sheet) => sheet.current_workbook_id)?.id ?? items[0]?.id ?? "");
    }).catch((problem: Error) => {
      if (!request.signal.aborted) {
        setGroups([]);
        setSheets([]);
        setWorkbookState(undefined);
        setError(problem.message);
      }
    }).finally(() => { if (!request.signal.aborted) setLoading(false); });
    return () => request.abort();
  }, [token, reload]);

  useEffect(() => {
    if (!sheet || !versionKey) return;
    const request = new AbortController();
    const options = { signal: request.signal };
    api(`/sheets/${sheet.id}/workbook/preview`, token, options)
      .then((response) => response.json())
      .then((data: Workbook) => {
        if (request.signal.aborted) return;
        let downloaded: Download | null = null;
        try { downloaded = JSON.parse(localStorage.getItem(downloadKey) || "null"); } catch { /* Ignore invalid storage. */ }
        setDownloadState({ key: downloadKey, version: downloaded });
        setWorkbookState({ id: versionKey, workbook: data });
      })
      .catch((problem: Error) => { if (!request.signal.aborted) setError(problem.message); });
    return () => request.abort();
  }, [sheet, versionKey, token, reload, downloadKey]);

  useEffect(() => {
    if (preview || !sheet?.current_workbook_id) return;
    const request = new AbortController();
    api(`/sheets/${sheet.id}/files?offset=${sourceOffset}`, token, { signal: request.signal })
      .then((response) => response.json())
      .then((items: Source[]) => { if (!request.signal.aborted) setSourceState({ key: sourceKey, sources: items }); })
      .catch((problem: Error) => { if (!request.signal.aborted) setError(problem.message); });
    return () => request.abort();
  }, [sheet, preview, token, sourceOffset, sourceKey, reload]);

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    setNotice("");
    setError("");
    try { await action(); } catch (problem) { setError((problem as Error).message); }
    finally { setBusy(""); }
  }

  async function download() {
    const response = await api(`/sheets/${sheetId}/workbook`, token);
    const id = response.headers.get("x-workbook-version");
    if (!id) throw new Error("Could not identify the downloaded workbook version.");
    const filename = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "workbook.xlsx";
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    const base = { id, filename };
    setDownloadState({ key: downloadKey, version: base });
    try { localStorage.setItem(downloadKey, JSON.stringify(base)); } catch { /* Available for this visit. */ }
  }

  function fileFrom(event: FormEvent<HTMLFormElement>) {
    const file = new FormData(event.currentTarget).get("file");
    if (!(file instanceof File) || !file.size) throw new Error("Choose a file first.");
    if (file.size > maxUploadMiB * 1024 * 1024) throw new Error(`Maximum file size is ${maxUploadMiB} MiB.`);
    return file;
  }

  function upload(event: FormEvent<HTMLFormElement>, replacement: boolean) {
    event.preventDefault();
    const form = event.currentTarget;
    void run(replacement ? "Uploading workbook…" : "Storing source…", async () => {
      const file = fileFrom(event);
      const root = `/sheets/${sheetId}`;
      if (replacement) {
        if (!version || !file.name.toLowerCase().endsWith(".xlsx")) throw new Error("Choose an edited XLSX based on the downloaded version.");
        await api(`${root}/workbook?expected_version_id=${encodeURIComponent(version.id)}`, token, {
          method: "PUT", body: file, headers: { "Content-Type": XLSX },
        });
        try { localStorage.removeItem(downloadKey); } catch { /* Session state still updates. */ }
        setDownloadState({ key: downloadKey, version: null });
        setNotice("Edited workbook saved.");
        setReload((value) => value + 1);
      } else {
        const response = await api(`${root}/files?review=true&filename=${encodeURIComponent(file.name)}`, token, {
          method: "POST", body: file, headers: { "Content-Type": file.type },
        });
        const { file: stored } = await response.json();
        router.push(`/analysis/review/${stored.id}`);
      }
      form.reset();
    });
  }

  return (
    <>
      {error && <p className="notice error" role="alert">{error} <button className="text-button" onClick={() => { setError(""); setReload((value) => value + 1); }}>Refresh</button></p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {loading ? <p role="status">Loading financial statements…</p> : !groups.length ? (
        <section className="section">
          {<p>{error}</p>}
          {preview && <><div className="section-head"><h2>Latest financial statement</h2></div><WorkbookGrid preview /></>}
          {!error && <p className="notice">Your Google account needs access from a finance administrator.</p>}
        </section>
      ) : !sheet ? (
        <section className="section">
          <div className="section-head"><h2>{preview ? "Latest financial statement" : "Create a workbook"}</h2></div>
          {preview && <WorkbookGrid preview />}
          <p className="notice">No workbook has been created yet.</p>
          {preview ? <Link className="button primary" href="/analysis">Open analysis</Link> : (
            <form className="create-form" onSubmit={(event) => {
              event.preventDefault();
              const name = new FormData(event.currentTarget).get("name");
              void run("Creating workbook…", async () => {
                await api("/sheets", token, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ group_id: groupId, name }) });
                setReload((value) => value + 1);
              });
            }}>
              {groups.length > 1 && <label>Finance group<select value={groupId} onChange={(event) => setGroupId(event.target.value)}>{groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>}
              <label>Workbook name<input name="name" required maxLength={100} /></label>
              <button className="button primary" disabled={!!busy}>{busy || "Create workbook"}</button>
            </form>
          )}
        </section>
      ) : (
        <>
          <section className="section">
            <div className="section-head">
              <div><h2>{preview ? "Latest financial statement" : "Workbook"}</h2>{workbook && <p className="muted">{sheet.name} · Version {workbook.version}</p>}</div>
              {preview ? <Link className="button" href="/analysis">Open analysis</Link> : workbook && (
                <div className="workbook-actions">
                  <button className="button" disabled={!!busy} onClick={() => void run("Downloading…", download)}>Download Excel</button>
                  <a className="button primary" href="#update-workbook">Upload source</a>
                </div>
              )}
            </div>
            {!preview && sheets.length > 1 && <label className="sheet-select">Workbook <select value={sheetId} disabled={!!busy} onChange={(event) => { setSheetId(event.target.value); setSourceOffset(0); setNotice(""); setError(""); }}>{sheets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
            {workbook ? <WorkbookGrid key={sheetId} workbook={workbook} preview={preview} /> : !sheet.current_workbook_id ? (
              <div className="notice">
                <p>This workbook needs to be initialized.</p>
                {!preview && <button className="button primary" disabled={!!busy} onClick={() => void run("Initializing…", async () => {
                  await api(`/sheets/${sheetId}/workbook/initialize`, token, { method: "POST" });
                  setReload((value) => value + 1);
                })}>{busy || "Initialize workbook"}</button>}
              </div>
            ) : !error && <p role="status">Reading workbook…</p>}
          </section>
          {!preview && workbook && <>
            <Charts months={workbook.monthly} />
            <section className="section" id="update-workbook">
              <div className="section-head"><h2>Update the workbook</h2></div>
              <div className="actions-grid">
                <form onSubmit={(event) => upload(event, false)}>
                  <h3>Upload source</h3>
                  <p>PDF, PNG, JPEG, XLSX, CSV or WebP, up to {maxUploadMiB} MiB. Review the extracted changes before applying them.</p>
                  <input name="file" type="file" aria-label="Source document" accept=".pdf,.png,.jpeg,.jpg,.webp,.xlsx,.csv" required disabled={!!busy} />
                  <button className="button primary" disabled={!!busy}>Upload and review</button>
                </form>
                <form key={version?.id ?? "replacement"} onSubmit={(event) => upload(event, true)}>
                  <h3>Upload edited Excel</h3>
                  <p>{version ? `Upload edits based on ${version.filename} (up to ${maxUploadMiB} MiB).` : "Download the workbook first, then make your edits in Excel."}</p>
                  <input name="file" type="file" aria-label="Edited Excel workbook" accept=".xlsx" required disabled={!version || !!busy} />
                  {version && <label className="check"><input type="checkbox" required /> My edits are based on {version.filename}</label>}
                  <button className="button" disabled={!version || !!busy}>Replace workbook</button>
                </form>
              </div>
              <p role="status">{busy}</p>
            </section>
            {(sources.length > 0 || sourceOffset > 0) && <section className="section">
              <div className="section-head"><h2>Source documents</h2></div>
              <ul className="source-list">{sources.map((source) => <li key={source.id}>
                <div><strong>{source.filename}</strong><span>{source.applied_to_current_workbook ? "Applied" : source.status === "uploading" ? "Upload incomplete" : "Awaiting review"}</span></div>
                {source.status !== "uploading" && <Link href={`/analysis/review/${source.id}`}>Review</Link>}
              </li>)}</ul>
              <div className="tabs">
                {sourceOffset > 0 && <button onClick={() => setSourceOffset((offset) => offset - 100)}>Previous sources</button>}
                {sources.length === 100 && <button onClick={() => setSourceOffset((offset) => offset + 100)}>More sources</button>}
              </div>
            </section>}
          </>}
        </>
      )}
    </>
  );
}
