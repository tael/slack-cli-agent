"""ReviewTask(ABC) — 답변 사후 점검(부검·디버그 추적·서식 점검) 공통 구조.

원본 bot.py 는 이 세 점검을 `run_postmortem`/`_run_postmortem`,
`run_debug_trace`/`_run_debug_trace`, `run_format_review`/`_run_format_review`
로 거의 같은 순서를 세 번 반복해 짰다. 공통 순서는 다음과 같다.

    1. 중복 확인 (이미 점검한 대상이면 그만둔다)
    2. 점검 시작을 기록한다
    3. 지목한 메시지를 조회한다 — 없으면 포기하고 기록을 지운다
    4. 처리중 표시를 단다
    5. 대화록·그 답을 만든 실행 기록을 모은다
    6. 하위 클래스가 만든 프롬프트로 모델을 부른다
    7. 실패했으면 안내를 올리고 기록을 지운다
    8. 구분선으로 요약과 상세를 가른다(부검만 구분선이 없으면 재시도한다)
    9. 요약은 채널에, 상세는 그 스레드에 올린다
    10. 완료를 기록한다

이 흐름을 여기서 한 번만 짜고, 하위 클래스는 `build_prompt()`/`build_header()`
만 채운다.

각 협력자는 Protocol 로 받는다. Slack SDK·엔진 실행 세부(EngineRequest 조립에
필요한 Profile·RuntimeSettings 연결)는 이 패키지가 알 이유가 없는 다른 계층의
일이라, `EngineCaller` 하나로 그 경계를 좁혔다 — 실제 프로덕션에서는 이
Protocol 을 구현하는 얇은 어댑터가 `engine.runner.EngineRunner.run()` 을
감싼다.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.review.ledger import ReviewLedger

# 원본은 부검·디버그 추적·서식 점검 셋 모두 같은 구분선 리터럴("===상세===")을
# 따로 정의해 썼다. 세 파일이 같은 문자열을 각자 갖고 있으면 한쪽만 고쳐도
# 조용히 어긋나므로 상수 하나로 모은다.
REVIEW_SPLIT = "===상세==="


def cell(value: Any) -> str:
    """표 한 칸에 넣을 수 있게 다듬는다.

    값에 파이프나 줄바꿈이 섞이면 그 줄부터 표가 어긋나 아래가 통째로 깨진다.
    원본 `cell()` 그대로다.
    """
    return str(value).replace("|", "/").replace("\n", " ").strip() or "-"


def as_table(rows: list[tuple[Any, Any]], head: tuple[str, str] = ("항목", "값")) -> str:
    """(라벨, 값) 목록을 파이프 표로 만든다. 빈 목록이면 빈 문자열이다.

    원본 `as_table()` 그대로다.
    """
    if not rows:
        return ""
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return "\n".join(lines)


def model_effort_cell(record: Mapping[str, Any] | None) -> str:
    """트러블슈팅 표에 넣을 "모델 / effort" 칸을 만든다.

    넘긴 값과 실제로 돈 모델이 다르면 둘 다 보인다. 원본 `model_effort_cell()`
    그대로다.
    """
    record = record or {}
    asked = record.get("model") or "?"
    actual = record.get("model_actual")
    shown = asked if not actual or actual == asked else f"{asked} (실제 {actual})"
    return f"{shown} / {record.get('effort') or '?'}"


@dataclass(frozen=True)
class ReviewTarget:
    """점검 대상 하나. 리액션 디스패치(이 패키지 밖의 일)가 채워 넘긴다."""

    channel: str
    ts: str
    by_user: str
    # load_channels() 로 얻는 채널 표시 이름. 채널 레지스트리는 이 패키지의
    # 책임이 아니므로 호출부가 이미 조회해 채워 넘긴다.
    channel_name: str
    # 그 채널이 리치 표기(마크다운 블록)인지 평문인지. is_rich() 의 결과를
    # 호출부가 채워 넘긴다.
    rich: bool


# 아래 계약은 전부 `runtime_checkable` 이다. 그래야 조립 계층이 넣은 객체가
# 계약을 만족하는지 시험으로 확인할 수 있다. 붙이지 않으면 `isinstance` 가
# TypeError 를 내서, 계약을 어긴 객체가 실행 시점까지 드러나지 않는다.
@runtime_checkable
class MessageLookupPort(Protocol):
    """지목한 메시지 원문을 조회한다."""

    def find(self, channel: str, ts: str) -> Mapping[str, Any] | None: ...


@runtime_checkable
class TranscriptPort(Protocol):
    """스레드 대화록을 만든다."""

    def transcript(self, channel: str, thread_ts: str) -> str: ...


@runtime_checkable
class AnswerRecordFinderPort(Protocol):
    """지목한 답변을 만든 감사 기록을 찾는다."""

    def find(self, channel: str, thread_ts: str, text: str) -> Mapping[str, Any] | None: ...


@runtime_checkable
class ReactionPort(Protocol):
    """처리중 표식을 달고 지운다."""

    def mark_processing(self, channel: str, ts: str) -> None: ...
    def clear_processing(self, channel: str, ts: str) -> None: ...


@runtime_checkable
class PermalinkPort(Protocol):
    """메시지 하나의 영구 링크를 얻는다. 실패하면 빈 문자열."""

    def permalink(self, channel: str, ts: str) -> str: ...


@runtime_checkable
class PublisherPort(Protocol):
    """트러블슈팅 채널에 글을 올린다. 실패하면 None."""

    def post(self, channel: str, thread_ts: str | None, text: str, *, rich: bool) -> str | None: ...


@runtime_checkable
class EngineCaller(Protocol):
    """모델을 한 턴 부른다.

    EngineRequest 조립(workdir·model·effort·시스템 프롬프트 선택)은 Profile 과
    RuntimeSettings 를 아는 조립 계층의 일이다. 이 패키지는 프롬프트 문자열과
    세션 ID 만 넘기고 EngineResponse 를 그대로 돌려받는다.
    """

    def run(self, prompt: str, session_id: str, resume: bool) -> EngineResponse: ...


class ReviewTask(ABC):
    """리액션 후처리(부검·디버그 추적·서식 점검) 하나의 공통 구조."""

    # `reviews.kind` 에 쓰는 값. 하위 클래스가 정한다.
    log_name: ClassVar[str]
    # 이 점검을 트리거하는 리액션 이모지 이름. 디스패치는 이 패키지 밖의 일이라
    # 여기서는 표시용으로만 쓴다.
    emoji: ClassVar[str]

    def __init__(
        self,
        *,
        ledger: ReviewLedger,
        message_lookup: MessageLookupPort,
        transcript: TranscriptPort,
        answer_finder: AnswerRecordFinderPort,
        reactions: ReactionPort,
        permalinks: PermalinkPort,
        publisher: PublisherPort,
        engine: EngineCaller,
        troubleshoot_channel: str,
    ) -> None:
        self._ledger = ledger
        self._message_lookup = message_lookup
        self._transcript = transcript
        self._answer_finder = answer_finder
        self._reactions = reactions
        self._permalinks = permalinks
        self._publisher = publisher
        self._engine = engine
        self._troubleshoot_channel = troubleshoot_channel

    @abstractmethod
    def build_prompt(self, target: ReviewTarget, *, transcript: str, flagged: str, question: str) -> str:
        """모델에게 보낼 프롬프트를 만든다."""

    @abstractmethod
    def build_header(self, target: ReviewTarget, record: Mapping[str, Any] | None, link: str) -> str:
        """보고 앞에 붙일 표·안내 머리말을 만든다."""

    def split_marker(self) -> str:
        return REVIEW_SPLIT

    def retry_on_missing_split(self) -> bool:
        """구분선이 없을 때 형식을 지켜 다시 쓰게 할지.

        원본에서 부검만 재시도했다 — 디버그 추적과 서식 점검은 경고만 남기고
        그대로 올렸다.
        """
        return False

    def missing_split_prompt(self) -> str:
        """구분선 없이 나온 결과를 다시 쓰게 하는 요청.

        `retry_on_missing_split()` 이 True 인 하위 클래스만 구현하면 된다.

        직전 출력을 인자로 받지 않는다. 재요청이 같은 세션을 이어받아 모델이
        그것을 이미 갖고 있고, 이어받기가 실패하면 재요청 자체가 실패로 끝나
        결과를 안 쓴다. 원본은 인자를 받되 프롬프트에 넣지 않아, 읽는 사람이
        넣는다고 착각할 여지가 있었다.
        """
        raise NotImplementedError

    # 실패·중단 문구는 점검 종류마다 다르다(부검은 "경단", 디버그는 "뇌",
    # 서식 점검은 "연필"을 다시 붙이라고 안내한다).
    def stopped_title(self) -> str:
        return f"{self.log_name} 중단"

    def not_ok_message(self, body: str) -> str:
        return f"점검을 마치지 못했습니다. {body}"

    def retry_hint(self) -> str:
        return "다시 리액션을 붙이면 재시도합니다."

    def run(self, target: ReviewTarget) -> None:
        """공통 흐름 — 중복 확인, 기록, 실행, 분할 게시, 실패 시 기록 되돌리기."""
        if self._ledger.is_reviewed(self.log_name, target.channel, target.ts):
            return
        self._ledger.begin(self.log_name, target.channel, target.ts, by=target.by_user)
        try:
            self._execute(target)
        except Exception as exc:  # noqa: BLE001 — 원본도 무슨 예외든 되돌리고 알린다
            self._ledger.drop(self.log_name, target.channel, target.ts)
            self._reactions.clear_processing(target.channel, target.ts)
            self._publisher.post(
                self._troubleshoot_channel,
                None,
                f"*{self.stopped_title()}*\n대상 : {target.channel}:{target.ts}\n"
                f"사유 : {exc}\n\n{self.retry_hint()}",
                rich=True,
            )

    def _execute(self, target: ReviewTarget) -> None:
        msg = self._message_lookup.find(target.channel, target.ts)
        if msg is None:
            self._ledger.drop(self.log_name, target.channel, target.ts)
            return

        self._reactions.mark_processing(target.channel, target.ts)

        thread_ts = msg.get("thread_ts") or target.ts
        flagged = (msg.get("text") or "").strip()
        transcript = self._transcript.transcript(target.channel, thread_ts)
        record = self._answer_finder.find(target.channel, thread_ts, flagged)
        question = (record or {}).get("question") or ""

        prompt = self.build_prompt(target, transcript=transcript, flagged=flagged, question=question)
        session_id = str(uuid.uuid4())
        response = self._engine.run(prompt, session_id, False)

        link = self._permalinks.permalink(target.channel, target.ts)
        header = self.build_header(target, record, link)
        self._reactions.clear_processing(target.channel, target.ts)

        if not response.ok:
            self._publisher.post(
                self._troubleshoot_channel, None,
                header + self.not_ok_message(response.body) + f"\n\n{self.retry_hint()}",
                rich=True,
            )
            self._ledger.drop(self.log_name, target.channel, target.ts)
            return

        body = response.body
        marker = self.split_marker()
        if marker not in body and self.retry_on_missing_split():
            retry = self._engine.run(self.missing_split_prompt(), session_id, True)
            if retry.ok and marker in retry.body:
                body = retry.body

        if marker in body:
            summary, detail = body.split(marker, 1)
        else:
            summary, detail = body, ""

        parent = self._publisher.post(self._troubleshoot_channel, None, header + summary.strip(), rich=True)
        if detail.strip():
            if parent:
                self._publisher.post(self._troubleshoot_channel, parent, detail.strip(), rich=True)
            else:
                self._publisher.post(self._troubleshoot_channel, None, detail.strip(), rich=True)

        report_link = ""
        if parent:
            report_link = self._permalinks.permalink(self._troubleshoot_channel, parent)

        self._ledger.complete(
            self.log_name, target.channel, target.ts,
            by=target.by_user, link=link, report=report_link,
        )
