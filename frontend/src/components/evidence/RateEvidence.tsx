import type { FxEvidenceBlock, FxRef } from "../../api/types";
import { formatDate, formatMoney, plural } from "../../format";

const leg = (r: FxRef, cur: string) => (r.legs as Array<{ currency: string; value: string | null }>).find((l) => l.currency === cur)?.value;

export function RateEvidence({ fx }: { fx: FxEvidenceBlock }) {
  const formulas = [...new Set(fx.refs.map((r) => r.formula))];
  const invoiceRefs = fx.refs.filter((r) => r.amount == null);
  const amountRefs = fx.refs.filter((r) => r.amount != null);
  const earlier = fx.refs.filter((r) => r.status === "FALLBACK").reduce((n, r) => n + (r.amount != null ? 1 : r.invoice_numbers.length), 0);
  return (
    <>
      <div className="basis-note" role="note">
        <strong>ECB euro reference rates, as policy §5.1 requires</strong>
        {fx.policy_note} Rates are quoted as units of the currency per 1 EUR.
      </div>
      {earlier > 0 && (
        <div className="disclosure-banner" role="note">
          <strong>
            {plural(earlier, "conversion")} used an earlier date because the ECB published no rate on the invoice
            date
          </strong>
          {fx.convention_note}
        </div>
      )}

      {amountRefs.length > 0 && (
        <div className="data-table-wrap">
          <table className="data-table">
            <caption>Conversions of amounts named in the question</caption>
            <thead>
              <tr>
                <th scope="col" className="num">Amount</th>
                <th scope="col">Requested date</th>
                <th scope="col">ECB rate date</th>
                <th scope="col" className="num">USD per EUR</th>
                <th scope="col" className="num">GBP per EUR</th>
                <th scope="col" className="num">Result</th>
              </tr>
            </thead>
            <tbody>
              {amountRefs.map((r, i) => (
                <tr key={i}>
                  <td className="num">{formatMoney(r.amount, r.from_currency)}</td>
                  <td>{formatDate(r.requested_date)}</td>
                  <td>
                    {r.observation_date ? formatDate(r.observation_date) : "none"} <DateTag r={r} />
                  </td>
                  <td className="num">{leg(r, "USD") ?? "—"}</td>
                  <td className="num">{leg(r, "GBP") ?? "—"}</td>
                  <td className={r.result ? "num" : "num missing"}>{r.result ? formatMoney(r.result, r.to_currency) : "no rate"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {invoiceRefs.length > 0 && (
        <div className="data-table-wrap">
          <table className="data-table">
            <caption>
              ECB reference rates used for {plural(fx.coverage.EXACT + fx.coverage.FALLBACK, "invoice")}
              {fx.coverage.IDENTITY ? `; ${plural(fx.coverage.IDENTITY, "invoice")} already in ${fx.reporting_currency} needed no rate` : ""}
            </caption>
            <thead>
              <tr>
                <th scope="col">Invoice date</th>
                <th scope="col">ECB rate date</th>
                <th scope="col">Why this date</th>
                <th scope="col" className="num">USD per EUR</th>
                <th scope="col" className="num">GBP per EUR</th>
                <th scope="col">Invoices</th>
              </tr>
            </thead>
            <tbody>
              {invoiceRefs.map((r) => (
                <tr key={`${r.fx_ref_id ?? r.invoice_numbers[0]}-${r.requested_date}`} className={r.status === "UNAVAILABLE" ? "unavailable" : undefined}>
                  <td>{formatDate(r.requested_date)}</td>
                  <td>{r.observation_date ? formatDate(r.observation_date) : "none"}</td>
                  <td>
                    <DateTag r={r} />
                    {r.status === "UNAVAILABLE" && <span> {r.missing_evidence.join("; ")}</span>}
                  </td>
                  <td className="num">{leg(r, "USD") ?? "—"}</td>
                  <td className="num">{leg(r, "GBP") ?? "—"}</td>
                  <td>{r.invoice_numbers.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <dl className="method">
        <dt>Conversion</dt>
        <dd>
          {formulas.join(". ")}
          {formulas.length ? ". " : ""}
          {fx.method}
        </dd>

        {fx.exact_date && (
          <>
            <dt>Exact dates only</dt>
            <dd>You asked for exact invoice-date rates, so no earlier date was used.</dd>
          </>
        )}
        <dt>Source</dt>
        <dd>
          <ul style={{ margin: 0, paddingLeft: "1.25rem" }}>
            {fx.sources.map((s) => (
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
        </dd>
      </dl>
    </>
  );
}

function DateTag({ r }: { r: FxRef }) {
  if (r.status === "EXACT") return <span className="tag ok">Invoice date</span>;
  if (r.status === "FALLBACK")
    return <span className="tag earlier">No ECB rate that day, {plural(r.fallback_days, "day")} earlier</span>;
  if (r.status === "UNAVAILABLE") return <span className="tag none">No acceptable rate</span>;
  return null;
}
