"""Builds Slack table blocks from markdown pipe tables.

2026-11-10 owner request: a long table cell forces horizontal scrolling,
which is unreadable on mobile. When Slack converts a markdown block's pipe
table it adds no column_settings, so every column stays on one line, and a
<br> in a cell is kept as literal text. Tables are therefore built here as
table blocks with is_wrapped on every column, and <br> becomes a newline
inside the cell. Measured the same day: a null entry in column_settings is
rejected with invalid_blocks, an explicit object passes, and "\n" inside a
rich_text cell is stored as-is. Two table blocks in one message are accepted.

Everything outside a table stays a markdown block. When conversion is not
possible to_blocks() returns None and the caller keeps the single markdown
block.
"""


import re

TABLE_CHAR_LIMIT = 10000   # 한 메시지의 표 칸 글자 합 상한
TABLE_ROW_LIMIT = 100
TABLE_COL_LIMIT = 20
MARKDOWN_LIMIT = 12000     # 한 메시지의 markdown 블록 글자 합 상한

_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_BR = re.compile(r"<br\s*/?>", re.I)
_INLINE = re.compile(
    r"\[([^\]]+)\]\((https?://[^)\s]+)\)"     # 1,2 마크다운 링크
    r"|<(https?://[^|>]+)\|([^>]+)>"          # 3,4 슬랙 링크
    r"|\*\*(.+?)\*\*"                         # 5 굵게
    r"|<@([UW][A-Z0-9]+)>"                    # 6 사람 멘션
    r"|<#(C[A-Z0-9]+)(?:\|[^>]*)?>"           # 7 채널
    r"|(https?://[^\s<>|]+)"                  # 8 맨 주소
)


def _is_row(line):
    s = line.strip()
    return len(s) >= 2 and s.startswith("|") and s.endswith("|")


def _split_row(line):
    s = line.strip()[1:-1]
    cells, cur, i = [], "", 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "|"
            i += 2
            continue
        if ch == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    cells.append(cur.strip())
    return cells


def _text(t, bold=False):
    el = {"type": "text", "text": t}
    if bold:
        el["style"] = {"bold": True}
    return el


def _cell(raw):
    raw = _BR.sub("\n", raw)
    els, pos = [], 0
    for m in _INLINE.finditer(raw):
        if m.start() > pos:
            els.append(_text(raw[pos:m.start()]))
        if m.group(1):
            els.append({"type": "link", "url": m.group(2), "text": m.group(1)})
        elif m.group(3):
            els.append({"type": "link", "url": m.group(3), "text": m.group(4)})
        elif m.group(5):
            els.append(_text(m.group(5), bold=True))
        elif m.group(6):
            els.append({"type": "user", "user_id": m.group(6)})
        elif m.group(7):
            els.append({"type": "channel", "channel_id": m.group(7)})
        else:
            els.append({"type": "link", "url": m.group(8)})
        pos = m.end()
    if pos < len(raw):
        els.append(_text(raw[pos:]))
    if not els:
        els = [_text(" ")]
    return {"type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": els}]}


def _table_block(lines):
    rows = [_split_row(l) for l in lines if not _SEP.match(l.strip())]
    if not rows:
        return None, 0
    ncol = max(len(r) for r in rows)
    if ncol > TABLE_COL_LIMIT or len(rows) > TABLE_ROW_LIMIT:
        return None, 0
    chars = 0
    out = []
    for r in rows:
        r = r + [""] * (ncol - len(r))
        chars += sum(len(c) for c in r)
        out.append([_cell(c) for c in r])
    return {"type": "table",
            "column_settings": [{"is_wrapped": True} for _ in range(ncol)],
            "rows": out}, chars


def to_blocks(text):
    """표가 있으면 markdown 블록과 table 블록 목록을, 없거나 못 바꾸면 None 을 돌려준다."""
    lines = text.split("\n")
    segs = []          # ("md", [lines]) 또는 ("table", [lines])
    fenced = False
    i = 0
    found = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            fenced = not fenced
        # 표는 머리 행 다음 줄이 구분선일 때만 표로 본다.
        if (not fenced and _is_row(line) and i + 1 < len(lines)
                and _SEP.match(lines[i + 1].strip())):
            j = i + 2
            while j < len(lines) and _is_row(lines[j]):
                j += 1
            segs.append(("table", lines[i:j]))
            found = True
            i = j
            continue
        if segs and segs[-1][0] == "md":
            segs[-1][1].append(line)
        else:
            segs.append(("md", [line]))
        i += 1
    if not found:
        return None
    blocks, tchars, mchars = [], 0, 0
    for kind, ls in segs:
        if kind == "table":
            blk, n = _table_block(ls)
            if blk is None:
                return None
            tchars += n
            blocks.append(blk)
        else:
            body = "\n".join(ls).strip("\n")
            if body.strip():
                mchars += len(body)
                blocks.append({"type": "markdown", "text": body})
    if tchars > TABLE_CHAR_LIMIT or mchars > MARKDOWN_LIMIT or len(blocks) > 50:
        return None
    return blocks
