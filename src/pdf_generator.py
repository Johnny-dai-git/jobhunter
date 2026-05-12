"""Markdown -> styled HTML -> PDF.

Use markdown library to render MD, weasyprint to convert to PDF.
Clean CSS designed for resume (Times-like serif + 1in margin + compact layout).
"""
from __future__ import annotations

from pathlib import Path

import markdown as md_lib
from weasyprint import CSS, HTML


# Clean professional resume style
RESUME_CSS = """
@page {
    size: letter;
    margin: 0.6in 0.8in;
}
body {
    font-family: 'Georgia', 'Times New Roman', serif;
    font-size: 10.5pt;
    line-height: 1.35;
    color: #1f2937;
}
h1 {
    font-size: 18pt;
    margin: 0 0 4px 0;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #1f2937;
    text-align: center;
    letter-spacing: 0.5px;
}
h2 {
    font-size: 12pt;
    margin: 10px 0 4px 0;
    padding-bottom: 2px;
    border-bottom: 0.5px solid #6b7280;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #111827;
}
h3 {
    font-size: 10.5pt;
    margin: 6px 0 2px 0;
    color: #111827;
}
blockquote {
    margin: 4px 0;
    padding-left: 8px;
    color: #6b7280;
    font-style: italic;
    border-left: 2px solid #e5e7eb;
}
ul {
    margin: 4px 0 6px 18px;
    padding: 0;
}
li {
    margin: 1px 0;
}
p {
    margin: 4px 0;
}
strong {
    color: #111827;
}
a {
    color: #1d4ed8;
    text-decoration: none;
}
"""


def md_to_pdf(md_text: str, out_pdf: Path, css: str = RESUME_CSS) -> Path:
    """Render markdown text to PDF file. out_pdf is absolute path."""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    html_body = md_lib.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    # Wrap in minimal HTML shell
    full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>{html_body}</body></html>"
    HTML(string=full_html).write_pdf(
        target=str(out_pdf),
        stylesheets=[CSS(string=css)],
    )
    return out_pdf
