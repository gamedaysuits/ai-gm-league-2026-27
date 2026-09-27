"""A deliberately small Markdown -> HTML converter for the league's own docs.

Covers what RULES.md, DRAND_COMMITMENT.md and the history write-ups use:
ATX headings, paragraphs, bullet/numbered lists (with indented continuation
lines), GFM pipe tables with alignment, fenced code, inline code, **bold**,
*italic* and [links](url). All text is HTML-escaped first, so the output is
safe to inject into a page.
"""
from __future__ import annotations

import html
import re

_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_]+", "-", text).strip("-") or "section"


def _safe_url(url: str) -> str:
    url = html.unescape(url).strip()
    if re.match(r"^(https?:|mailto:|#|\.?/|[\w.-]+\.html)", url, re.I):
        return html.escape(url, quote=True)
    return "#"


def inline(text: str) -> str:
    """Escape, then apply code spans, links, bold and italic."""
    text = html.escape(text, quote=False)
    codes: list[str] = []

    def stash(m: re.Match[str]) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)

    def link(m: re.Match[str]) -> str:
        href = _safe_url(m.group(2))
        external = href.startswith("http")
        extra = ' rel="noopener" target="_blank"' if external else ""
        return f'<a href="{href}"{extra}>{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _align(cell: str) -> str:
    left, right = cell.startswith(":"), cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return ""


def _table(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    def cell(tag: str, text: str, i: int) -> str:
        a = aligns[i] if i < len(aligns) else ""
        cls = f' class="num"' if a == "right" else (' class="center"' if a == "center" else "")
        scope = ' scope="col"' if tag == "th" else ""
        return f"<{tag}{cls}{scope}>{inline(text)}</{tag}>"

    head = "".join(cell("th", h, i) for i, h in enumerate(header))
    body = "".join("<tr>" + "".join(cell("td", c, i) for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def md_to_html(md: str, *, shift: int = 0, drop_title: bool = False) -> str:
    """Convert Markdown to HTML. `shift` demotes headings; `drop_title` removes a leading H1."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            out.append("<p>" + inline(" ".join(s.strip() for s in para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            lang = re.sub(r"[^\w-]", "", stripped[3:].strip())
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
            continue

        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            if drop_title and level == 1 and not out:
                i += 1
                continue
            level = min(6, level + shift)
            text = m.group(2)
            out.append(f'<h{level} id="{slugify(text)}">{inline(text)}</h{level}>')
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            flush()
            header = _cells(line)
            aligns = [_align(c) for c in _cells(lines[i + 1])]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append(_table(header, aligns, rows))
            continue

        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                flush()
                items: list[list[str]] = []
                while i < len(lines):
                    cur = lines[i]
                    mm = pattern.match(cur)
                    if mm:
                        items.append([mm.group(1)])
                    elif cur.strip() and cur[:1] in (" ", "\t") and items:
                        items[-1].append(cur.strip())
                    else:
                        break
                    i += 1
                body = "".join("<li>" + inline(" ".join(it)) + "</li>" for it in items)
                out.append(f"<{tag}>{body}</{tag}>")
                break
        else:
            if not stripped:
                flush()
            else:
                para.append(line)
            i += 1
            continue
        continue

    flush()
    return "\n".join(out)
