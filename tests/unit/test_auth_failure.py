"""세 엔진이 로그인 만료를 같은 방식으로 판정한다 (sca-sj8r).

판정 로직은 Engine 한 자리에 두고 엔진은 자기 문구만 댄다. codex 만
구현돼 있던 동안 claude 나 gemini 가 1차일 때 로그인이 풀리면 일반 실패로
처리돼 2차 전환이 시작되지 않았다.

문구는 실측이다 (2026-09-19, 각 CLI 바이너리의 문자열) - claude 2.1.270 은
'Please run /login' 과 'Failed to authenticate', agy 는 'authentication
required. Run ... to log in.' 과 'You are currently not signed in.' 을 낸다.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from test_engine import claude_profile, codex_profile, gemini_profile
from test_engine_capability import SETTINGS

from slack_cli_agent.engine.base import EngineResponse
from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.gemini import GeminiEngine


def 실패(**kw) -> EngineResponse:
    기본: dict[str, object] = {
        "ok": False, "body": "처리에 실패했습니다.", "session_id": None,
        "model_actual": None, "elapsed": 0.0, "turns": None, "usage": None,
        "failure_reason": "nonzero_exit",
    }
    return EngineResponse(**{**기본, **kw})


@pytest.fixture
def 엔진들(tmp_path: Path):
    return {
        "claude": ClaudeEngine(claude_profile(tmp_path), SETTINGS),
        "codex": CodexEngine(codex_profile(tmp_path), SETTINGS),
        "gemini": GeminiEngine(gemini_profile(tmp_path), SETTINGS),
    }


class Test세_엔진이_모두_판정한다:
    #: 각 CLI 가 로그인이 풀렸을 때 실제로 내는 출력이다.
    실측문구: ClassVar[dict[str, str]] = {
        "claude": "Please run /login · API Error: 401",
        "codex": "ERROR codex_login::auth::manager: Failed to refresh token: "
                 '401 Unauthorized: {"code": "refresh_token_invalidated"}',
        "gemini": "Error: authentication required. Run 'agy login' to log in.",
    }

    def test_자기_엔진의_문구를_인증_실패로_본다(self, 엔진들) -> None:
        for 이름, 엔진 in 엔진들.items():
            사유 = 엔진.detect_auth_failure(실패(raw={"stderr": self.실측문구[이름]}))
            assert 사유 is not None, 이름

    def test_안내에_다시_로그인하는_법이_들어간다(self, 엔진들) -> None:
        """'한도가 풀리면' 으로 읽히면 아무도 다시 로그인하지 않는다."""
        for 이름, 엔진 in 엔진들.items():
            사유 = 엔진.detect_auth_failure(실패(raw={"stderr": self.실측문구[이름]}))
            assert "로그인" in (사유 or ""), 이름

    def test_성공한_응답은_인증_실패가_아니다(self, 엔진들) -> None:
        """실패 텍스트가 아니라 사용자가 쓴 말에 그 문구가 들어갈 수 있다."""
        성공 = EngineResponse(
            ok=True, body="Please run /login 이 무슨 뜻인지 물어보셨습니다.", session_id=None,
            model_actual=None, elapsed=0.0, turns=None, usage=None,
        )
        for 이름, 엔진 in 엔진들.items():
            assert 엔진.detect_auth_failure(성공) is None, 이름

    def test_실패_본문에_있는_문구로는_판정하지_않는다(self, 엔진들) -> None:
        """본문에는 사람이 쓴 말이 섞인다. 인증 문구를 물어본 턴이 다른 이유로
        실패하면 그것만으로 엔진이 바뀐다 (리뷰 지적 2026-09-19)."""
        응답 = 실패(body="Please run /login 이 무슨 뜻인지 물어보셨습니다.",
                  raw={"stderr": "timeout after 600s"})
        for 이름, 엔진 in 엔진들.items():
            assert 엔진.detect_auth_failure(응답) is None, 이름

    def test_도구가_낸_인증_오류는_판정하지_않는다(self, 엔진들) -> None:
        """MCP 서버나 gh·aws 같은 도구도 인증 오류를 낸다. 그것으로 엔진을
        바꾸면 CLI 로그인은 멀쩡한데 1차가 통째로 내려간다."""
        도구출력 = [
            "MCP session expired for airflow - send mcp_reconnect and retry",
            "MCP server authentication required for notion",
            "ERROR rmcp::transport::worker: worker quit with fatal: HTTP 401 Unauthorized",
            "fatal: could not read Username for 'https://github.com': not logged in",
            "An error occurred (InvalidApiKey) when calling the GetObject operation",
        ]
        for 이름, 엔진 in 엔진들.items():
            for 줄 in 도구출력:
                assert 엔진.detect_auth_failure(실패(raw={"stderr": 줄})) is None, (이름, 줄)

    def test_MCP_서버의_401은_인증_실패가_아니다(self, 엔진들) -> None:
        """MCP 서버 하나가 401 을 내도 그 CLI 의 로그인은 멀쩡하다. 401 만으로
        좁히면 엔진이 통째로 바뀐다."""
        for 이름, 엔진 in 엔진들.items():
            응답 = 실패(raw={"stderr": "ERROR rmcp::transport::worker: "
                            "worker quit with fatal: HTTP 401 Unauthorized"})
            assert 엔진.detect_auth_failure(응답) is None, 이름

    def test_다른_엔진의_문구로는_판정하지_않는다(self, 엔진들) -> None:
        """문구 표를 공용으로 두면 한 CLI 의 출력이 다른 엔진을 전환시킨다."""
        for 낸쪽 in 엔진들:
            for 보는쪽, 엔진 in 엔진들.items():
                if 보는쪽 == 낸쪽:
                    continue
                응답 = 실패(raw={"stderr": self.실측문구[낸쪽]})
                assert 엔진.detect_auth_failure(응답) is None, (낸쪽, 보는쪽)


class Test판정이_한_자리에_있다:
    """엔진마다 따로 구현하면 한쪽만 고쳐 어긋난다. 엔진이 대는 것은 문구뿐이다."""

    def test_엔진은_판정_함수를_각자_두지_않는다(self, 엔진들) -> None:
        from slack_cli_agent.engine.base import Engine

        for 이름, 엔진 in 엔진들.items():
            assert type(엔진).detect_auth_failure is Engine.detect_auth_failure, 이름

    def test_문구가_없는_엔진은_판정하지_않는다(self, tmp_path: Path) -> None:
        """새 엔진이 붙었을 때 문구를 안 대면 조용히 전환되면 안 된다."""
        from slack_cli_agent.engine.base import Engine

        class 문구없는엔진(ClaudeEngine):
            AUTH_FAILURE_MARKERS: ClassVar[tuple[str, ...]] = ()

        엔진 = 문구없는엔진(claude_profile(tmp_path), SETTINGS)
        assert Engine.detect_auth_failure(엔진, 실패(raw={"stderr": "Please run /login"})) is None


class Test응답의_어느_자리든_본다:
    """엔진마다 실패 텍스트가 담기는 자리가 다르다 - codex 는 stderr, claude 의
    is_error 는 result, gemini 는 error 다. 한 자리만 보면 그 엔진만 샌다."""

    @pytest.mark.parametrize("자리", ["stderr", "stdout", "result", "error"])
    def test_자리마다_같게_본다(self, 엔진들, 자리: str) -> None:
        응답 = 실패(failure_reason="is_error", raw={자리: "Please run /login"})
        assert 엔진들["claude"].detect_auth_failure(응답) is not None


class Test클로드_API_상태코드_401:
    """claude 는 로그인 만료를 result 의 api_error_status 401 로도 알린다 (C7).

    2026-11-07 07:50-15:40 마메치 로그의 401 239줄은 모두 'Failed to
    authenticate.' 를 함께 냈으나, 문구 없이 상태 코드만 오는 응답도 같은
    상태다. 이 필드는 CLI 자신의 API 호출 결과라 MCP 서버의 401 과 섞이지
    않는다.
    """

    #: 2026-11-07 마메치 운영 로그의 실제 result 이벤트에서 키만 추렸다.
    운영결과: ClassVar[dict[str, object]] = {
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": 401, "terminal_reason": "api_error",
        "result": "Failed to authenticate. API Error: 401 OAuth access token has expired. "
                  "Re-authenticate to continue.",
        "session_id": "43d62be6-01cb-4f91-ae13-1305a8c47434",
    }

    def test_운영_만료_결과는_계속_탐지한다(self, 엔진들) -> None:
        import json

        claude = 엔진들["claude"]
        응답 = claude.parse(json.dumps(self.운영결과), "", 1)
        assert claude.detect_auth_failure(응답) is not None
        응답 = claude.parse(json.dumps(self.운영결과), "", 0)
        assert claude.detect_auth_failure(응답) is not None

    def test_문구_없이_401_만_와도_탐지한다(self, 엔진들) -> None:
        import json

        claude = 엔진들["claude"]
        결과 = {**self.운영결과, "result": "API Error: 401 token expired"}
        for 종료코드 in (0, 1):
            응답 = claude.parse(json.dumps(결과), "", 종료코드)
            assert claude.detect_auth_failure(응답) is not None, 종료코드

    def test_401_이_아닌_상태코드는_인증_실패가_아니다(self, 엔진들) -> None:
        import json

        claude = 엔진들["claude"]
        결과 = {**self.운영결과, "api_error_status": 500, "result": "API Error: 500 overloaded"}
        for 종료코드 in (0, 1):
            응답 = claude.parse(json.dumps(결과), "", 종료코드)
            assert claude.detect_auth_failure(응답) is None, 종료코드

    def test_Cloudflare_403은_새_세션_재시도를_막지_않는다(self, 엔진들) -> None:
        """Cloudflare 의 일시적 403도 'Failed to authenticate.'로 시작한다.

        로그인 만료로 오분류하면 DirectInvoker가 auth_failure로 바꿔 pipeline의
        새 세션 재시도를 막는다. 2026-11-13 운영에서는 같은 호스트와 계정의
        다음 최소 호출이 성공했으므로 이 응답은 일시 실패로 남겨야 한다.
        """
        import json

        claude = 엔진들["claude"]
        결과 = {
            **self.운영결과,
            "api_error_status": 403,
            "result": "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare",
        }
        for 종료코드 in (0, 1):
            응답 = claude.parse(json.dumps(결과), "", 종료코드)
            assert claude.detect_auth_failure(응답) is None, 종료코드

    def test_다른_엔진은_401_필드로_판정하지_않는다(self, 엔진들) -> None:
        for 이름 in ("codex", "gemini"):
            응답 = 실패(raw={"api_error_status": 401, "stderr": "boom"})
            assert 엔진들[이름].detect_auth_failure(응답) is None, 이름

    @pytest.mark.parametrize("문구", [
        "OAuth token has been revoked. Please run /login",
        "API Error: 401 OAuth access token has expired.",
        "OAuth token has been revoked.",
    ])
    def test_OAuth_만료_폐기_문구를_탐지한다(self, 엔진들, 문구: str) -> None:
        응답 = 실패(failure_reason="is_error", raw={"result": 문구})
        assert 엔진들["claude"].detect_auth_failure(응답) is not None


class Test토큰_부재도_전환_계기다:
    """마메치는 CLAUDE_CODE_OAUTH_TOKEN 이 없으면 no_oauth_token 으로 전환한다.

    여기서는 키체인 로그인도 쓰므로 토큰 변수가 없다는 것만으로 실패시키지
    않는다. 토큰도 키체인 로그인도 없으면 CLI 가 'Not logged in · Please run
    /login' 을 내고 (2026-09-15 실측, environment.py), 그것이 로그인 만료와 같은
    계기로 잡혀야 2차로 넘어간다.
    """

    def test_로그인_없음_결과가_인증_실패로_잡힌다(self, 엔진들) -> None:
        import json

        claude = 엔진들["claude"]
        결과 = {"type": "result", "subtype": "success", "is_error": True,
                "result": "Not logged in · Please run /login"}
        for 종료코드 in (0, 1):
            응답 = claude.parse(json.dumps(결과), "", 종료코드)
            assert claude.detect_auth_failure(응답) is not None, 종료코드

    def test_JSON_아닌_로그인_없음_출력도_잡힌다(self, 엔진들) -> None:
        claude = 엔진들["claude"]
        응답 = claude.parse("Not logged in · Please run /login\n", "", 1)
        assert claude.detect_auth_failure(응답) is not None
