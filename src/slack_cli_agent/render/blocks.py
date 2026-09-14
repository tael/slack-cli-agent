"""Block Kit 조립 보조 — 마커 정리, 미리보기, 보조 줄 분리.

원본 bot.py 의 clean_markers, preview, split_context 를
그대로 옮겼다 (이식 분류 — 함수 본문은 수정하지 않는다).

`SPLIT_MARKER` 는 답변이 스스로 표시한 분할 경계다. `render/splitter.py`,
`render/verifier.py` 도 같은 값을 참조한다 — 이 모듈에서 임포트해 쓴다.
"""

from __future__ import annotations

import re

SPLIT_MARKER = "<<<SPLIT>>>"


class BlockBuilder:
    """미리보기·보조 줄·마커 정리를 담당한다.

    `bot_display_name` 은 원본의 전역 상수 `BOT_DISPLAY_NAME` 을 주입 가능하게
    바꾼 것이다 — 회사 결합 제거 대상이라 봇마다 다른 이름을 쓸 수 있어야 한다.
    """

    # 보조 줄로 내릴 인용은 짧은 것만 본다.
    # 본문이 스펙 원문 인용으로 끝나는 답도 있어서, 길이를 재지 않으면
    # 근거로 인용한 원문이 작은 글씨로 내려가 읽는 쪽이 본문과 구분하지 못한다.
    CONTEXT_MAX_LINES = 3
    CONTEXT_MAX_CHARS = 120

    def __init__(self, bot_display_name: str) -> None:
        self._bot_display_name = bot_display_name

    def clean_markers(self, text: str) -> str:
        """표나 코드블록 한가운데 찍힌 마커를 지운다.

        거기서 끊으면 열 이름 행이 없는 조각이 생겨 표가 파이프 글자로 노출된다.
        줄 단위로 덩어리를 나누기 전에 지워야 한다.
        나눈 뒤에 지우면 이미 표가 두 동강 난 상태다.
        """
        lines = text.split("\n")
        out, in_fence = [], False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue
            if stripped == SPLIT_MARKER:
                if in_fence:
                    continue
                prev = next((x.strip() for x in reversed(lines[:i]) if x.strip()), "")
                nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
                if prev.startswith("|") and nxt.startswith("|"):
                    continue
            out.append(line)
        return "\n".join(out)

    def preview(self, text: str) -> str:
        """알림 미리보기와 검색 색인에 쓸 한 줄을 뽑는다.

        blocks 를 넘기면 text 는 화면에 안 보이고 알림에만 쓰인다.
        비워 두면 알림에 본문이 없다고 뜨므로 첫 문장을 잘라 넣는다.
        알림은 서식을 렌더하지 않으므로 강조 기호를 걷어낸다.
        """
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith(("#", "|", ">", "```", "---")):
                continue
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)   # 링크는 라벨만 남긴다
            line = re.sub(r"[*_`~]", "", line)                       # 강조 기호는 알림에서 글자로 보인다
            line = line.lstrip("-+ ").strip()
            if line:
                return line[:150]
        return self._bot_display_name + " 답변"

    def split_context(self, text: str) -> tuple[str, str]:
        """본문 끝의 짧은 인용 줄을 보조 줄로 떼어낸다.

        걸린 시간, 기준시각 같은 것은 답의 내용이 아니라 답에 붙는 표시다.
        본문과 같은 크기로 나가면 마지막 문장처럼 읽힌다.
        슬랙 context 블록으로 내리면 작은 회색 글씨가 되어 본문과 구분된다.

        돌려주는 값은 (본문, 보조 줄) 두 짝이다. 떼어낼 것이 없으면 보조 줄은 빈 문자열이다.
        """
        lines = text.rstrip().split("\n")
        note = []
        while lines and lines[-1].lstrip().startswith(">"):
            note.insert(0, lines.pop().lstrip()[1:].strip())
            if len(note) > self.CONTEXT_MAX_LINES:
                return text, ""
        if not note:
            return text, ""
        joined = "\n".join(note)
        if len(joined) > self.CONTEXT_MAX_CHARS:
            return text, ""
        return "\n".join(lines).rstrip(), joined
