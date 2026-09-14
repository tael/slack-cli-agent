"""메시지 상한에 맞춘 안전 분할.

원본 bot.py 의 chunk, md_chunks, fit_chunk,
split_for_blocks, merge_tiny 를 그대로 옮겼다 (이식 분류 — 함수 본문은
수정하지 않는다).

상한값은 `RuntimeSettings` 에서 받는다 — `slack_chunk`(평문 채널 한 덩어리
상한, 3500자), `markdown_block_limit`(리치 채널 markdown 블록 상한, 12000자).
"""

from __future__ import annotations

import re

from slack_cli_agent.config.settings import RuntimeSettings

from .blocks import SPLIT_MARKER, BlockBuilder

# 표·코드블록·인용은 한 줄만 떨어져 나가도 렌더가 무너지는 덩어리 유형이다.
ATOMIC_HEADS = ("|", "```", ">")


class ContentSplitter:
    """텍스트를 슬랙 상한에 맞춰 안전하게 나눈다."""

    def __init__(self, settings: RuntimeSettings, block_builder: BlockBuilder) -> None:
        self._settings = settings
        self._blocks = block_builder

    def chunk(self, text: str, size: int | None = None) -> list[str]:
        """슬랙 메시지 길이에 맞춰 자른다. 줄 경계를 우선한다."""
        if size is None:
            size = self._settings.slack_chunk
        if len(text) <= size:
            return [text]
        parts, buf = [], ""
        for line in text.split("\n"):
            if len(buf) + len(line) + 1 > size:
                if buf:
                    parts.append(buf)
                    buf = ""
                while len(line) > size:
                    parts.append(line[:size])
                    line = line[size:]
            buf = f"{buf}\n{line}" if buf else line
        if buf:
            parts.append(buf)
        return parts

    def line_kind(self, line: str) -> str:
        """줄 하나가 어떤 덩어리에 속하는지 가른다."""
        stripped = line.strip()
        if stripped.startswith("|"):
            return "table"
        if stripped.startswith(">"):
            return "quote"
        if re.match(r"[-*+]\s|\d+[.)]\s", stripped):
            return "list"
        if re.match(r"\s+", line) and stripped:
            return "list"          # 들여쓴 줄은 앞 항목의 이어짐이다
        if not stripped:
            return "blank"
        return "text"

    def md_chunks(self, text: str) -> list[str]:
        """마크다운을 중간에서 자르면 안 되는 덩어리 단위로 끊는다.

        표, 코드블록, 인용, 목록은 한 줄만 떨어져 나가도 렌더가 무너진다.
        표는 열 이름 행을 잃고, 코드블록은 펜스 짝이 깨지고,
        목록은 번호가 되돌아가거나 들여쓰기가 풀린다.
        빈 줄이 없는 답변에서도 경계를 잡아야 하므로 문단이 아니라 줄을 본다.
        """
        chunks: list[str] = []
        buf: list[str] = []
        in_fence, mode = False, None

        def flush() -> None:
            if buf:
                chunks.append("\n".join(buf))
                buf.clear()

        for line in text.split("\n"):
            stripped = line.strip()

            if in_fence:
                buf.append(line)
                if stripped.startswith("```"):
                    flush()
                    in_fence = False
                    mode = None
                continue
            if stripped.startswith("```"):
                flush()
                buf.append(line)
                in_fence = True
                continue

            kind = self.line_kind(line)
            if kind == "blank":
                # 빈 줄은 목록 안에서는 항목 사이 여백일 수 있어 덩어리를 끊지 않는다.
                buf.append(line)
                continue
            if mode and kind != mode:
                flush()
            if not mode or kind != mode:
                mode = kind if kind in ("table", "quote", "list") else None
                if mode is None and buf and self.line_kind(buf[-1]) != "text":
                    pass
            buf.append(line)
        flush()
        return [c for c in chunks if c.strip() or c == ""]

    def fit_chunk(self, chunk: str, limit: int) -> list[str]:
        """덩어리 하나가 상한을 넘으면 형식을 지키며 쪼갠다.

        표는 조각마다 열 이름 행과 구분선을 다시 달아 준다.
        이게 없으면 둘째 조각이 표로 인식되지 않고 파이프가 글자로 노출된다.
        코드블록은 조각마다 펜스를 닫고 다시 연다.
        """
        if len(chunk) <= limit:
            return [chunk]
        lines = chunk.split("\n")

        if lines[0].strip().startswith("|") and len(lines) > 2:
            head = lines[:2]
            out, cur = [], list(head)
            for row in lines[2:]:
                if len(cur) > 2 and len("\n".join(cur + [row])) > limit:
                    out.append("\n".join(cur))
                    cur = list(head)
                cur.append(row)
            if len(cur) > 2:
                out.append("\n".join(cur))
            return out

        if lines[0].strip().startswith("```"):
            fence = lines[0]
            body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
            out, cur = [], [fence]
            for row in body:
                if len(cur) > 1 and len("\n".join(cur + [row, "```"])) > limit:
                    cur.append("```")
                    out.append("\n".join(cur))
                    cur = [fence]
                cur.append(row)
            cur.append("```")
            out.append("\n".join(cur))
            return out

        if lines[0].strip().startswith(">"):
            out, cur = [], []
            for row in lines:
                if cur and len("\n".join(cur + [row])) > limit:
                    out.append("\n".join(cur))
                    cur = []
                cur.append(row)
            if cur:
                out.append("\n".join(cur))
            return out

        if len(lines) > 1:
            out, cur = [], []
            for row in lines:
                if cur and len("\n".join(cur + [row])) > limit:
                    out.append("\n".join(cur))
                    cur = []
                cur.append(row)
            if cur:
                out.append("\n".join(cur))
            return out

        return [chunk[i:i + limit] for i in range(0, len(chunk), limit)]

    def split_for_blocks(self, text: str, limit: int | None = None) -> list[str]:
        """답변을 메시지 단위로 나눈다.

        답변이 스스로 표시한 경계를 먼저 따른다. 쓴 쪽이 의미 단위를 안다.
        다만 표와 코드블록 안에 찍힌 마커는 무시한다.
        거기서 끊으면 열 이름 행이 없는 조각이 생겨 표가 파이프 글자로 노출된다.
        마커가 없거나 조각이 상한을 넘으면 형식을 지키며 기계적으로 더 쪼갠다.
        """
        if limit is None:
            limit = self._settings.markdown_block_limit
        text = self._blocks.clean_markers(text)
        parts: list[str] = []
        forced: list[bool] = []
        buf: list[str] = []

        def flush(by_marker: bool = False, final: bool = False) -> None:
            if not buf:
                return
            if final:
                # 마지막 방출에서는 헤딩을 되실행하지 않는다. 되돌린 꼬리가 갈 곳이 없다.
                parts.append("\n".join(buf).strip("\n"))
                forced.append(by_marker)
                buf.clear()
                return
            # 조각이 헤딩으로 끝나면 읽는 사람은 내용 없는 제목만 본다.
            # 그 헤딩은 뒤따르는 내용과 함께 다음 조각으로 넘긴다.
            trailing: list[str] = []
            while buf and (not buf[-1].strip() or buf[-1].strip().startswith("#")):
                trailing.insert(0, buf.pop())
            if not buf:
                buf.extend(trailing)
                trailing = []
            parts.append("\n".join(buf).strip("\n"))
            forced.append(by_marker)
            buf.clear()
            buf.extend(x for x in trailing if x.strip())

        room = int(limit * 0.92)   # 꽉 채우면 앞뒤 문장이 붙을 자리가 없다
        for block in self.md_chunks(text):
            if block.lstrip().startswith(("|", "```")):
                segments = [block.replace(SPLIT_MARKER, "").rstrip()]
            else:
                segments = block.split(SPLIT_MARKER)
            for idx, seg in enumerate(segments):
                if idx:
                    flush(by_marker=True)
                seg = seg.strip("\n")
                if not seg.strip():
                    continue
                for piece in self.fit_chunk(seg, room):
                    if buf and len("\n".join(buf + [piece])) > limit:
                        flush()
                    buf.append(piece)
        flush(final=True)
        return self.merge_tiny(parts, forced, limit)

    def merge_tiny(
        self, parts: list[str], forced: list[bool], limit: int, floor: int = 500
    ) -> list[str]:
        """혼자 나가면 어색한 짧은 조각을 앞 조각에 붙인다.

        답변이 마커로 일부러 끊은 자리는 건드리지 않는다.
        표를 쪼개고 남은 몇 줄이 단독 메시지로 나가는 경우만 다시 붙인다.
        """
        out: list[str] = []
        for part, by_marker in zip(parts, forced):
            joinable = out and not by_marker and (len(part) < floor or len(out[-1]) < floor)
            if joinable and len(out[-1]) + len(part) + 1 <= limit:
                out[-1] = out[-1] + "\n" + part
            else:
                out.append(part)
        return [p for p in out if p.strip()]
