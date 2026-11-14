import { useEffect, useState } from "react";
import type { Turn } from "../state/chatReducer";
import { AnswerCard } from "./AnswerCard";

type Props = { turn: Turn; onRetry: () => void; onCancel: () => void; onEdit: () => void };

export function TurnView({ turn, onRetry, onCancel, onEdit }: Props) {
  return (
    <article className="turn" aria-labelledby={`${turn.id}-q`}>
      <h2 className="question" id={`${turn.id}-q`}>
        {turn.question}
      </h2>
      <p className="question-meta">Asked at {new Date(turn.askedAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</p>
      {turn.state === "pending" && <Pending since={turn.askedAt} onCancel={onCancel} />}
      {turn.state === "failed" && turn.error && (
        <div className="notice fault" role="alert" style={{ maxWidth: "48rem", marginTop: "1rem" }}>
          <p className="notice-title">{turn.error.kind === "cancelled" ? "Cancelled" : "No answer"}</p>
          <p>{turn.error.message}</p>
          <p style={{ display: "flex", gap: "0.75rem", marginTop: "0.75rem" }}>
            <button className="btn small" onClick={onRetry}>
              Ask again
            </button>
            <button className="btn small" onClick={onEdit}>
              Edit the question
            </button>
          </p>
        </div>
      )}
      {turn.state === "done" && turn.response && <AnswerCard response={turn.response} onEdit={onEdit} />}
    </article>
  );
}

// The server does not stream progress, so these are the usual stages shown against elapsed time,
// not confirmed steps: nothing is ticked off as "done".
const STEPS = [
  { at: 0, text: "Choosing which sources can answer this" },
  { at: 5, text: "Reading the policy and querying the records" },
  { at: 15, text: "Recalculating the figures and checking rates" },
  { at: 25, text: "Writing the answer" },
];

function Pending({ since, onCancel }: { since: number; onCancel: () => void }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.floor((now - since) / 1000));
  const current = STEPS.reduce((acc, s, i) => (secs >= s.at ? i : acc), 0);
  return (
    <div className="pending" role="status" aria-live="off">
      <p className="notice-title" style={{ fontWeight: 800 }}>
        Working on it
      </p>
      <p className="panel-intro" style={{ marginTop: "0.25rem" }}>
        An answer usually goes through these stages:
      </p>
      <ol className="pending-steps">
        {STEPS.map((s, i) => (
          <li key={s.text} className={i === current ? "current" : undefined} aria-current={i === current ? "step" : undefined}>
            <span aria-hidden="true">{i === current ? "›" : "·"}</span>
            {s.text}
          </li>
        ))}
      </ol>
      <div className="progress-bar" aria-hidden="true">
        <span />
      </div>
      <div className="pending-foot">
        <span className="num">{secs} s</span>
        <span>Answers usually take 20 to 60 seconds.</span>
        <button className="btn small" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
