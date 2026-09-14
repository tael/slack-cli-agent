"""IngressService — 슬랙 이벤트 수신부터 작업 큐 등록까지.

여기서 하는 일은 접수뿐이다. **엔진을 부르지 않는다.** 실제 처리는 별도
프로세스인 워커가 큐를 소비해 한다.

흐름은 이렇다.

1. `register(gateway)` 로 `app_mention`·`message`·`reaction_added` 핸들러를
   게이트웨이에 등록한다
2. 들어온 이벤트를 `EventListener` 로 `RequestContext` 로 바꾼다. `None` 이면
   아무것도 하지 않는다
3. `DeduplicationTracker` 로 같은 이벤트의 재전송을 거른다
4. 관리 명령이면 `AdminRouter` 가 그 자리에서 답하고 끝난다. 큐를 거치지 않는다
5. 첨부가 있으면 `AttachmentStore` 로 저장하고 저장된 경로를 컨텍스트에 반영한다
6. `JobQueue.enqueue` 로 큐에 넣는다. 이미 있는 요청이면(False) 리액션을
   달지 않고 조용히 끝난다
7. 새로 들어갔으면 `ReactionMarker.mark_waiting` 으로 대기 표식을 단다
8. 리액션 이벤트는 큐를 거치지 않고 `on_reaction` 콜백으로 그대로 넘긴다

의존은 전부 생성자 주입이다. 슬랙 client 를 여기서 직접 만들지 않는다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from ..admin.command import AdminContext
from ..admin.router import AdminRouter
from ..jobs.ports import JobQueue
from ..reliability.dedup import DeduplicationTracker
from ..slack.attachments import AttachmentStore
from ..slack.gateway import SlackGateway
from ..slack.listener import EventListener
from ..slack.reactions import ReactionMarker
from .context import RequestContext

log = logging.getLogger(__name__)

# 슬랙이 보내는 멘션 표기. 본문 어디에 있든 지운다.
#
# 지우지 않으면 관리 명령이 하나도 맞지 않는다. 명령들은 `text.strip() in
# ("도움말", ...)` 로 판정하는데 멘션 이벤트의 본문은 `<@U123> 도움말`
# 형태로 오기 때문이다. 원본도 본문을 받자마자 이 표기를 지운 뒤
# 관리 명령 판정과 엔진 호출 양쪽에 그 결과를 썼다.
#
# 원본 패턴은 `<@[A-Z0-9]+>` 였다. 슬랙은 표시 이름을 붙여 `<@U123|이름>`
# 형태로 보내는 경우가 있고 그 패턴은 그것을 못 지운다. 꺾쇠 안에 공백이
# 없는 것만 받게 해 두 형태를 다 지우면서 일반 문장은 건드리지 않는다.
MENTION_RE = re.compile(r"<@[^>\s]+>")

# 신원 확인이 필요한 리액션 콜백 시그니처. (이모지, 채널, 메시지시각, 누른사람).
ReactionCallback = Callable[[str, str, str, str], None]
# 관리 명령 응답 발신 콜백. (채널, 스레드시각, 본문).
ReplyCallback = Callable[[str, str, str], None]
# RequestContext 로부터 관리 명령 실행에 필요한 맥락을 만든다.
AdminContextBuilder = Callable[[RequestContext], AdminContext]


class IngressService:
    """슬랙 이벤트를 받아 영속 작업 큐에 넣는다."""

    def __init__(
        self,
        listener: EventListener,
        dedup: DeduplicationTracker,
        queue: JobQueue,
        reactions: ReactionMarker,
        attachments: AttachmentStore,
        admin_router: AdminRouter,
        admin_context_builder: AdminContextBuilder,
        reply: ReplyCallback,
        allowed_reactions: frozenset[str],
        on_reaction: ReactionCallback,
    ) -> None:
        self._listener = listener
        self._dedup = dedup
        self._queue = queue
        self._reactions = reactions
        self._attachments = attachments
        self._admin_router = admin_router
        self._admin_context_builder = admin_context_builder
        self._reply = reply
        self._allowed_reactions = allowed_reactions
        self._on_reaction = on_reaction

    def register(self, gateway: SlackGateway) -> None:
        """게이트웨이에 이벤트 핸들러 세 종류를 등록한다."""
        gateway.on("app_mention", self.handle_app_mention)
        gateway.on("message", self.handle_message)
        gateway.on("reaction_added", self.handle_reaction)

    def handle_app_mention(self, event: Mapping[str, Any]) -> None:
        self._process(self._listener.from_app_mention(event), event)

    def handle_message(self, event: Mapping[str, Any]) -> None:
        self._process(self._listener.from_message(event), event)

    def handle_reaction(self, event: Mapping[str, Any]) -> None:
        """리액션 이벤트는 큐를 거치지 않는다. 콜백으로 그대로 넘긴다.

        한 건에서 난 예외가 이후 이벤트 처리를 막지 않는다.
        """
        try:
            result = self._listener.from_reaction(event, self._allowed_reactions)
            if result is None:
                return
            self._on_reaction(*result)
        except Exception:
            log.exception("리액션 이벤트 처리 실패: %s", event.get("reaction"))

    def _process(self, ctx: RequestContext | None, event: Mapping[str, Any]) -> None:
        """멘션·메시지 이벤트 하나를 접수한다.

        한 건에서 난 예외가 이후 이벤트 처리를 막지 않는다 — 큐 저장소
        장애 한 번이 그 뒤의 모든 요청을 막으면 안 된다. 다만 삼키되
        기록은 남긴다. 안 남기면 요청이 사라진 것과 아무 일도 없던 것이
        같은 모습이 된다.
        """
        try:
            if ctx is None:
                return
            if self._dedup.already_seen_event(ctx.channel, ctx.ts):
                return

            ctx = replace(ctx, text=MENTION_RE.sub("", ctx.text).strip())

            admin_ctx = self._admin_context_builder(ctx)
            admin_result = self._admin_router.dispatch(ctx.text, admin_ctx)
            if admin_result is not None:
                self._reply(ctx.channel, ctx.thread_ts, admin_result.message)
                return

            ctx = self._merge_attachments(ctx, event)

            if not self._queue.enqueue(ctx):
                # 이미 있는 요청이다. 대기 표식을 두 번 달지 않는다.
                return
            self._reactions.mark_waiting(ctx.channel, ctx.ts)
        except Exception:
            channel = ctx.channel if ctx is not None else event.get("channel")
            ts = ctx.ts if ctx is not None else event.get("ts")
            log.exception("요청 접수 실패: %s:%s", channel, ts)

    def _merge_attachments(
        self, ctx: RequestContext, event: Mapping[str, Any]
    ) -> RequestContext:
        """첨부를 저장하고 저장된 경로를 컨텍스트에 반영한다.

        원본 파일 필드(이름·크기·URL 등)는 그대로 유지하고 `local_path` 만
        더한다 — `RequestContext.files` 는 슬랙이 준 파일 정보 형태를 그대로
        담는 자리이지, 저장 결과 전용 타입이 아니다.
        """
        saved = self._attachments.save(event)
        if not saved:
            return ctx
        originals = {f.get("name"): f for f in (event.get("files") or [])}
        merged_files = []
        for item in saved:
            original = dict(originals.get(item.name) or {})
            original["name"] = item.name
            original["mimetype"] = item.kind
            original["local_path"] = item.path
            merged_files.append(original)
        return replace(ctx, files=tuple(merged_files))
