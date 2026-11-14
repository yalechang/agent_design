import { useId, useRef, useState, type KeyboardEvent } from "react";
import type { AskResponse } from "../../api/types";
import { CheckEvidence } from "./CheckEvidence";
import { InvoiceEvidence, ResultEvidence } from "./DataEvidence";
import { PolicyEvidenceList } from "./PolicyEvidence";
import { RateEvidence } from "./RateEvidence";

type Tab = { key: string; label: string; count?: number };

export function EvidencePanel({ response: r }: { response: AskResponse }) {
  const d = r.evidence.data;
  const fx = r.evidence.fx;
  const tabs: Tab[] = [];
  if (d) tabs.push({ key: "result", label: "Result" });
  if (r.evidence.policy.length) tabs.push({ key: "policy", label: "Policy clauses", count: r.evidence.policy.length });
  if (d && d.supporting_rows.total_count) tabs.push({ key: "invoices", label: "Invoices", count: d.supporting_rows.total_count });
  if (fx && (fx.refs.length || fx.unavailable_invoices.length)) tabs.push({ key: "rates", label: "Exchange rates", count: fx.refs.length });
  tabs.push({ key: "check", label: "How it was checked" });

  const [active, setActive] = useState(tabs[0].key);
  const base = useId();
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const onKey = (e: KeyboardEvent, i: number) => {
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    e.preventDefault();
    const t = tabs[(next + tabs.length) % tabs.length];
    setActive(t.key);
    refs.current[t.key]?.focus();
  };

  return (
    <section className="evidence" aria-label="Evidence behind this answer">
      <h3 className="evidence-title">Evidence</h3>
      <div className="tabs" role="tablist" aria-label="Evidence type">
        {tabs.map((t, i) => (
          <button
            key={t.key}
            ref={(el) => {
              refs.current[t.key] = el;
            }}
            role="tab"
            id={`${base}-tab-${t.key}`}
            aria-controls={`${base}-panel-${t.key}`}
            aria-selected={active === t.key}
            tabIndex={active === t.key ? 0 : -1}
            className="tab"
            onClick={() => setActive(t.key)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {t.label}
            {t.count !== undefined && <span className="count"> ({t.count})</span>}
          </button>
        ))}
      </div>
      <div className="tab-panel" role="tabpanel" id={`${base}-panel-${active}`} aria-labelledby={`${base}-tab-${active}`} tabIndex={0}>
        {active === "result" && d && <ResultEvidence data={d} />}
        {active === "policy" && <PolicyEvidenceList items={r.evidence.policy} />}
        {active === "invoices" && d && <InvoiceEvidence data={d} />}
        {active === "rates" && fx && <RateEvidence fx={fx} />}
        {active === "check" && <CheckEvidence response={r} />}
      </div>
    </section>
  );
}
