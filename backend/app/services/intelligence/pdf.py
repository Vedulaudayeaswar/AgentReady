"""Minimal text PDF writer (PDF 1.4, Helvetica, A4) — enough for tabular merchant reports, no dependency.

Text is encoded as WinAnsi (Latin-1 subset); characters outside it are replaced with '?'.
"""

from __future__ import annotations

import textwrap

PAGE_W, PAGE_H, MARGIN = 595, 842, 48
LINE = 13


def _esc(s: str) -> str:
    s = s.encode("cp1252", "replace").decode("cp1252")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render(title: str, blocks: list[tuple[str, str]]) -> bytes:
    """blocks: (style, text) with style in h1|h2|p|small|row."""
    pages: list[list[str]] = [[]]
    y = PAGE_H - MARGIN

    def emit(text: str, size: int, bold: bool = False, indent: int = 0):
        nonlocal y
        width = int((PAGE_W - 2 * MARGIN - indent) / (size * 0.5))
        for line in textwrap.wrap(text, width) or [""]:
            if y < MARGIN + LINE:
                pages.append([])
                y = PAGE_H - MARGIN
            font = "F2" if bold else "F1"
            pages[-1].append(f"BT /{font} {size} Tf {MARGIN + indent} {y} Td ({_esc(line)}) Tj ET")
            y -= size + 4

    emit(title, 16, True)
    y -= 4
    for style, text in blocks:
        if style == "h1":
            y -= 6
            emit(text, 13, True)
        elif style == "h2":
            y -= 2
            emit(text, 11, True)
        elif style == "small":
            emit(text, 8)
        elif style == "row":
            emit(text, 9, indent=8)
        else:
            emit(text, 10)
    objs: list[bytes] = []
    kids = []
    n_pages = len(pages)
    # object numbering: 1 catalog, 2 pages, 3 font regular, 4 font bold, then (page, content) pairs
    for i, cmds in enumerate(pages):
        footer = f"BT /F1 8 Tf {MARGIN} 24 Td ({_esc(f'AgentReady report - page {i + 1} of {n_pages}')}) Tj ET"
        stream = "\n".join(cmds + [footer]).encode("cp1252", "replace")
        page_no, content_no = 5 + 2 * i, 6 + 2 * i
        kids.append(f"{page_no} 0 R")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << /Font << /F1 3 0 R "
                    f"/F2 4 0 R >> >> /Contents {content_no} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    head = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n_pages} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(head + objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
