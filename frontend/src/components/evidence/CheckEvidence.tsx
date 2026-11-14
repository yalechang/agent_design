import type { AskResponse } from "../../api/types";
import { columnLabel } from "../../format";

const CHECK_TEXT = { verified: "Matched", unverified: "Not recalculated", unavailable: "Unavailable" } as const;
const SOURCE_TEXT: Record<string, string> = { policy: "procurement policy", records: "invoice and vendor records", fx: "ECB euro reference rates" };

export function CheckEvidence({ response: r }: { response: AskResponse }) {
  const d = r.evidence.data;
  const route = r.route;
  return (
    <>
      {route && (
        <div>
          <h4>Why these sources</h4>
          <p className="panel-intro" style={{ marginTop: "0.25rem" }}>
            {route.in_scope
              ? `Planned to use the ${route.sources.map((s) => SOURCE_TEXT[s]).join(", ")}. `
              : "No available source covers this question. "}
            {route.rationale}
            {route.original_currency_filter && ` Only invoices issued in ${route.original_currency_filter} are included.`}
            {route.in_scope && route.sources.includes("fx") && ` Amounts are reported in ${route.reporting_currency}.`}
          </p>
          {route.rules_applied.length > 0 && (
            <ul className="panel-intro" style={{ marginTop: "0.25rem" }}>
              {route.rules_applied.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {d && d.aggregations.length > 0 && (
        <div>
          <h4>Figures recalculated by the application</h4>
          <ul className="check-list" style={{ marginTop: "0.5rem" }}>
            {d.aggregations.map((a) => (
              <li key={a.column}>
                <span>
                  <span className={`tag ${a.status === "verified" ? "ok" : a.status === "unavailable" ? "none" : "earlier"}`}>
                    {CHECK_TEXT[a.status]}
                  </span>
                </span>
                <span>
                  <strong>{columnLabel(a.column)}</strong>: {a.agg} of {columnLabel(a.source_column).toLowerCase()}
                  {a.group_by.length > 0 && ` per ${a.group_by.map((g) => columnLabel(g).toLowerCase()).join(" and ")}`}. {a.detail}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {d && d.notes.length > 0 && (
        <div>
          <h4>Query notes</h4>
          <ul className="panel-intro" style={{ marginTop: "0.25rem" }}>
            {d.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      )}

      {d && (
        <details className="disclosure">
          <summary>Show the generated query</summary>
          <pre className="code" tabIndex={0} aria-label="Generated pandas query">
            <code>{d.code.trim()}</code>
          </pre>
        </details>
      )}

      <p className="panel-intro">
        The full decision trail for this answer is stored under reference <span className="num">{r.trace_id}</span>.
      </p>
    </>
  );
}
