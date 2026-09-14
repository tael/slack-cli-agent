"""Claude Code CLI 어댑터.

경로는 --add-dir 로 넘긴다. 세션 ID 는 우리가 발급해 --session-id 로
넘기고, 이어가는 턴은 --resume 으로 같은 값을 다시 넘긴다. 시스템 프롬프트는
--append-system-prompt 로 매 턴 다시 준다 — Claude 는 시스템 프롬프트가
턴마다 갱신되는 엔진이라 directives_for_turn() 을 쓸 필요가 없다(기본값
그대로 빈 문자열).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from .base import Engine, EngineRequest, EngineResponse, Usage, UsageLimit

# 원본 bot.py usage_limit_message() 의 힌트 목록을 그대로 옮긴다.
_USAGE_LIMIT_HINTS = (
    "weekly limit", "usage limit", "rate limit",
    "hit your limit", "limit · resets", "limit reached",
)


class ClaudeEngine(Engine):
    name = "claude"

    def build_command(self, request: EngineRequest) -> list[str]:
        # --max-budget-usd 를 넣지 않는다. 구독 OAuth 토큰(sk-ant-oat)은 그
        # 인자를 주면 정상 요청까지 끊긴다 — 구독은 seat allowance 를 쓰지
        # 사용액 기반 예산이 아니다. API 키 과금 경로가 아니므로 예산 상한
        # 자체가 성립하지 않는다.
        cmd: list[str] = [
            str(self.spec.binary),
            "-p",
            "--output-format", "json",
            "--permission-mode", "dontAsk",
            # --allowedTools 화이트리스트가 settings 의 allow 규칙보다 우선한다
            # (실측 확인). 읽기 전용 권한 모델은 여기 목록으로 강제된다 —
            # request.allowed_tools 에 Bash/Edit/Write/NotebookEdit 를 넣지
            # 않는 것이 호출부(이 모듈 밖)의 책임이다.
            "--allowedTools", ",".join(request.allowed_tools),
            "--model", request.model,
            "--effort", request.effort,
        ]
        for path in request.readable_dirs:
            cmd += ["--add-dir", str(path)]
        cmd += ["--append-system-prompt", request.system_prompt]
        cmd += (["--resume", request.session_id] if request.resume
                else ["--session-id", request.session_id])
        # 프롬프트는 반드시 -- 뒤에 둔다. 하이픈으로 시작하는 사용자 입력이
        # 옵션으로 해석되는 것을 막는다. 원본 bot.py build_command() 와 같다.
        cmd += ["--", request.prompt]
        return cmd

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def parse(self, stdout: str, stderr: str, returncode: int) -> EngineResponse:
        if returncode != 0:
            limit = self._limit_from_text(stdout)
            if limit is not None:
                return EngineResponse(
                    ok=False, body=self._limit_body(limit), session_id=None,
                    model_actual=None, elapsed=0.0, turns=None, usage=None,
                    raw={"usage_limit": {"detail": limit.detail, "source": limit.source}},
                    failure_reason="usage_limit",
                )
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout, "stderr": stderr, "returncode": returncode},
                failure_reason="nonzero_exit",
            )

        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, Mapping):
            return EngineResponse(
                ok=False, body="처리에 실패했습니다.", session_id=None, model_actual=None,
                elapsed=0.0, turns=None, usage=None,
                raw={"stdout": stdout}, failure_reason="bad_json",
            )

        session_id = payload.get("session_id")
        model_actual = payload.get("model")
        turns = payload.get("num_turns")
        usage = Usage.from_mapping(payload.get("usage"))

        if payload.get("is_error"):
            subtype = str(payload.get("subtype") or "")
            is_limit = "limit" in subtype or "budget" in subtype
            reason = "usage_limit" if is_limit else "is_error"
            body = ("사용 한도에 걸려 중단했습니다." if is_limit
                    else "처리에 실패했습니다.")
            raw: dict[str, Any] = dict(payload)
            if is_limit:
                raw["usage_limit"] = {"detail": "", "source": "subtype"}
            return EngineResponse(
                ok=False, body=body, session_id=session_id, model_actual=model_actual,
                elapsed=0.0, turns=turns, usage=usage, raw=raw, failure_reason=reason,
            )

        body = str(payload.get("result") or "").strip()
        if not body:
            return EngineResponse(
                ok=False, body="응답이 비어 있습니다.", session_id=session_id,
                model_actual=model_actual, elapsed=0.0, turns=turns, usage=usage,
                raw=dict(payload), failure_reason="empty_response",
            )
        return EngineResponse(
            ok=True, body=body, session_id=session_id, model_actual=model_actual,
            elapsed=0.0, turns=turns, usage=usage, raw=dict(payload), failure_reason=None,
        )

    def detect_usage_limit(self, response: EngineResponse) -> UsageLimit | None:
        info = response.raw.get("usage_limit") if isinstance(response.raw, Mapping) else None
        if not info:
            return None
        return UsageLimit(detail=info.get("detail", ""), source=info.get("source", "hint"))

    @staticmethod
    def _limit_body(limit: UsageLimit) -> str:
        if limit.detail:
            return f"구독 사용 한도에 걸려 지금은 답할 수 없어요.\n\n> {limit.detail}"
        return "구독 사용 한도에 걸려 지금은 답할 수 없어요."

    @staticmethod
    def _limit_from_text(stdout: str) -> UsageLimit | None:
        """원본 usage_limit_message() 의 판정을 그대로 옮긴다.

        subtype 은 success 인데 api_error_status 가 429 고 실제 사유가
        result 문구에만 있는 경우가 있다. 종료 코드만 보면 못 잡는다.
        2026-09-11 실측.
        """
        try:
            payload = json.loads(stdout or "")
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        result = str(payload.get("result") or "")
        status = payload.get("api_error_status")
        subtype = str(payload.get("subtype") or "")
        low = result.lower()
        if status == 429:
            source = "status_code"
        elif any(hint in low for hint in _USAGE_LIMIT_HINTS):
            source = "hint"
        elif "limit" in subtype or "budget" in subtype:
            source = "subtype"
        else:
            return None
        return UsageLimit(detail=result.strip(), source=source)
