import type { PolicyEvidence } from "../../api/types";

export function PolicyEvidenceList({ items }: { items: PolicyEvidence[] }) {
  return (
    <>
      <p className="panel-intro">
        Quoted from {items[0]?.document}. Each quote was checked word for word against the policy file before it
        was shown.
      </p>
      {items.map((c) => (
        <figure className="clause" key={`${c.clause_id}-${c.quote}`} style={{ margin: 0 }}>
          <figcaption className="clause-ref">
            {c.kind === "table_row" ? `Table row ${c.clause_id}` : `Clause ${c.clause_id}`}{" "}
            <span className="section">{c.section}</span>
          </figcaption>
          <blockquote>{c.kind === "table_row" ? tableRowText(c.clause_text, c.table_header) : c.quote}</blockquote>
          {c.kind !== "table_row" && normalise(c.quote) !== normalise(c.clause_text) && (
            <p className="clause-full">Full clause: {stripMarkdown(c.clause_text)}</p>
          )}
        </figure>
      ))}
    </>
  );
}

const cells = (row: string) =>
  row
    .split("|")
    .map((s) => s.trim())
    .filter(Boolean);

function tableRowText(row: string, header?: string | null) {
  const values = cells(row);
  const heads = header ? cells(header) : [];
  return values.map((v, i) => (heads[i] ? `${heads[i]}: ${v}` : v)).join("; ");
}

const stripMarkdown = (s: string) => s.replace(/\*\*|`/g, "");
const normalise = (s: string) => stripMarkdown(s).replace(/\s+/g, " ").trim();
