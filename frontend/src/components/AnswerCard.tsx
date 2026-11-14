import { useState } from "react";
import type { AskResponse } from "../api/types";
import { STATUS_TEXT, sourceSlots } from "../sources";
import { EvidencePanel } from "./evidence/EvidencePanel";

export function AnswerCard({ response: r, onEdit }: { response: AskResponse; onEdit: () => void }) {
  const status = STATUS_TEXT[r.status];
  const slots = sourceSlots(r);
  const hasEvidence = r.evidence.policy.length > 0 || r.evidence.data != null || r.evidence.fx != null;
  const caveats = r.caveats.filter((c) => !c.startsWith("Procurement policy FIN-POL-014 §5.1") && !c.startsWith("Prototype convention"));

  return (
    <div className="answer">
      <p className={`status-line ${status.tone}`}>
        <span className="status-mark" aria-hidden="true">
          {status.mark}
        </span>
        {status.text}
      </p>

      <div className="based-on">
        <p className="based-on-title" id={`${r.trace_id}-based`}>
          Based on
        </p>
        <ul className="source-slots" aria-labelledby={`${r.trace_id}-based`}>
          {slots.map((s) => (
            <li key={s.key} className={`source-slot ${s.state}`}>
              <span className="slot-name">
                <span className="slot-icon" aria-hidden="true">
                  {s.state === "verified" ? "✓" : s.state === "limited" ? "!" : ""}
                </span>
                {s.name}
              </span>
              <span className="slot-state">{s.stateText}</span>
              {s.detail && <span className="slot-detail">{s.detail}</span>}
            </li>
          ))}
        </ul>
      </div>

      {r.answer?.text && <p className="answer-text">{r.answer.text}</p>}
      {!r.answer?.text && r.error && <p className="answer-text">{r.error.message}</p>}

      {r.answer && r.answer.key_figures.length > 0 && (
        <dl className="figures">
          {r.answer.key_figures.slice(0, 3).map((f, i) => (
            <div key={`${f.label}-${i}`}>
              <dt>{f.label}</dt>
              <dd>{f.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {r.limitations.length > 0 && (
        <div className="notice caution" role="note" style={{ maxWidth: "52rem" }}>
          <p className="notice-title">{r.limitations.length === 1 ? "Limitation" : "Limitations"}</p>
          <ul>
            {r.limitations.map((l, i) => (
              <li key={`${l.kind}-${i}`}>
                {l.message}
                {l.affected_invoices.length > 0 && <AffectedList ids={l.affected_invoices} />}
                {l.recommendation && <> {l.recommendation}</>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {caveats.length > 0 && (
        <div className="caveats">
          <p>Notes</p>
          <ul>
            {caveats.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {hasEvidence && <EvidencePanel response={r} />}

      <div className="answer-actions">
        {(r.status === "QUERY_FAILED" || r.status === "NO_EVIDENCE" || r.status === "ERROR") && (
          <button className="btn small" onClick={onEdit}>
            Edit the question
          </button>
        )}
        <TraceId id={r.trace_id} />
      </div>
    </div>
  );
}

function AffectedList({ ids }: { ids: string[] }) {
  const [open, setOpen] = useState(false);
  const shown = open ? ids : ids.slice(0, 6);
  return (
    <span>
      {" "}
      Affected: <span className="num">{shown.join(", ")}</span>
      {ids.length > 6 && (
        <>
          {" "}
          <button className="link-btn" onClick={() => setOpen(!open)}>
            {open ? "Show fewer" : `Show all ${ids.length}`}
          </button>
        </>
      )}
      .
    </span>
  );
}

function TraceId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span>
      Reference for troubleshooting <span className="num">{id.slice(0, 8)}</span>{" "}
      <button
        className="link-btn"
        onClick={() => {
          void navigator.clipboard?.writeText(id).then(() => setCopied(true));
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}
