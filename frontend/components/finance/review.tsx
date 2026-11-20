"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, LoginNotice, money, useSession } from "./session";

type Review = {
  file: { id: string; filename: string };
  version_id: string;
  applied: boolean;
  blocked: string | null;
  changes: { sheet: string; cell: string; category: string; month: string; before_cents: number; delta_cents: number; after_cents: number }[];
  rows: { date: string | null; description: string | null; category: string | null; amount_cents: number | null }[];
};

export function SourceReview({ id }: { id: string }) {
  const session = useSession();
  return (
    <>
      <Link href="/analysis" className="back">Back to analysis</Link>
      <h1 className="page-title">Review source changes</h1>
      {session ? <ReviewPanel key={session.user.id + id} id={id} token={session.access_token} /> :
        <div className="notice">{session === undefined ? "Checking your session…" : <LoginNotice />}</div>}
    </>
  );
}

function ReviewPanel({ id, token }: { id: string; token: string }) {
  const [review, setReview] = useState<Review>();
  const [error, setError] = useState("");
  const [result, setResult] = useState<number>();
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState("");
  const root = `/files/${id}`;

  useEffect(() => {
    const request = new AbortController();
    api(root + "/review", token, { method: "POST", signal: request.signal })
      .then((response) => response.json())
      .then((data: Review) => { if (!request.signal.aborted) setReview(data); })
      .catch((problem: Error) => { if (!request.signal.aborted) setError(problem.message); });
    return () => request.abort();
  }, [root, token, revision]);

  async function apply() {
    if (!review) return;
    setBusy("Applying changes…");
    setError("");
    try {
      const response = await api(root + "/review", token, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_version_id: review.version_id }),
      });
      setResult((await response.json()).version);
    } catch (problem) { setError((problem as Error).message); }
    finally { setBusy(""); }
  }

  async function openSource() {
    setError("");
    try {
      const { url } = await (await api(root + "/download", token)).json();
      if (new URL(url).protocol !== "https:") throw new Error("Source link is unavailable.");
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.click();
    } catch (problem) { setError((problem as Error).message); }
  }

  return (
    <>
      {error && <p role="alert" className="notice error">{error} <button className="text-button" onClick={() => { setError(""); setReview(undefined); setRevision((value) => value + 1); }}>Refresh review</button></p>}
      {result !== undefined && <p className="notice" role="status">Published to workbook version {result}. <Link href="/analysis">Open analysis</Link></p>}
      {!review && !error && <p role="status">Reading source and preparing changes…</p>}
      {review && <>
        <section className="section">
          <div className="section-head"><div><h2>Proposed workbook changes</h2><p className="muted">{review.file.filename}</p></div><button className="button" onClick={() => void openSource()}>Open original</button></div>
          {review.applied ? <p className="notice">This source is already included in the current workbook.</p> :
            review.blocked ? <p className="notice error" role="alert">{review.blocked}</p> :
              review.changes.length ? (
                <div className="table-scroll" tabIndex={0} aria-label="Proposed changes">
                  <table className="review-table">
                    <thead><tr>{["Statement", "Category", "Month", "Current", "Change", "Proposed"].map((name) => <th scope="col" key={name}>{name}</th>)}</tr></thead>
                    <tbody>{review.changes.map((change) => <tr key={change.sheet + change.cell}>
                      <td>{change.sheet}</td><th scope="row">{change.category} <span className="muted">{change.cell}</span></th><td>{change.month}</td>
                      <td>{money(change.before_cents)}</td><td>{change.delta_cents > 0 ? "+" : ""}{money(change.delta_cents)}</td><td>{money(change.after_cents)}</td>
                    </tr>)}</tbody>
                  </table>
                </div>
              ) : <p>No workbook amounts would change.</p>}
          {!review.applied && !review.blocked && review.changes.length > 0 && result === undefined && (
            <div className="review-actions"><Link className="button" href="/analysis">Back to analysis</Link><button className="button primary" disabled={!!busy || !!error} onClick={() => void apply()}>{busy || "Apply changes"}</button></div>
          )}
        </section>
        <section className="section">
          <div className="section-head"><div><h2>Extracted transactions</h2><p className="muted">AI-generated draft. Check against the original source.</p></div></div>
          <div className="table-scroll" tabIndex={0} aria-label="Extracted transactions">
            <table className="review-table">
              <thead><tr>{["Date", "Description", "Category", "Amount"].map((name) => <th scope="col" key={name}>{name}</th>)}</tr></thead>
              <tbody>{review.rows.map((row, index) => <tr key={index}><td>{row.date ?? "Missing"}</td><th scope="row">{row.description ?? "Missing"}</th><td>{row.category?.replaceAll("_", " ") ?? "Missing"}</td><td>{row.amount_cents === null ? "Missing" : money(row.amount_cents)}</td></tr>)}
              {!review.rows.length && <tr><td colSpan={4}>No transactions detected.</td></tr>}</tbody>
            </table>
          </div>
        </section>
      </>}
    </>
  );
}
