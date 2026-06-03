"""A static index page that links every dashboard/comparison HTML in a directory.

``scan_directory`` reads each ``*.html`` (skipping the index itself), pulls its
``<title>``, and ``render_index`` lays them out as a simple linked list. Pure and
offline like the rest of the report package; the only IO is reading the local
files to be linked.
"""

from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path

from .dashboard import _esc
from .theme import document


class _TitleParser(HTMLParser):
    """Extracts the first complete ``<title>`` text. Using the stdlib parser (rather
    than ``str.find``) handles attributes (``<title foo>``), entity decoding, and
    nested/malformed markup without ever leaking literal tags into the result."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # entities -> text automatically
        self._in = False
        self._done = False
        self._buf: list[str] = []
        self.title: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self._done:
            self._in = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in:
            self._in = False
            self._done = True
            self.title = "".join(self._buf).strip()

    def handle_data(self, data):
        if self._in and not self._done:
            self._buf.append(data)


def _extract_title(html: str, fallback: str) -> str:
    """Pull the document <title> as plain text; fall back to ``fallback`` if absent.

    Handles attributes, entity decoding, and a missing close tag. (A *pathological*
    nested ``<title>`` is interpreter-dependent — ``<title>`` is an RCDATA element on
    some Python versions — but the title is always HTML-escaped on render, so any
    residue is inert.)"""
    p = _TitleParser()
    try:
        p.feed(html)
    except Exception:  # malformed HTML — never let the index build crash
        return fallback
    return p.title or fallback


def scan_directory(directory, *, index_name: str = "index.html") -> list[dict]:
    """Return ``[{file, title, bytes}]`` for every ``*.html`` in ``directory`` except
    the index file, sorted by filename (deterministic)."""
    d = Path(directory)
    entries: list[dict] = []
    for p in sorted(d.glob("*.html")):
        if p.name == index_name:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entries.append({"file": p.name, "title": _extract_title(text, p.name),
                        "bytes": p.stat().st_size})
    return entries


def render_index(
    entries: Sequence[dict],
    *,
    title: str = "Intraday engine — report index",
    generated_at: str | None = None,
) -> str:
    """Render a linked index from ``[{file, title, bytes}]`` entries."""
    gen = _esc(generated_at) if generated_at else "—"
    if not entries:
        body = (
            f"<h1>{_esc(title)}</h1>"
            f'<p class="sub">generated {gen}</p>'
            '<p class="muted">No dashboards found in this directory yet. '
            "Generate one with <code>python -m intraday report --out &lt;dir&gt;/&lt;name&gt;.html</code>.</p>"
        )
        return document(title, body)

    rows = []
    for e in entries:
        kb = f"{e.get('bytes', 0) / 1024:.0f} KB"
        rows.append(
            "<li>"
            f'<a class="idx__title" href="{_esc(e["file"])}">{_esc(e["title"])}</a>'
            f'<span class="idx__meta">{_esc(e["file"])}</span>'
            f'<span class="idx__meta num">{kb}</span>'
            "</li>"
        )
    body = (
        f"<h1>{_esc(title)}</h1>"
        f'<p class="sub">{len(entries)} report(s) &middot; generated {gen}</p>'
        '<section class="card"><ul class="idx">' + "".join(rows) + "</ul></section>"
        '<p class="foot">Links are relative — keep the HTML files alongside this index. '
        "Offline; no external resources.</p>"
    )
    return document(title, body)


def build_index(
    directory,
    *,
    title: str = "Intraday engine — report index",
    generated_at: str | None = None,
    index_name: str = "index.html",
) -> str:
    """Scan ``directory`` for dashboards and render the index page."""
    return render_index(
        scan_directory(directory, index_name=index_name),
        title=title, generated_at=generated_at,
    )
