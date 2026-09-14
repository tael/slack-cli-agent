"""예외 계층. 호출부가 무엇을 복구할 수 있는지로 나눈다."""

from __future__ import annotations


class AgentError(Exception):
    """이 패키지가 내는 모든 예외의 기반."""


class ConfigError(AgentError):
    """프로필·채널 설정이 잘못됐다. 기동 전에 검출한다."""


class MissingPromptError(ConfigError):
    """프롬프트 파일이 없거나 비었다.

    가드 문구가 빠진 채로 답하지 않기 위해 기본값으로 넘어가지 않는다.
    """


class EngineError(AgentError):
    """엔진 실행 실패."""


class UsageLimitError(EngineError):
    """구독 한도 소진. 폴백 판정의 입력이다."""


class SlackError(AgentError):
    """슬랙 API 호출 실패."""


class HistoryUnavailable(SlackError):
    """기록 조회가 빈 결과만 줬다. 없는 것과 구분한다."""
