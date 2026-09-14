"""분할 결과 검증과 안전 낙하.

원본 bot.py 의 verify_chunks, safe_fallback,
separate_tables, blocks_rejected 를 그대로 옮겼다 (이식 분류 — 함수 본문은
수정하지 않는다).
"""

from __future__ import annotations

import re

from slack_cli_agent.config.settings import RuntimeSettings

from .blocks import SPLIT_MARKER

TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
FENCE_LINE = re.compile(r"^\s*```")


class SplitVerifier:
    """분할 결과를 발송 전에 점검하고, 실패하면 안전한 형태로 낮춘다."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def verify_chunks(self, text: str, chunks: list[str]) -> list[str]:
        """나눈 결과를 발송 전에 점검한다.

        덩어리마다 열고 닫는 짝이 맞아야 렌더가 산다.
        표는 열 이름 행으로 시작해야 하고, 코드블록은 펜스가 짝을 이뤄야 한다.
        내용이 사라지지 않았는지도 함께 본다. 형식보다 내용 보존이 앞선다.
        """
        limit = self._settings.markdown_block_limit
        problems = []
        source = text.replace(SPLIT_MARKER, "")
        kept = sum(len(c) for c in chunks)
        missing = len(source.strip()) - kept
        # 마커와 줄바꿈을 걷어내면 몇 글자는 줄어든다. 비율만 보면 짧은 답변에서 오탐이 난다.
        if missing > 200 and kept < len(source.strip()) * 0.97:
            problems.append(f"내용 유실 의심 {len(source)} -> {kept}")
        for i, part in enumerate(chunks):
            if len(part) > limit:
                problems.append(f"{i}번 조각 상한 초과 {len(part)}")
            if part.count("```") % 2:
                problems.append(f"{i}번 조각 코드블록 펜스 짝 안 맞음")
            # 조각 첫머리만 보면 가운데에서 시작하는 표를 놓친다.
            # 표가 시작되는 자리마다 열 이름 행이 뒤따르는지 본다.
            lines = [x for x in part.split("\n") if x.strip()]
            prev_row = False
            for j, line in enumerate(lines):
                is_row = line.lstrip().startswith("|")
                if is_row and not prev_row:
                    nxt = lines[j + 1].lstrip() if j + 1 < len(lines) else ""
                    if not nxt.startswith("|-"):
                        problems.append(f"{i}번 조각 표 열 이름 행 없음")
                        break
                prev_row = is_row
            if SPLIT_MARKER in part:
                problems.append(f"{i}번 조각 마커 잔존")
        return problems

    def safe_fallback(self, text: str, limit: int | None = None) -> list[str]:
        """점검에 걸렸을 때 쓰는 마지막 경로.

        형식은 일부 포기하더라도 내용은 잃지 않는다. 줄 경계로만 자른다.
        """
        if limit is None:
            limit = self._settings.markdown_block_limit
        parts, buf = [], []
        for line in text.replace(SPLIT_MARKER, "").split("\n"):
            if buf and len("\n".join(buf + [line])) > limit:
                parts.append("\n".join(buf))
                buf = []
            buf.append(line)
        if buf:
            parts.append("\n".join(buf))
        return parts or [text[:limit]]

    def separate_tables(self, text: str) -> str:
        """표와 그 앞뒤 문단 사이에 빈 줄을 넣는다.

        슬랙은 마크다운을 자기 표기로 옮기면서 표 바로 다음 줄을
        빈 줄이 없으면 표의 한 행으로 삼는다.

        2026-08-27 확인. 같은 표에 같은 문단을 붙여 일곱 가지로 보냈다.
        줄바꿈 하나만 둔 것은 표가 4행이 되어 문단이 마지막 행으로 들어갔고,
        빈 줄, 구분선, 헤딩, 불릿, 인용을 둔 것은 전부 표 3행에 문단이 따로 섰다.
        가른 것은 빈 줄 하나였다.

        지침으로 "표에는 데이터 행만 넣는다" 를 걸어 뒀지만 그것만으로는 안 된다.
        모델이 문단을 표 밖에 써도 줄바꿈 하나로 붙여 두면 슬랙이 도로 끌어들인다.
        나가기 직전에 코드로 빈 줄을 보장한다.

        코드 울타리 안은 건드리지 않는다. 표처럼 생긴 예시일 수 있다.
        """
        lines = (text or "").split("\n")
        out = []
        in_fence = False
        prev_table = False
        for line in lines:
            if FENCE_LINE.match(line):
                in_fence = not in_fence
                out.append(line)
                prev_table = False
                continue
            if in_fence:
                out.append(line)
                continue

            is_table = bool(TABLE_LINE.match(line))
            blank = not line.strip()

            if is_table and not prev_table and out and out[-1].strip():
                # 표가 시작하는데 바로 위가 글이면 띄운다
                out.append("")
            elif prev_table and not is_table and not blank:
                # 표가 끝났는데 바로 아래가 글이면 띄운다
                out.append("")

            out.append(line)
            prev_table = is_table
        return "\n".join(out)

    def blocks_rejected(self, exc: Exception) -> bool:
        """슬랙이 리치 표기 자체를 거절했는지 본다.

        예외 형에 기대지 않고 응답에 담긴 사유로 판별한다.
        라이브러리를 올려도 판별이 어긋나지 않는다.
        """
        res = getattr(exc, "response", None)
        try:
            return (res or {}).get("error") == "invalid_blocks"
        except Exception:
            return "invalid_blocks" in str(exc)
