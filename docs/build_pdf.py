"""Render docs/ARCHITECTURE.md to docs/Implementation_Guide.pdf.

Handles the Markdown subset used in the guide: headings (#, ##, ###), paragraphs, bullet and
numbered lists, **bold**, *italic*, `code`, fenced code blocks and pipe tables.

    backend/.venv/bin/pip install -r docs/requirements.txt
    backend/.venv/bin/python docs/build_pdf.py
"""
import re
import sys
from pathlib import Path

try:
    from reportlab.lib import colors
except ModuleNotFoundError:
    sys.exit("reportlab is not installed for this Python. Install it with:\n"
             "    backend/.venv/bin/pip install -r docs/requirements.txt\n"
             "then run:\n"
             "    backend/.venv/bin/python docs/build_pdf.py")
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = Path(__file__).parent
SRC = HERE / "ARCHITECTURE.md"
OUT = HERE / "Implementation_Guide.pdf"

FONTS = Path("/System/Library/Fonts")
SUP = FONTS / "Supplemental"
pdfmetrics.registerFont(TTFont("Body", str(SUP / "Arial Unicode.ttf")))
pdfmetrics.registerFont(TTFont("Bold", str(SUP / "Arial Bold.ttf")))
pdfmetrics.registerFont(TTFont("Italic", str(SUP / "Arial Italic.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONTS / "Menlo.ttc"), subfontIndex=0))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Bold", italic="Italic", boldItalic="Bold")

INK = colors.black
INK2 = colors.HexColor("#333333")
LEDGER = colors.HexColor("#0B3D91")
WASH = colors.HexColor("#EDF1F8")
RULE = colors.HexColor("#6B6B6B")

body = ParagraphStyle("body", fontName="Body", fontSize=9.6, leading=14, textColor=INK, spaceAfter=5,
                      alignment=TA_LEFT)
small = ParagraphStyle("small", parent=body, fontSize=8.6, leading=12, textColor=INK2)
cell = ParagraphStyle("cell", parent=body, fontSize=8.3, leading=11, spaceAfter=0)
cellh = ParagraphStyle("cellh", parent=cell, fontName="Bold")
h1 = ParagraphStyle("h1", fontName="Bold", fontSize=22, leading=27, textColor=INK, spaceAfter=6)
h2 = ParagraphStyle("h2", fontName="Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=14, spaceAfter=6)
h3 = ParagraphStyle("h3", fontName="Bold", fontSize=11.5, leading=15, textColor=LEDGER, spaceBefore=10,
                    spaceAfter=4)
code = ParagraphStyle("code", fontName="Mono", fontSize=7.2, leading=9.4, textColor=INK, backColor=WASH,
                      borderPadding=(5, 6, 5, 6), spaceBefore=4, spaceAfter=9, leftIndent=2, rightIndent=2)
contents_h = ParagraphStyle("contents", parent=h2)
toc_l1 = ParagraphStyle("toc1", parent=body, fontName="Bold", fontSize=9.6, leading=12.5, leftIndent=0)
toc_l2 = ParagraphStyle("toc2", parent=body, fontSize=8.6, leading=10.6, leftIndent=14, textColor=INK2)


def inline(text: str) -> str:
    """Markdown inline -> ReportLab mini-HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f'<font face="Mono" size="8.2" color="#0B3D91">{p[1:-1]}</font>')
            continue
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", p)
        out.append(p)
    return "".join(out)


class Doc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ("h2", "h3"):
            level = 0 if flowable.style.name == "h2" else 1
            text = flowable.getPlainText()
            key = f"h{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))


def table(rows: list[list[str]], width: float) -> Table:
    ncol = len(rows[0])
    # Column widths: proportional to the longest cell text, bounded.
    lens = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(ncol)]
    lens = [min(max(l, 6), 70) for l in lens]
    total = sum(lens)
    widths = [width * l / total for l in lens]
    data = [[Paragraph(inline(c), cellh if ri == 0 else cell) for c in r + [""] * (ncol - len(r))]
            for ri, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), WASH),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build_story(md: str, width: float):
    lines = md.splitlines()
    story, i = [], 0
    para: list[str] = []

    def flush():
        nonlocal para
        if para:
            story.append(Paragraph(inline(" ".join(para)), body))
            para = []

    while i < len(lines):
        line = lines[i]
        s = line.rstrip()
        if s.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            pre = Preformatted("\n".join(block), code)
            story.append(KeepTogether([pre]) if len(block) <= 48 else pre)
            i += 1
            continue
        if s.startswith("# "):
            flush()
            story.append(Paragraph(inline(s[2:]), h1))
            i += 1
            continue
        if s.startswith("## "):
            flush()
            story.append(Paragraph(inline(s[3:]), h2))
            i += 1
            continue
        if s.startswith("### "):
            flush()
            story.append(Paragraph(inline(s[4:]), h3))
            i += 1
            continue
        if s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            story.append(table(rows, width))
            story.append(Spacer(1, 8))
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            flush()
            ordered = m.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if not m:
                    if lines[i].startswith("   ") and lines[i].strip() and items:
                        items[-1][1] += " " + lines[i].strip()   # continuation line
                        i += 1
                        continue
                    break
                items.append([len(m.group(1)), m.group(3)])
                i += 1
            flow, stack = [], []
            for indent, text in items:
                item = ListItem(Paragraph(inline(text), body), leftIndent=12 + indent * 4, value=None)
                flow.append(item)
            story.append(ListFlowable(flow, bulletType="1" if ordered else "bullet", start="1" if ordered else "•",
                                      bulletFontName="Body", bulletFontSize=8, leftIndent=14,
                                      bulletColor=INK2))
            story.append(Spacer(1, 3))
            continue
        if not s:
            flush()
            i += 1
            continue
        para.append(s.strip())
        i += 1
    flush()
    return story


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Body", 7.5)
    canvas.setFillColor(INK2)
    canvas.drawString(doc.leftMargin, 12 * mm, "Procurement Q&A Agent — Implementation Guide")
    canvas.drawRightString(A4[0] - doc.rightMargin, 12 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(INK)
    canvas.setLineWidth(1.2)
    canvas.line(doc.leftMargin, A4[1] - 14 * mm, A4[0] - doc.rightMargin, A4[1] - 14 * mm)
    canvas.restoreState()


def main():
    md = SRC.read_text(encoding="utf-8")
    doc = Doc(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm,
              bottomMargin=20 * mm, title="Procurement Q&A Agent — Implementation Guide",
              author="Procurement Q&A Agent project", subject="Architecture, component APIs and walkthroughs")
    width = A4[0] - doc.leftMargin - doc.rightMargin
    story = build_story(md, width)
    # Title block, then table of contents, then the body (from section 1 on).
    first_h2 = next(n for n, f in enumerate(story) if isinstance(f, Paragraph) and f.style.name == "h2")
    toc = TableOfContents()
    toc.levelStyles = [toc_l1, toc_l2]
    toc.dotsMinLevel = 0
    head = story[:first_h2]
    contents = [Paragraph("Contents", contents_h), toc, PageBreak()]
    doc.multiBuild(head + [Spacer(1, 10)] + contents + story[first_h2:], onFirstPage=on_page,
                   onLaterPages=on_page)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
