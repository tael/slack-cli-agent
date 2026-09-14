"""상태 표식(리액션) 관리.

원본 이 봇은 처리 상태를 리액션으로 알린다. 사람이 지금 어떤 상태인지
반응 하나로 알 수 있어야 한다는 것이 원본의 설계다.

    eyes              받아서 처리 중
    hourglass         앞 요청이 끝나기를 기다리는 중
    white_check_mark  답을 냈다
    x                 처리에 실패했다
    zipper_mouth_face 답하지 않기로 했다
    mag               감시 큐로 넘어갔다 (완료 아님)

eyes·hourglass·x 는 도중에 프로세스가 죽어도 그대로 남는다. 끝났다는 뜻이
아니므로 되짚기 대상으로 남긴다. 사람이 직접 white_check_mark 를 달아도
같게 본다 — 손으로 넘길 수단이 되기 때문이다.

리액션 API 실패는 원본과 같이 조용히 넘긴다. 표식 하나 실패했다고 전체
처리를 멈추지 않는다는 것이 원본의 판단이다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from slack_cli_agent.guard.watch import WATCH_MARK_EMOJI

SILENT_MARK_EMOJI = "zipper_mouth_face"
DONE_EMOJI = frozenset({"white_check_mark", SILENT_MARK_EMOJI})
UNFINISHED_EMOJI = frozenset({"eyes", "hourglass", "x"})

# 사람이 답변에 이 리액션을 남기면 그 답변을 되짚어 후처리한다.
POSTMORTEM_EMOJI = "dango"
DEBUG_TRACE_EMOJI = "brain"
FORMAT_REVIEW_EMOJI = "pencil2"


class ReactionMarker:
    """요청 하나에 대한 처리 상태 표식을 관리한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def add(self, channel: str, ts: str, name: str) -> None:
        try:
            self._client.reactions_add(channel=channel, timestamp=ts, name=name)
        except Exception:  # noqa: BLE001, S110 — 표식은 부가 정보다. 모듈 docstring 대로 조용히 넘긴다
            pass

    def remove(self, channel: str, ts: str, name: str) -> None:
        try:
            self._client.reactions_remove(channel=channel, timestamp=ts, name=name)
        except Exception:  # noqa: BLE001, S110 — 표식은 부가 정보다. 모듈 docstring 대로 조용히 넘긴다
            pass

    def mark_processing(self, channel: str, ts: str) -> None:
        self.add(channel, ts, "eyes")

    def mark_waiting(self, channel: str, ts: str) -> None:
        self.add(channel, ts, "hourglass")

    def _settle(self, channel: str, ts: str, mark: str) -> None:
        """미완료 표식을 걷고 결론 표식을 단다.

        미완료 표식이 둘인 이유는 접수와 처리가 프로세스로 갈려 있기
        때문이다. 접수 쪽이 모래시계를, 처리 쪽이 눈을 단다. 눈만 지우면
        끝난 요청에 모래시계가 남아 사람 눈에는 아직 대기 중으로 보인다.

        달 표식 자신은 지우지 않는다. 실패 표식 x 가 미완료 표식이기도
        해서, 지웠다 다시 달면 슬랙 호출만 한 번 늘어난다.
        """
        for stale in UNFINISHED_EMOJI:
            if stale != mark:
                self.remove(channel, ts, stale)
        self.add(channel, ts, mark)

    def mark_done(self, channel: str, ts: str) -> None:
        self._settle(channel, ts, "white_check_mark")

    def mark_failed(self, channel: str, ts: str) -> None:
        self._settle(channel, ts, "x")

    def mark_silent(self, channel: str, ts: str) -> None:
        """답하지 않기로 한 것도 처리 결과다. 흔적을 남긴다."""
        self._settle(channel, ts, SILENT_MARK_EMOJI)

    def mark_watch(self, channel: str, ts: str) -> None:
        """감시로 넘어간 건은 완료가 아니다. white_check_mark 를 달면 미완료
        복구 대상에서 빠져 되짚기가 다시 보지 않는다."""
        self._settle(channel, ts, WATCH_MARK_EMOJI)

    def mark_resolved_like(self, channel: str, ts: str, mark: str) -> None:
        """다른 건과 같은 결론이 났다고 보고 그 표식을 그대로 남긴다.

        미완료 표식이 남아 있으면 지우고 새 표식을 단다.
        """
        self._settle(channel, ts, mark)

    def reactions_on(self, msg: Mapping[str, Any]) -> set[str]:
        """그 메시지에 달린 이모지 이름들."""
        return {r.get("name") for r in (msg.get("reactions") or []) if r.get("name")}

    def already_handled(self, msg: Mapping[str, Any]) -> bool:
        """이 메시지는 판단이 끝났는가.

        white_check_mark 또는 zipper_mouth_face 가 있으면 끝난 것이다.
        eyes·hourglass·x 는 끝난 것이 아니다.
        """
        return bool(self.reactions_on(msg) & DONE_EMOJI)
