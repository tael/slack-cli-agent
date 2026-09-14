"""발신 직전 본문 끝에 붙는 표식 정규식.

`ELAPSED_LINE`, `ELAPSED_MODEL_LINE` 은 원래 `guard/watch.py` 에 있었다.
가드·게이트·발신(publisher) 세 계층이 모두 이 정규식을 쓰는데, 슬랙 계층이
가드 계층을 import 할 이유가 없어 공용 위치인 `core` 로 옮겼다. `guard/watch.py`
는 이 모듈에서 재노출해 기존 import 경로를 유지한다.
"""

from __future__ import annotations

import re

# 원본이 특정 채널 한정으로 답변 끝에 붙이던 소요 시간 줄. 본문이 아니라 덧붙인
# 표식이라 되물음 판정에서 걷어낸다. 2026-09-02 : 이 줄이 끝에 붙어 있어
# "지금 반영할까요." 뒤에 온 "네" 가 맞장구로 걸러졌다.
ELAPSED_LINE = re.compile(r"(?:\n\s*>\s*걸린 시간 : \s*\d+초\s*)+$")
# 모델이 앞 대화를 흉내 내 본문 끝에 같은 줄을 써 넣는 경우를 지운다.
# 걸린 시간 줄과 같은 이유다. 예전 표기 "실행 엔진" 도 함께 지운다.
ELAPSED_MODEL_LINE = re.compile(r"\n*>\s*실행\s*(모델|엔진)\s*:.*$", re.MULTILINE)

__all__ = ["ELAPSED_LINE", "ELAPSED_MODEL_LINE"]
