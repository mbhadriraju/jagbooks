"use client";

import { useState } from "react";
import { currency, money } from "./session";

type Cell = string | number | boolean | null;
type Metric = "revenue_cents" | "expense_cents" | "net_cents" | "cash_flow_cents";
export type Month = { month: string } & Record<Metric, number | null>;
export type Workbook = { version: number; monthly: Month[]; tabs: { name: string; rows: Cell[][]; computed: Record<string, number> }[] };

export function WorkbookGrid({ workbook, preview }: { workbook?: Workbook; preview?: boolean }) {
  const [tab, setTab] = useState(0);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState("");
  const sheet = workbook?.tabs[tab];
  const columns = sheet?.rows[0] ?? ["Income Statement", "Aug-26", "Sep-26", "Oct-26", "Nov-26", "Dec-26", "Jan-27", "Feb-27", "Mar-27", "Apr-27", "May-27", "Cumulative"];
  const placeholders = ["Revenue", "Merchandise Sales", "Student Dues", "Fundraising Revenue", "Total Revenue", "Expenses", "Tournament Fees", "Total Expenses", "Net Income"];
  const rows = sheet?.rows.slice(1) ?? placeholders.map((label) => [label, ...columns.slice(1).map(() => null)]);
  const visible = rows
    .map((row, index) => ({ row, rowNumber: index + 2 }))
    .filter(({ row }) => row[0] && String(row[0]).toLowerCase().includes(filter.trim().toLowerCase()));
  return (
    <div className="workbook">
      {workbook && (
        <div className="sheet-tools">
          <div className="tabs" aria-label="Workbook tabs">
            {workbook.tabs.map((item, index) => (
              <button key={item.name} aria-pressed={index === tab} onClick={() => { setTab(index); setSelected(""); setFilter(""); }}>{item.name}</button>
            ))}
          </div>
          {!preview && <input aria-label="Filter workbook rows" placeholder="Filter rows" value={filter} onChange={(event) => setFilter(event.target.value)} />}
        </div>
      )}
      {selected && <p className="formula" role="status">{selected}</p>}
      <div className="table-scroll" tabIndex={0} aria-label={sheet?.name ?? "Statement layout preview"}>
        <table className="sheet">
          <thead><tr>{columns.map((cell, index) => <th key={index} scope="col">{String(cell ?? "")}</th>)}</tr></thead>
          <tbody>
            {visible.map(({ row, rowNumber }) => (
              <tr key={rowNumber} className={/^(Total |Net |Cumulative )/.test(String(row[0])) ? "total" : ""}>
                <th scope="row">{String(row[0])}</th>
                {row.slice(1).map((cell, index) => {
                  const address = String.fromCharCode(66 + index) + rowNumber;
                  const value = sheet?.computed[address];
                  return (
                  <td key={index}>
                    {typeof cell === "number" ? currency.format(cell) : typeof cell === "string" && cell.startsWith("=") ?
                      <button className="formula-button" aria-label={`Formula for ${row[0]}, ${columns[index + 1]}`} onClick={() => setSelected(`${row[0]} · ${columns[index + 1]}  ${cell}`)}>{value === undefined ? "ƒx" : money(value)}</button> : String(cell ?? "")}
                  </td>
                  );
                })}
              </tr>
            ))}
            {!visible.length && <tr><td colSpan={columns.length}>No matching rows.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const metrics: { key: Metric; label: string }[] = [
  { key: "revenue_cents", label: "Revenue" }, { key: "expense_cents", label: "Expenses" },
  { key: "net_cents", label: "Net income" }, { key: "cash_flow_cents", label: "Net cash flow" },
];
const monthLabel = (value: string) => new Intl.DateTimeFormat("en-US", {
  month: "short", year: "2-digit", timeZone: "UTC",
}).format(new Date(value + "-01T00:00:00Z"));

export function Charts({ months }: { months: Month[] }) {
  const [metric, setMetric] = useState<Metric>("net_cents");
  const [month, setMonth] = useState("");
  const selected = months.find((item) => item.month === month);
  const values = months.map((item) => item[metric] ?? 0);
  const high = Math.max(0, ...values);
  const low = Math.min(0, ...values);
  const span = high - low || 1;
  const zero = high === low ? 50 : (high / span) * 100;
  const totals = metrics.slice(0, 3).map(({ key, label }) => ({
    label, value: months.reduce((total, item) => total + (item[key] ?? 0), 0),
  }));
  const label = metrics.find((item) => item.key === metric)?.label;
  return (
    <section className="section chart-section" aria-labelledby="chart-heading">
      <div className="section-head"><h2 id="chart-heading">Monthly performance</h2></div>
      <dl className="totals" aria-label="Fiscal year operating totals">
        {totals.map(({ label, value }) => <div key={label}><dt>{label}</dt><dd>{money(value)}</dd></div>)}
      </dl>
      <div className="chart">
        <div className="tabs" aria-label="Chart metric">
          {metrics.map(({ key, label }) => <button key={key} aria-pressed={metric === key} onClick={() => setMetric(key)}>{label}</button>)}
        </div>
        <div className="plot-scroll" tabIndex={0} aria-label="Monthly chart">
          <div className="plot">
            <div className="axis" aria-hidden="true"><span>{money(high)}</span>{low < 0 && <span>{money(low)}</span>}</div>
            <div className="bars">
              <i aria-hidden="true" className="zero" style={{ top: `calc((100% - 30px) * ${zero / 100})` }} />
              {months.map((item) => {
                const value = item[metric];
                const height = Math.abs(value ?? 0) / span * 100;
                const top = value !== null && value > 0 ? zero - height : zero;
                const formatted = value === null ? "Unavailable" : money(value);
                return <button key={item.month} aria-pressed={selected?.month === item.month} aria-label={`${monthLabel(item.month)}: ${formatted}`} title={formatted} onClick={() => setMonth(item.month)}>
                  <span className="bar-space"><i className={value === null ? "missing" : ""} style={{ height: `${height}%`, top: `${top}%` }} /></span>
                  <span>{monthLabel(item.month)}</span>
                </button>;
              })}
            </div>
          </div>
        </div>
        <p className="chart-caption" aria-live="polite">{selected ? `${monthLabel(selected.month)} · ${label} · ${selected[metric] === null ? "Unavailable" : money(selected[metric])}` : "Select a month for its exact amount."}</p>
        {metric === "cash_flow_cents" && <p className="chart-caption">Uses the workbook’s cash flow inputs, including linked bank deposits. Changed formulas may require Excel.</p>}
      </div>
    </section>
  );
}
