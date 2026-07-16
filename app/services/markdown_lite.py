"""Minimal, dependency-free markdown-ish renderer for interview-prep notes.

Supports only what /recruiter actually produces: headers (## / ###), bold
(**text**), bullet/numbered lists (- / 1.), links ([text](url)), and
paragraphs. Input is HTML-escaped first, so no raw markup in a note can
ever reach the page — the markdown syntax below is then reconstructed on
top of the escaped text.
"""
import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)")
_H3_RE = re.compile(r"^###\s+(.*)")
_H2_RE = re.compile(r"^##\s+(.*)")


def _inline(text: str) -> str:
    text = _LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    return text


def render(body: str) -> str:
    if not body:
        return ""
    escaped = html.escape(body, quote=False)
    lines = escaped.split("\n")

    html_parts: list[str] = []
    list_tag: str | None = None  # "ul" or "ol", when currently inside a list
    para: list[str] = []

    def flush_para():
        if para:
            html_parts.append(f"<p>{'<br>'.join(para)}</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            html_parts.append(f"</{list_tag}>")
            list_tag = None

    for raw_line in lines:
        line = raw_line.strip()

        if not line:
            flush_para()
            close_list()
            continue

        h2 = _H2_RE.match(line)
        h3 = _H3_RE.match(line)
        bullet = _BULLET_RE.match(line)
        numbered = _NUMBERED_RE.match(line)

        if h2 or h3:
            flush_para()
            close_list()
            tag = "h4" if h2 else "h5"
            content = (h2 or h3).group(1)
            html_parts.append(f"<{tag}>{_inline(content)}</{tag}>")
        elif bullet:
            flush_para()
            if list_tag != "ul":
                close_list()
                html_parts.append("<ul>")
                list_tag = "ul"
            html_parts.append(f"<li>{_inline(bullet.group(1))}</li>")
        elif numbered:
            flush_para()
            if list_tag != "ol":
                close_list()
                html_parts.append("<ol>")
                list_tag = "ol"
            html_parts.append(f"<li>{_inline(numbered.group(1))}</li>")
        else:
            close_list()
            para.append(_inline(line))

    flush_para()
    close_list()
    return "\n".join(html_parts)
