"""LateAddendumChecker — 발송 직전 스레드 재확인.

원본 `late_thread_addendum`, `late_addendum_prompt`, `mark_thread_consumed`,
`_thread_consumed` 를 옮겼다.

답을 만드는 데는 시간이 걸린다. 그 사이 스레드 아래로 사람 말이 새로
달릴 수 있다. 만든 답을 그 사실을 모른 채 그대로 올리면 이미 지나간
물음에 답하는 꼴이 된다. 발송 직전에 그 사이 달린 말이 있는지 다시 확인해
있으면 담아 다시 내야 한다.

슬랙 조회는 여기서 직접 하지 않는다. `reliability.ports.HistoryReader`
계약을 주입받아 그 구현(예: `slack.history_port.SlackHistoryPort`)을 그대로
쓴다. 이 모듈은 그 결과를 걸러 대화록 문자열로 만드는 것만 한다.

2026-09-09 재발방지 : 한 스레드에 사람 말이 빠르게 이어질 때 그 말을 소비하는
경로가 둘이었다. 발송 전 재확인이 앞 실행에 그 말을 흡수했는데, 대기줄이 같은
말을 별도 실행으로 또 돌렸다. 두 경로가 공유 상태 없이 각자 판단해 답변
세 개 중 뒤 두 개가 앞 답변의 재서술이 됐다. `ThreadConsumption` 이 흡수한
시각을 적어 두 경로가 같은 기준을 보게 한다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.observability.notices import NoticeCatalog
from slack_cli_agent.reliability.ports import HistoryReader
from slack_cli_agent.slack.identity import BotIdentity
from slack_cli_agent.slack.message_kind import MessageKind
from slack_cli_agent.slack.speaker import SpeakerNamer

from ..core.timezones import KST


class ThreadConsumption:
    """스레드별로 어느 시각까지 소화했는지 기록한다.

    "이 스레드에서 ts 까지는 이미 답에 담았다" 는 사실을 담는 단일 값이다.
    뒤로 물러나지 않는다 — 나중에 더 이른 시각으로 적으려 해도 무시한다.
    여러 실행이 동시에 같은 스레드를 만질 수 있어 잠금으로 감싼다.
    """

    def __init__(self) -> None:
        self._consumed: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, thread_ts: str, ts: str | float | None) -> None:
        """이 스레드에서 ts 까지 소화했다고 적는다. 뒤로 물러나지 않는다."""
        if not ts:
            return
        with self._lock:
            cur = self._consumed.get(thread_ts, 0.0)
            self._consumed[thread_ts] = max(cur, float(ts))

    def consumed_ts(self, thread_ts: str) -> float:
        """이 스레드에서 지금까지 소화한 것으로 기록된 마지막 시각.

        기록이 없으면 0.0 이다 — 아직 아무것도 소화하지 않은 것과 같다.
        """
        with self._lock:
            return self._consumed.get(thread_ts, 0.0)

    def forget(self, thread_ts: str) -> None:
        """이 스레드의 소화 기록을 지운다.

        그 스레드에서 더 돌 요청이 없을 때 부른다. 다음에 그 스레드가
        다시 쓰이면 새 요청 기준으로 처음부터 판단하게 한다.
        """
        with self._lock:
            self._consumed.pop(thread_ts, None)


class LateAddendumChecker:
    """발송 직전에 스레드 아래로 새로 달린 사람 말이 있는지 본다."""

    def __init__(
        self,
        history: HistoryReader,
        notices: NoticeCatalog,
        name_resolver: Callable[[str], str],
        settings: RuntimeSettings,
        identity: BotIdentity,
        bot_display_name: str,
        owner_user_id: str = "",
        owner_display_name: str = "",
    ) -> None:
        self._history = history
        self._notices = notices
        self._settings = settings
        # 화자 표시는 TranscriptBuilder 와 공유하는 SpeakerNamer 에 위임한다.
        # 지금은 MessageKind.is_human 이 봇 메시지를 미리 걸러 내 봇 분기를 탈
        # 일이 없지만, 그 전제에 기대지 않는다 — 전제가 바뀌면 이 봇의 말이
        # "이름 모르는 봇" 으로 적히고 그 오류는 화면을 볼 때까지 안 드러난다.
        self._speaker = SpeakerNamer(
            name_resolver=name_resolver,
            is_self=identity.is_self,
            bot_display_name=bot_display_name,
            owner_user_id=owner_user_id,
            owner_display_name=owner_display_name,
        )
        # 메시지를 무엇으로 볼지는 한 곳에서 판정한다. 여기 따로 적으면 기준이 갈린다.
        self._kind = MessageKind()

    def check(
        self,
        channel: str,
        thread_ts: str,
        ts: str | float,
        scope: str = "thread",
    ) -> tuple[str, str | None]:
        """그 사이 스레드 아래로 새로 달린 말을 읽어 대화록으로 만든다.

        (새로 달린 기록, 그 마지막 메시지 시각) 을 돌려준다. 없으면
        ("", None) 이다. 조회가 판정 불가(None)로 끝나도 같다 — 있는지
        없는지 모르는 상태를 "없다" 로 취급해 그대로 넘어간다. 원본이
        조회 실패를 경고 로그만 남기고 조용히 넘어간 것과 같은 태도다.
        """
        base_ts = float(ts)
        limit = self._settings.history_max_msgs
        if scope == "channel":
            msgs = self._history.read_history(channel, base_ts, limit)
            if msgs is None:
                return "", None
        else:
            msgs = self._history.read_thread(channel, thread_ts, limit)

        lines: list[str] = []
        latest: str | None = None
        for m in msgs:
            if not self._kind.is_human(m):
                continue
            mts = float(m.get("ts", 0) or 0)
            if mts <= base_ts:
                continue
            text = (m.get("text") or "").strip()
            if not text or self._notices.is_notice(text):
                continue
            when = datetime.fromtimestamp(mts, KST).strftime("%H:%M:%S")
            who = self._speaker.speaker_of(m)
            lines.append(f"[{when} {who}]\n{text}")
            latest = m.get("ts")
        return "\n\n".join(lines), latest


def late_addendum_prompt(addendum: str) -> str:
    """발송 전 재확인에서 새로 찾은 말을 다시 답하라는 요청으로 감싼다.

    앞 답이 아직 게시 전이라는 사실을 먼저 못박는다. 이 자리는 답을 다 만들고
    아직 올리지 않은 상태다. 사람은 그 답을 본 적이 없다.

    2026-09-11 에 이 전제가 빠져 있어 사고가 났다. 프롬프트가 "새로 달린 말이
    찾는 것을 방금 답이 이미 담고 있으면 어디에 있는지 가리키고 끝낸다"고
    지시했는데, 모델이 그대로 따라 완성된 목록을 버리고 위치만 가리켰다.
    가리킬 대상이 아직 슬랙에 없어서 사람은 빈손으로 짧은 안내만 받았다.
    그 지시는 앞 답이 이미 사람에게 보인 자리에서만 맞다.
    """
    return (
        "방금 답을 만들었고 아직 슬랙에 올리지 않았다.\n"
        "그 답은 사람이 본 적이 없다. 이 대화 어디에도 없다.\n"
        "올리기 직전에 이 스레드 아래로 말이 새로 달린 것을 발견했다.\n"
        "대괄호 안이 시각과 그 말을 한 사람이다.\n\n"
        "===== 새로 달린 말 =====\n\n"
        f"{addendum}\n\n"
        "===== 여기까지다 =====\n\n"
        "지금 낼 답 하나가 이 스레드에 올라가는 전부다.\n"
        "방금 만든 답의 내용을 빠짐없이 담고, 새로 달린 말에 대한 답을 더한다.\n"
        "앞 답이 이미 있다고 보고 그리로 가리키지 않는다. 가리킬 곳이 없다.\n"
        "목록, 표, 링크, 티켓 번호를 줄이거나 생략하지 않는다.\n"
        "방금 낸 답이 새로 달린 말과 어긋나는 부분만 그에 맞게 고친다.\n"
        "달라질 것이 없으면 방금 만든 답을 그대로 다시 낸다."
    )
