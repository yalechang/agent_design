import type { SummaryResponse } from "../api/types";
import type { SummaryState } from "../state/useSummary";
import { formatAmount, formatDate, formatMoney, plural } from "../format";

export const EXAMPLE_QUESTIONS = [
  "What are our top 5 vendors by total spend in 2025?",
  "What is our total spend with vendors based in Spain?",
  "Who is allowed to approve an invoice of EUR 60,000?",
  "Which invoices above the Finance Director threshold have no approver recorded?",
  "What is the total value of invoices originally issued in GBP?",
];

type Props = { state: SummaryState; onRetry: () => void; onAsk: (q: string) => void };

export function SummaryPanel({ state, onRetry, onAsk }: Props) {
  if (state.status === "loading") {
    return (
      <section aria-busy="true" aria-labelledby="overview-h">
        <h2 id="overview-h">2025 spend</h2>
        <p className="scope">Loading the invoice overview…</p>
        <div className="skeleton-line wide" />
        <div className="skeleton-line" />
        <div className="skeleton-line" />
        <div className="skeleton-line" />
        <Examples onAsk={onAsk} />
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <section aria-labelledby="overview-h">
        <h2 id="overview-h">2025 spend</h2>
        <div className="notice fault" role="alert">
          <p className="notice-title">The overview did not load</p>
          <p>{state.message}</p>
          <p style={{ marginTop: "0.75rem" }}>
            <button className="btn small" onClick={onRetry}>
              Load the overview again
            </button>
          </p>
        </div>
        <Examples onAsk={onAsk} />
      </section>
    );
  }
  const d = state.data;
  return (
    <>
      <Consolidation data={d} />
      <StatusBreakdown data={d} />
      <TopVendors data={d} />
      <DataQuality data={d} />
      <Examples onAsk={onAsk} />
    </>
  );
}

function Consolidation({ data }: { data: SummaryResponse }) {
  const c = data.consolidated;
  const [from, to] = data.dataset.date_range;
  return (
    <section aria-labelledby="overview-h">
      <h2 id="overview-h">2025 spend</h2>
      <p className="scope">
        {plural(data.dataset.invoice_count, "invoice")} from {plural(data.dataset.vendor_count, "vendor")},{" "}
        {formatDate(from)} to {formatDate(to)}. Totals include credit notes.
      </p>

      {c.available ? (
        <table className="ledger">
          <caption className="visually-hidden">Spend by original currency, converted to EUR</caption>
          <thead>
            <tr>
              <th scope="col">Invoiced in</th>
              <th scope="col" className="num">
                Original amount
              </th>
              <th scope="col" className="num">
                In EUR
              </th>
            </tr>
          </thead>
          <tbody>
            {c.by_currency.map((l) => (
              <tr key={l.currency}>
                <th scope="row">
                  {l.currency}
                  <span className="sub">{plural(l.invoice_count, "invoice")}</span>
                </th>
                <td className="num">{formatAmount(l.original_amount)}</td>
                <td className="num">{formatAmount(l.converted_amount)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">Total</th>
              <td />
              <td className="num">{formatMoney(c.total, "EUR")}</td>
            </tr>
          </tfoot>
        </table>
      ) : (
        <>
          <table className="ledger">
            <caption className="visually-hidden">Spend by original currency</caption>
            <thead>
              <tr>
                <th scope="col">Invoiced in</th>
                <th scope="col" className="num">
                  Original amount
                </th>
              </tr>
            </thead>
            <tbody>
              {data.original_currency_totals.map((t) => (
                <tr key={t.currency}>
                  <th scope="row">
                    {t.currency}
                    <span className="sub">{plural(t.invoice_count, "invoice")}</span>
                  </th>
                  <td className="num">{formatAmount(t.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="notice caution" role="status">
            <p className="notice-title">No EUR total is shown</p>
            <p>{c.unavailable_reason}</p>
            {c.missing.length > 0 && (
              <p>
                {plural(c.missing.length, "invoice")} could not be converted. Adding up the others would understate
                spend, so the total is left out.
              </p>
            )}
            {c.recommendation && <p>{c.recommendation}</p>}
          </div>
        </>
      )}

      {c.available && c.evidence && (
        <div className="basis">
          <p>Converted at ECB euro reference rates for each invoice date, as the policy (§5.1) requires.</p>
          {c.evidence.fallback_invoices.length > 0 && (
            <p>
              {plural(c.evidence.fallback_invoices.length, "invoice")} fell on a weekend or TARGET holiday, when the
              ECB publishes no rate, and used the latest earlier rate (at most{" "}
              {plural(c.evidence.max_fallback_days, "day")} earlier). The policy does not cover these dates, so review
              them if exact compliance matters.
            </p>
          )}
          <details className="disclosure">
            <summary>Rate sources and method</summary>
            <ul>
              {c.evidence.sources.map((s) => (
                <li key={s.currency}>
                  <a href={s.page ?? s.url} target="_blank" rel="noreferrer">
                    {s.series}
                  </a>
                  {s.release && `, ${s.release}`}
                  {s.snapshot_id && (
                    <>
                      , snapshot <span className="num">{s.snapshot_id}</span>
                    </>
                  )}
                </li>
              ))}
            </ul>
            <p style={{ marginTop: "0.5rem" }}>{c.evidence.formula.join(". ")}.</p>
            <p style={{ marginTop: "0.5rem" }}>{c.evidence.method}</p>
            <p style={{ marginTop: "0.5rem" }}>{c.evidence.convention_note}</p>
          </details>
        </div>
      )}
    </section>
  );
}

const STATUS_LABEL: Record<string, string> = { ACTIVE: "Active", ON_HOLD: "On hold", INACTIVE: "Inactive" };

function StatusBreakdown({ data }: { data: SummaryResponse }) {
  return (
    <section aria-labelledby="status-h">
      <h2 id="status-h">By vendor status</h2>
      <table className="ledger">
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col" className="num">
              Vendors
            </th>
            <th scope="col" className="num">
              Invoices
            </th>
            <th scope="col" className="num">
              EUR
            </th>
          </tr>
        </thead>
        <tbody>
          {data.by_vendor_status.map((s) => (
            <tr key={s.status}>
              <th scope="row">{STATUS_LABEL[s.status] ?? s.status}</th>
              <td className="num">{s.vendor_count}</td>
              <td className="num">{s.invoice_count}</td>
              <td className="num">{s.eur_available ? formatAmount(s.eur_total) : "unavailable"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function TopVendors({ data }: { data: SummaryResponse }) {
  return (
    <section aria-labelledby="top-h">
      <h2 id="top-h">Largest vendors</h2>
      {data.top_vendors_eur ? (
        <table className="ledger">
          <thead>
            <tr>
              <th scope="col">Vendor</th>
              <th scope="col" className="num">
                EUR
              </th>
            </tr>
          </thead>
          <tbody>
            {data.top_vendors_eur.map((v) => (
              <tr key={v.vendor_id}>
                <th scope="row">
                  {v.vendor_name}
                  <span className="sub">
                    {v.country}, {STATUS_LABEL[v.status]?.toLowerCase() ?? v.status},{" "}
                    {plural(v.invoice_count, "invoice")}
                  </span>
                </th>
                <td className="num">{formatAmount(v.eur_total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="notice quiet">{data.top_vendors_unavailable_reason}</p>
      )}
    </section>
  );
}

const QUALITY_TEXT: Record<string, (n: number) => string> = {
  MISSING_PO: (n) => `${n === 1 ? "invoice has" : "invoices have"} no PO number`,
  MISSING_APPROVER: (n) => `${n === 1 ? "invoice has" : "invoices have"} no approver recorded`,
  CREDIT_NOTE: (n) => `${n === 1 ? "credit note is" : "credit notes are"} included in the totals`,
  NON_ACTIVE_VENDOR: (n) => `${n === 1 ? "invoice is" : "invoices are"} from vendors on hold or inactive`,
  VENDOR_NAME_MISMATCH: (n) => `${n === 1 ? "invoice names" : "invoices name"} the vendor differently from the vendor master`,
};

function DataQuality({ data }: { data: SummaryResponse }) {
  if (data.data_quality.length === 0) return null;
  return (
    <section aria-labelledby="dq-h">
      <h2 id="dq-h">Worth checking</h2>
      <ul className="flag-list">
        {data.data_quality.map((q) => (
          <li key={q.code}>
            <span className="num">{q.count}</span>
            <span>{QUALITY_TEXT[q.code]?.(q.count) ?? q.message}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Examples({ onAsk }: { onAsk: (q: string) => void }) {
  return (
    <section aria-labelledby="examples-h">
      <h2 id="examples-h">Try asking</h2>
      <ul className="examples">
        {EXAMPLE_QUESTIONS.map((q) => (
          <li key={q}>
            <button className="example-btn" onClick={() => onAsk(q)}>
              {q}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
