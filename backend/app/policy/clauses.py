"""Split procurement_policy.md into clause-addressable, verbatim chunks.

The source file is only read, never modified. Every clause keeps its exact text and the
character span it came from, so a citation can always be traced back to the source.

Clause ids:
  "2.1", "3.1", ...   numbered paragraphs
  "1"                 an unnumbered paragraph directly under section 1 (intro text)
  "3.T"               the whole table in section 3
  "3.T1", "3.T2" ...  individual table data rows (header row kept in ``table_header``)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^(\d+\.\d+)\s+")
_META_RE = re.compile(r"\*\*(.+?):\*\*\s*(.+?)\s*(?=·|$)")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}")


@dataclass(frozen=True)
class Clause:
    id: str
    section_number: str
    section_title: str
    kind: str                 # "paragraph" | "intro" | "table" | "table_row"
    text: str                 # verbatim slice of the source document
    start: int
    end: int
    table_header: str | None = None


@dataclass
class PolicyDocument:
    title: str
    metadata: dict[str, str]
    clauses: list[Clause]
    source_text: str
    sha256: str
    path: Path
    _by_id: dict[str, Clause] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_id = {c.id: c for c in self.clauses}

    def get(self, clause_id: str) -> Clause | None:
        return self._by_id.get(clause_id)

    @property
    def doc_label(self) -> str:
        doc_id = self.metadata.get("Document ID", "policy")
        version = self.metadata.get("Version")
        return f"{doc_id} v{version}" if version else doc_id

    def quote_in_clause(self, clause_id: str, quote: str) -> bool:
        """True if ``quote`` appears verbatim in the clause (ignoring whitespace and markdown markers)."""
        clause = self.get(clause_id)
        q = _normalize_for_match(quote)
        return bool(clause and q and q in _normalize_for_match(clause.text))


def normalize_ws(s: str) -> str:
    return " ".join(s.split())


def _normalize_for_match(s: str) -> str:
    """Whitespace-normalise and drop markdown emphasis/code markers, which quotes usually omit."""
    return normalize_ws(s.replace("**", "").replace("`", ""))


class PolicyParseError(Exception):
    pass


def load_policy(path: Path) -> PolicyDocument:
    if not path.exists():
        raise PolicyParseError(f"policy file not found: {path}")
    raw = path.read_bytes()
    text = raw.decode("utf-8")

    # Split into lines while tracking each line's character offset.
    lines: list[tuple[int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        lines.append((pos, line.rstrip("\r\n")))
        pos += len(line)

    title = ""
    metadata: dict[str, str] = {}
    clauses: list[Clause] = []
    section_no: str | None = None
    section_title = ""

    i = 0
    while i < len(lines):
        start, line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            i += 1
            continue
        if section_no is None and "**" in stripped and ":**" in stripped:
            metadata.update({k.strip(): v.strip() for k, v in _META_RE.findall(stripped)})
            i += 1
            continue
        m = _SECTION_RE.match(stripped)
        if m:
            section_no, section_title = m.group(1), m.group(2)
            i += 1
            continue
        if section_no is None:
            i += 1
            continue

        # Gather a block: consecutive non-blank lines of the same kind (table vs text).
        is_table = stripped.startswith("|")
        block = [(start, line)]
        j = i + 1
        while j < len(lines):
            _, nxt = lines[j]
            ns = nxt.strip()
            if not ns or _SECTION_RE.match(ns) or ns.startswith("|") != is_table:
                break
            if not is_table and _NUMBERED_RE.match(ns):
                break  # a new numbered paragraph starts
            block.append(lines[j])
            j += 1

        if is_table:
            clauses.extend(_table_clauses(block, section_no, section_title, text))
        else:
            b_start = block[0][0]
            b_end = block[-1][0] + len(block[-1][1])
            body = text[b_start:b_end]
            nm = _NUMBERED_RE.match(body)
            if nm:
                cid, kind = nm.group(1), "paragraph"
            else:
                cid, kind = section_no, "intro"
                n = 2
                while any(c.id == cid for c in clauses):
                    cid, n = f"{section_no}.intro{n}", n + 1
            clauses.append(Clause(cid, section_no, section_title, kind, body, b_start, b_end))
        i = j

    ids = [c.id for c in clauses]
    dupes = {x for x in ids if ids.count(x) > 1}
    if dupes:
        raise PolicyParseError(f"duplicate clause ids: {sorted(dupes)}")
    if not clauses:
        raise PolicyParseError("no clauses found in policy document")
    for c in clauses:
        assert text[c.start:c.end] == c.text, c.id

    return PolicyDocument(
        title=title, metadata=metadata, clauses=clauses, source_text=text,
        sha256=hashlib.sha256(raw).hexdigest(), path=path,
    )


def _table_clauses(block: list[tuple[int, str]], section_no: str, section_title: str, text: str) -> list[Clause]:
    b_start = block[0][0]
    b_end = block[-1][0] + len(block[-1][1])
    header = block[0][1]
    out = [Clause(f"{section_no}.T", section_no, section_title, "table",
                  text[b_start:b_end], b_start, b_end, table_header=header)]
    n = 0
    for start, line in block[1:]:
        if _TABLE_SEP_RE.match(line.strip()):
            continue
        n += 1
        out.append(Clause(f"{section_no}.T{n}", section_no, section_title, "table_row",
                          line, start, start + len(line), table_header=header))
    return out
