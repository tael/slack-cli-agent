"""마크다운 습관을 슬랙 mrkdwn 표기로 바로잡는 변환기.

원본 bot.py 의 to_mrkdwn, tables_to_bullets 를 그대로
옮겼다 (이식 분류 — 함수 본문은 수정하지 않는다).
"""

from __future__ import annotations

import re


class MarkdownConverter:
    """마크다운 원문을 슬랙 mrkdwn 으로 변환한다."""

    def tables_to_bullets(self, text: str) -> str:
        """마크다운 표를 불릿으로 편다.

        슬랙은 표를 렌더하지 않아 파이프와 구분선이 글자 그대로 노출된다.
        헤더와 구분선은 버리고 각 행을 불릿 한 줄로 만든다.
        """
        lines = text.split("\n")
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            is_row = line.strip().startswith("|") and line.strip().endswith("|")
            divider = (
                i + 1 < len(lines)
                and re.fullmatch(r"\s*\|[\s:|-]+\|\s*", lines[i + 1] or "")
            )
            if not (is_row and divider):
                out.append(line)
                i += 1
                continue

            # 표를 만났다. 헤더와 구분선을 건너뛰고 본문 행만 편다.
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                cells = [c for c in cells if c]
                if cells:
                    out.append("- " + " ".join(cells))
                i += 1
        return "\n".join(out)

    def to_mrkdwn(self, text: str) -> str:
        """마크다운 습관을 슬랙 mrkdwn 으로 바로잡는다.

        프롬프트로만 막으면 이중 별표와 헤딩이 계속 새어 나온다.
        슬랙은 이중 별표를 굵게로 읽지 않고 문자 그대로 보여준다.
        코드블록 안은 손대지 않는다.
        """
        parts = text.split("```")
        for i in range(0, len(parts), 2):   # 짝수 조각만 코드블록 밖이다
            b = parts[i]
            b = re.sub(r"\*\*\*(.+?)\*\*\*", r"*\1*", b)      # 굵은 기울임도 굵게로
            b = re.sub(r"\*\*(.+?)\*\*", r"*\1*", b)            # 이중 별표를 단일로
            b = re.sub(r"__(.+?)__", r"_\1_", b)                  # 이중 밑줄을 단일로
            b = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", r"*\1*", b, flags=re.MULTILINE)  # 헤딩을 굵게로
            b = re.sub(r"^\s*[•◦▪]\s+", "- ", b, flags=re.MULTILINE)      # 가운뎃점을 하이픈으로
            b = re.sub(r"^\s*[-*]{3,}\s*$", "", b, flags=re.MULTILINE)    # 구분선 제거
            # 표를 먼저 편다. 링크를 먼저 바꾸면 그 안의 파이프가 셀 구분자로 오인된다.
            b = self.tables_to_bullets(b)
            b = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", b)  # 마크다운 링크
            parts[i] = b
        return "```".join(parts)
