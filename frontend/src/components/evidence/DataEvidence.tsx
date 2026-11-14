import { useState } from "react";
import type { DataEvidence, ResultTable } from "../../api/types";
import { FX_STATUS_LABEL, columnLabel, formatAmount, formatDate, formatMoney, plural } from "../../format";

type Cell = string | number | boolean | null | undefined;

function renderCell(col: string, v: Cell, row: Cell[], cols: string[], money: Record<string, string>) {
  const cur = money[col];
  if (cur) {
    if (v === null || v === undefined) return { text: "unavailable", missing: true, num: true };
    const rowCur = cur === "ORIGINAL" ? (row[cols.indexOf("currency")] as string | undefined) : cur;
    return { text: formatMoney(v as string | number, rowCur ?? null), num: true };
  }
  if (v === null || v === undefined) return { text: "—", missing: false, num: false, empty: true };
  if (typeof v === "boolean") return { text: v ? "Yes" : "No" };
  if (typeof v === "number") return { text: Number.isInteger(v) ? v.toLocaleString("en-GB") : v.toLocaleString("en-GB", { maximumFractionDigits: 4 }), num: true };
  if (/date/.test(col) && /^\d{4}-\d{2}-\d{2}/.test(v)) return { text: formatDate(v) };
  return { text: v };
}

export function ResultTableView({ table, caption }: { table: ResultTable; caption: string }) {
  const money = table.money_columns ?? {};
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <caption>{caption}</caption>
        <thead>
          <tr>
            {table.columns.map((c) => (
              <th key={c} scope="col" className={money[c] ? "num" : undefined}>
                {columnLabel(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, i) => (
            <tr key={i}>
              {table.columns.map((c, j) => {
                const cell = renderCell(c, row[j] as Cell, row as Cell[], table.columns, money);
                return (
                  <td key={c} className={[cell.num ? "num" : "", cell.missing ? "missing" : ""].join(" ").trim() || undefined}>
                    {cell.text}
                  </td>
                );
              })}
            </tr>
          ))}
          {table.rows.length === 0 && (
            <tr>
              <td colSpan={table.columns.length}>No invoices match this question.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function ResultEvidence({ data }: { data: DataEvidence }) {
  const t = data.result_table;
  return (
    <>
      <p className="panel-intro">
        {data.result_description} Computed by a query on {data.row_sources.join(" and ")}; amounts shown here are the
        application's own recalculation, not the query's arithmetic.
      </p>
      <ResultTableView table={t} caption={`Result, ${plural(t.row_count, "row")}${t.truncated ? " (truncated)" : ""}`} />
      {data.original_currency_breakdown.length > 0 && (
        <div>
          <table className="data-table" style={{ maxWidth: "32rem" }}>
            <caption>The same invoices in their original currencies</caption>
            <thead>
              <tr>
                <th scope="col">Currency</th>
                <th scope="col" className="num">
                  Invoices
                </th>
                <th scope="col" className="num">
                  Amount
                </th>
              </tr>
            </thead>
            <tbody>
              {data.original_currency_breakdown.map((b) => (
                <tr key={b.currency}>
                  <th scope="row">{b.currency}</th>
                  <td className="num">{b.invoice_count}</td>
                  <td className="num">{formatAmount(b.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.group_breakdowns.length > 1 && <GroupBreakdowns data={data} />}
    </>
  );
}

function GroupBreakdowns({ data }: { data: DataEvidence }) {
  const keys = Object.keys(data.group_breakdowns[0].group);
  const currencies = [...new Set(data.group_breakdowns.flatMap((g) => g.totals.map((t) => t.currency)))].sort();
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <caption>Original currencies for each {keys.map(columnLabel).join(" and ").toLowerCase()}</caption>
        <thead>
          <tr>
            {keys.map((k) => (
              <th key={k} scope="col">
                {columnLabel(k)}
              </th>
            ))}
            {currencies.map((c) => (
              <th key={c} scope="col" className="num">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.group_breakdowns.map((g, i) => (
            <tr key={i}>
              {keys.map((k) => (
                <td key={k}>{g.group[k]}</td>
              ))}
              {currencies.map((c) => {
                const t = g.totals.find((x) => x.currency === c);
                return (
                  <td key={c} className="num">
                    {t ? formatAmount(t.amount) : "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const PAGE = 50;

export function InvoiceEvidence({ data }: { data: DataEvidence }) {
  const s = data.supporting_rows;
  const [shown, setShown] = useState(PAGE);
  const idx = (c: string) => s.columns.indexOf(c);
  const rep = s.columns.find((c) => /^amount_(eur|usd|gbp)$/.test(c));
  const repCur = rep?.slice(-3).toUpperCase();
  const cols = ["invoice_number", "invoice_date", "vendor_name", "vendor_country", "vendor_status", "description", "amount", ...(rep ? [rep, "fx_status", "fx_observation_date"] : []), "po_number", "approved_by"];
  return (
    <>
      <p className="panel-intro">
        Every invoice that contributed to the answer, from {data.row_sources.join(" and ")}. Amounts are in the invoice's
        own currency{rep ? `, with the ${repCur} amount the application calculated` : ""}. Empty PO or approver cells
        mean the field is missing in the data.
      </p>
      <div className="data-table-wrap">
        <table className="data-table">
          <caption>
            {plural(s.total_count, "invoice")}
            {s.total_count > shown ? `, showing ${shown}` : ""}
          </caption>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c} scope="col" className={c === "amount" || c === rep ? "num" : undefined}>
                  {columnLabel(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {s.rows.slice(0, shown).map((row) => {
              const fx = rep ? (row[idx("fx_status")] as string) : null;
              return (
                <tr key={row[idx("invoice_number")] as string} className={fx === "UNAVAILABLE" ? "unavailable" : undefined}>
                  {cols.map((c) => {
                    const v = row[idx(c)] as Cell;
                    if (c === "amount")
                      return (
                        <td key={c} className="num">
                          {formatMoney(v as string, row[idx("currency")] as string)}
                        </td>
                      );
                    if (c === rep)
                      return (
                        <td key={c} className={v == null ? "num missing" : "num"}>
                          {v == null ? "no rate" : formatAmount(v as string)}
                        </td>
                      );
                    if (c === "fx_status")
                      return (
                        <td key={c}>
                          <span className={`tag ${v === "FALLBACK" ? "earlier" : v === "UNAVAILABLE" ? "none" : v === "EXACT" ? "ok" : ""}`}>
                            {FX_STATUS_LABEL[v as string] ?? v}
                          </span>
                        </td>
                      );
                    if (c === "invoice_date" || c === "fx_observation_date") return <td key={c}>{formatDate(v as string)}</td>;
                    if ((c === "po_number" || c === "approved_by") && v == null)
                      return (
                        <td key={c} className="missing">
                          missing
                        </td>
                      );
                    return <td key={c}>{v == null ? "—" : String(v)}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {s.total_count > shown && s.rows.length > shown && (
        <button className="btn small" onClick={() => setShown(s.rows.length)}>
          Show all {plural(s.rows.length, "invoice")}
        </button>
      )}
      {s.truncated && <p className="panel-intro">Only the first {s.rows.length} invoices are included in this response.</p>}
    </>
  );
}
