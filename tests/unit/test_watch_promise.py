"""WATCH 없는 약속 탐지 (마메치 test_bot.py 2602-2656 의 입력·기대값 이식).

마메치 bot.py:1632-1664 의 넓힌 어휘와 인용·코드 가림을 옮긴 것을 고정한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.guard.base import GuardContext
from slack_cli_agent.guard.watch import (
    PROMISE_WITHOUT_WATCH_RE,
    WatchPromiseGuard,
    promise_search,
    promise_without_watch_prompt,
)
from slack_cli_agent.prompt.sections import SILENT_MARK
from tests.unit.test_pipeline import build_pipeline, fail_response, make_ctx, ok_response

CASES = [
    # 2026-09-01 사건 문장
    ("배포 반영 대기 중입니다. 완료되면 이어서 재실행하고 보고할게요.", True),
    ("재실행 완료를 백그라운드로 지켜보는 중입니다. 끝나면 결과와 함께 다시 보고할게요.", True),
    ("백그라운드로 확인 중입니다. 완료되면 결과와 함께 보고할게요.", True),
    # 2026-09-19 구 패턴이 통과시킨 문장
    ("코덱스 재검토가 돌고 있습니다. 결과가 나오면 확인해 올리겠습니다", True),
    (("결과가 나오는 대로 두 안이 각각 몇 건을 해소하고 몇 건 얼마를 남기는지 "
      "표로 올리겠습니다."), True),
    ("두 세션 결과가 나오는 대로 각각 올리겠습니다.", True),
    ("회신이 들어오는 대로 블로커를 정리해 올리겠습니다.", True),
    ("결과가 나오면 단계별 결과와 차액 대조 표를 올리겠습니다.", True),
    (("재기동 끝나면 아무 말이나 한마디 주시면 그 턴에 교체까지 끝내고 "
      "결과를 올리겠습니다."), True),
    # 2026-11-09 조건 없이 앞으로 할 일을 선언한 문장
    ("이후 조사와 대조 작업도 codex 워커 위주로 나눠 돌리겠습니다.", True),
    ("앞으로 나머지 항목도 이어서 진행하겠습니다.", True),
    # 정상 답
    ("PR 생성까지 완료했습니다.", False),
    ("이후 단계는 승인 후에 정합니다.", False),
    ("머지 확인했습니다.", False),
    ("확인이 더 필요하면 다시 말씀해 주세요.", False),
    ("착수하실 거면 상태를 진행 중으로 올리고 워크플로 이식부터 진행하겠습니다.", False),
    ("수정과 재기동까지 진행할지 말씀 주시면 그때 실행하겠습니다.", False),
    # 위임만 서술한 답은 어휘로 잡지 않는다
    ("하위 세션 pid 72584 로 실행을 시작했습니다. 지라에 코멘트를 남기도록 지시했습니다.", False),
]


@pytest.mark.parametrize(("text", "want"), CASES)
def test_빈_약속_판정(text: str, want: bool) -> None:
    assert bool(PROMISE_WITHOUT_WATCH_RE.search(text)) is want
    assert bool(promise_search(text)) is want


def test_줄바꿈을_넘어_잡지_않는다() -> None:
    assert promise_search("작업이 끝나면\n\n다음 단계 보고서 형식은 아래와 같습니다.") is None


def test_인용_줄_안의_일상_표현은_약속이_아니다() -> None:
    assert promise_search("> 이야기가 나오면 먼저 말씀 주셔도 좋고\n본문") is None


def test_코드블록_안의_문구는_약속이_아니다() -> None:
    body = "예시입니다.\n```\n끝나면 보고하겠습니다\n```\n이상입니다."
    assert promise_search(body) is None


def test_본문의_약속은_그대로_잡는다() -> None:
    assert promise_search("결과가 나오면 말씀드릴게요") is not None


def test_인용이_있는_답은_재요청하지_않는다() -> None:
    """인용 줄에는 가림 전 정규식도 잡는 구 어휘를 둔다. 가림이 없으면
    재요청이 일어나 실패해야 이 시험이 가림을 고정한다."""
    body = "시뮬레이션 예시입니다.\n> 끝나면 보고하겠습니다\n위 문장은 고객 발화입니다."
    result = WatchPromiseGuard().apply(body, GuardContext())
    assert result.rerun is None
    assert result.body == body


def test_재시도_실패_시_인용_뒤의_본문_약속에서_자른다() -> None:
    """인용 안의 표현에서 자르면 인용 이후 본문이 통째로 사라진다.
    가린 본문에서 찾은 위치(본문 약속의 조건절 시작)에서 자른다."""
    body = (
        "> 끝나면 보고하겠습니다\n"
        "위 인용은 예시입니다.\n"
        "재실행이 끝나면 다시 보고하겠습니다."
    )
    result = WatchPromiseGuard().apply(body, GuardContext(is_rewrite_retry=True))
    assert result.body == (
        "> 끝나면 보고하겠습니다\n위 인용은 예시입니다.\n재실행이\n\n"
        "확인이 더 필요하면 다시 말씀해 주세요."
    )


def test_보정_프롬프트() -> None:
    out = promise_without_watch_prompt("완료되면 결과와 함께 보고할게요.")
    assert "완료되면 결과와 함께 보고할게요." in out
    assert "[[WATCH:" in out
    assert "다시 물어봐" in out
    assert "새로 지어내지 않는다" in out


PROMISE_BODY = "인용 예시입니다.\n> 끝나면 보고하겠습니다\n재실행을 걸었습니다. 끝나면 다시 보고하겠습니다."
CUT_BODY = "인용 예시입니다.\n> 끝나면 보고하겠습니다\n재실행을 걸었습니다.\n\n확인이 더 필요하면 다시 말씀해 주세요."


@pytest.mark.parametrize(
    "rerun",
    [fail_response("timeout"), ok_response(body=f"{SILENT_MARK} 답할 것이 없습니다")],
    ids=["재작성_실패", "재작성_침묵"],
)
def test_재작성이_안_되면_약속_위치에서_자른다(rerun, tmp_path: Path) -> None:
    """마메치 bot.py:8249-8255 else 분기. 재작성 재실행이 실패하거나 침묵해도
    태그 없는 약속을 그대로 게시하지 않고 잘라 표준 문구를 붙인다."""
    pipeline, deps = build_pipeline(
        responses=[ok_response(body=PROMISE_BODY), rerun],
        guards=[WatchPromiseGuard()],
        tmp_path=tmp_path,
    )
    outcome = pipeline.handle(make_ctx())

    assert outcome.ok is True
    assert len(deps["runner"].calls) == 2
    assert deps["publisher"].posted[0]["text"] == CUT_BODY


def test_감시_표식_아래의_수신자_표식을_지우지_않는다() -> None:
    """2026-11-09 private-taeil 20:31 : ASK / WATCH / RECIPIENT 순서로 끝난 답에서
    감시 표식을 떼며 수신자 표식까지 지워 답이 게시되지 않았다."""
    body = (
        "옵션만 정해 주시면 진행하겠습니다.\n\n[[ASK]]\n"
        "[[WATCH: jobs/exit6 파일이 생겼는지와 그 값, 착수 조건(B1) 충족 여부를 보고]]\n"
        "[[RECIPIENT:ASKER]]"
    )
    result = WatchPromiseGuard().apply(body, GuardContext())
    assert result.detail["watch_desc"] == "jobs/exit6 파일이 생겼는지와 그 값, 착수 조건(B1) 충족 여부를 보고"
    assert result.body == "옵션만 정해 주시면 진행하겠습니다.\n\n[[ASK]]\n[[RECIPIENT:ASKER]]"


def test_감시_표식이_마지막_줄이면_그대로_떼어_낸다() -> None:
    body = "확인했습니다.\n[[WATCH@2026-11-12: dag x run y 상태]]"
    result = WatchPromiseGuard().apply(body, GuardContext())
    assert result.detail == {"watch_desc": "dag x run y 상태", "watch_due": "2026-11-12"}
    assert result.body == "확인했습니다."
