"""Convert GitHub-flavored Markdown into clean, readable Telegram HTML and native WhatsApp formatting."""
import html
import re


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?[\s:-]+\|[\s|:-]*$", line))


def _format_inline_tg(text: str) -> str:
    """Convert inline Markdown to Telegram-safe HTML."""
    codes = []

    def _stash_code(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00C{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", _stash_code, text)
    s = html.escape(s)

    # Links [label](https://...)
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        s,
    )
    # Bold **text** or __text__
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    # Strikethrough ~~text~~
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    # Italic *text* (avoid matching inside words/asterisks)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)

    s = re.sub(r"\x00C(\d+)\x00", lambda m: codes[int(m.group(1))], s)
    return s


def md_to_telegram_html(md_text: str) -> str:
    """Convert Markdown into clean, well-spaced Telegram HTML (parse_mode='HTML')."""
    src = str(md_text or "").replace("\r\n", "\n").strip()
    if not src:
        return "(empty)"

    blocks = []

    def _stash_pre(m):
        lang = m.group(1).strip()
        code = html.escape(m.group(2).rstrip("\n"))
        if lang:
            blocks.append(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
        else:
            blocks.append(f"<pre>{code}</pre>")
        return f"\n\x00B{len(blocks) - 1}\x00\n"

    src = re.sub(r"```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)```", _stash_pre, src)

    lines = src.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        bm = re.match(r"^\x00B(\d+)\x00$", stripped)
        if bm:
            out.append(blocks[int(bm.group(1))])
            i += 1
            continue

        # Headings -> bold section titles with clean visual hierarchy
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            lvl = len(hm.group(1))
            title = _format_inline_tg(hm.group(2).strip())
            if out and out[-1] != "":
                out.append("")
            if lvl == 1:
                out.append(f"<b>▸ {title}</b>")
            elif lvl == 2:
                out.append(f"<b>{title}</b>")
            else:
                out.append(f"<b><u>{title}</u></b>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            out.append("────────────")
            i += 1
            continue

        # Markdown table -> readable bullet rows (tables break in mobile chat)
        if "|" in stripped and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            headers = _split_table_row(line)
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                cells = _split_table_row(lines[i])
                pairs = []
                for idx, h in enumerate(headers):
                    val = cells[idx] if idx < len(cells) else ""
                    if val:
                        pairs.append(f"<b>{_format_inline_tg(h)}:</b> {_format_inline_tg(val)}")
                if pairs:
                    out.append("• " + "  |  ".join(pairs))
                i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(_format_inline_tg(re.sub(r"^>\s?", "", lines[i].strip())))
                i += 1
            out.append("<blockquote>" + "\n".join(q) + "</blockquote>")
            continue

        # Bullet list
        lm = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if lm:
            indent = "  " if len(lm.group(1)) >= 2 else ""
            out.append(f"{indent}• {_format_inline_tg(lm.group(2))}")
            i += 1
            continue

        # Numbered list
        nm = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if nm:
            indent = "  " if len(nm.group(1)) >= 2 else ""
            out.append(f"{indent}{nm.group(2)}. {_format_inline_tg(nm.group(3))}")
            i += 1
            continue

        out.append(_format_inline_tg(line))
        i += 1

    return "\n".join(out).strip()


def _format_inline_wa(text: str) -> str:
    """Convert inline Markdown to native WhatsApp syntax (*bold*, _italic_, ~strike~, `code`)."""
    codes = []

    def _stash_code(m):
        codes.append(f"`{m.group(1)}`")
        return f"\x00C{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", _stash_code, text)

    # Links [label](url) -> label (url)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", s)

    # Stash bold (**text** / __text__) before converting single *italic*
    bolds = []

    def _stash_bold(m):
        bolds.append(f"*{m.group(1).strip()}*")
        return f"\x00D{len(bolds) - 1}\x00"

    s = re.sub(r"\*\*(.+?)\*\*", _stash_bold, s)
    s = re.sub(r"__(.+?)__", _stash_bold, s)

    # Italic *text* -> _text_
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"_\1_", s)
    # Strikethrough ~~text~~ -> ~text~
    s = re.sub(r"~~(.+?)~~", r"~\1~", s)

    s = re.sub(r"\x00D(\d+)\x00", lambda m: bolds[int(m.group(1))], s)
    s = re.sub(r"\x00C(\d+)\x00", lambda m: codes[int(m.group(1))], s)
    return s


def md_to_whatsapp(md_text: str) -> str:
    """Convert Markdown into clean, readable native WhatsApp formatting."""
    src = str(md_text or "").replace("\r\n", "\n").strip()
    if not src:
        return ""

    blocks = []

    def _stash_pre(m):
        code = m.group(2).rstrip("\n")
        blocks.append(f"```\n{code}\n```")
        return f"\n\x00B{len(blocks) - 1}\x00\n"

    src = re.sub(r"```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)```", _stash_pre, src)

    lines = src.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        bm = re.match(r"^\x00B(\d+)\x00$", stripped)
        if bm:
            out.append(blocks[int(bm.group(1))])
            i += 1
            continue

        # Headings -> *BOLD*
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            lvl = len(hm.group(1))
            raw_title = re.sub(r"\*\*(.+?)\*\*", r"\1", hm.group(2).strip())
            if out and out[-1] != "":
                out.append("")
            out.append(f"*{raw_title.upper()}*" if lvl == 1 else f"*{raw_title}*")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            out.append("────────────")
            i += 1
            continue

        # Markdown table -> readable bullet rows
        if "|" in stripped and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            headers = _split_table_row(line)
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                cells = _split_table_row(lines[i])
                pairs = []
                for idx, h in enumerate(headers):
                    val = cells[idx] if idx < len(cells) else ""
                    if val:
                        pairs.append(f"*{h}:* {_format_inline_wa(val)}")
                if pairs:
                    out.append("• " + " | ".join(pairs))
                i += 1
            continue

        # Blockquote (WhatsApp supports `> ` natively)
        if stripped.startswith(">"):
            out.append("> " + _format_inline_wa(re.sub(r"^>\s?", "", stripped)))
            i += 1
            continue

        # Bullet list
        lm = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if lm:
            indent = "  " if len(lm.group(1)) >= 2 else ""
            out.append(f"{indent}• {_format_inline_wa(lm.group(2))}")
            i += 1
            continue

        # Numbered list
        nm = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if nm:
            indent = "  " if len(nm.group(1)) >= 2 else ""
            out.append(f"{indent}{nm.group(2)}. {_format_inline_wa(nm.group(3))}")
            i += 1
            continue

        out.append(_format_inline_wa(line))
        i += 1

    return "\n".join(out).strip()
