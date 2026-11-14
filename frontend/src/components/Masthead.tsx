import type { SummaryState } from "../state/useSummary";
import { formatDate } from "../format";

export function Masthead({ summary }: { summary: SummaryState }) {
  return (
    <header className="masthead">
      <h1>Procurement spend questions</h1>
      <div className="sources-loaded" aria-live="polite">
        {summary.status === "loading" && <span>Loading the data sources…</span>}
        {summary.status === "error" && <span>Data sources could not be checked.</span>}
        {summary.status === "ready" && <SourcesLoaded data={summary.data} />}
      </div>
    </header>
  );
}

function SourcesLoaded({ data }: { data: Extract<SummaryState, { status: "ready" }>["data"] }) {
  const [from, to] = data.dataset.date_range;
  const fx = Object.values(data.fx_status);
  const fxOk = fx.every((s) => s.state === "ok");
  const latest = fx.map((s) => s.last_observation).filter(Boolean).sort()[0];
  return (
    <>
      <span>
        <strong>{data.dataset.invoice_count} invoices</strong>, {formatDate(from)} to {formatDate(to)}
      </span>
      <span>
        <strong>Policy</strong> {data.dataset.policy_document}
      </span>
      <span>
        <strong>ECB reference rates</strong>{" "}
        {fxOk ? `loaded${latest ? ` up to ${formatDate(latest)}` : ""}` : "not available, EUR consolidation is off"}
      </span>
    </>
  );
}
