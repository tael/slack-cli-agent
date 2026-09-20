"""엔진이 매 턴 실제로 보내는 바이트를 선언한다 (sca-ygd).

codex 와 gemini 는 재개 턴마다 시스템 지침 전체를 user prompt 에 다시 싣는다.
claude 는 CLI 가 지침을 따로 받는다. 크기와 전달 방식을 선언으로 두고 감사에
남겨야 상한을 정할 근거가 생긴다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from test_engine import SETTINGS, claude_profile, codex_profile, gemini_profile, request

from slack_cli_agent.engine.claude import ClaudeEngine
from slack_cli_agent.engine.codex import CodexEngine
from slack_cli_agent.engine.footprint import (
    INSTRUCTION_TRANSPORT_NATIVE,
    INSTRUCTION_TRANSPORT_USER_PROMPT,
)
from slack_cli_agent.engine.gemini import GeminiEngine
from slack_cli_agent.engine.tool_selection import ToolSelection


class Test바이트를_센다:
    def test_utf8_바이트로_센다(self, tmp_path: Path) -> None:
        """글자 수로 세면 한글이 실제 비용의 3분의 1로 읽힌다."""
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        발자국 = 엔진.footprint_for(request(prompt="한글", system_prompt="지침"))
        assert 발자국.user_prompt_bytes == 6
        assert 발자국.instruction_bytes == 6

    def test_총량은_지침과_본문과_어댑터_추가분의_합이다(self, tmp_path: Path) -> None:
        엔진 = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        발자국 = 엔진.footprint_for(request(prompt="본문", system_prompt="지침"))
        assert 발자국.total_bytes == (
            발자국.instruction_bytes + 발자국.user_prompt_bytes + 발자국.adapter_added_bytes
        )


class Test전달_방식을_선언한다:
    def test_claude_는_지침을_따로_보낸다(self, tmp_path: Path) -> None:
        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        발자국 = 엔진.footprint_for(request(resume=True, session_id="s1"))
        assert 발자국.instruction_transport == INSTRUCTION_TRANSPORT_NATIVE
        assert 발자국.instruction_replayed_on_resume is False

    def test_codex_신규턴은_지침을_따로_보낸다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        발자국 = 엔진.footprint_for(request(resume=False))
        assert 발자국.instruction_transport == INSTRUCTION_TRANSPORT_NATIVE
        assert 발자국.instruction_replayed_on_resume is False

    def test_codex_재개턴은_지침이_본문에_섞인다(self, tmp_path: Path) -> None:
        """CLI 가 재개 세션의 developer_instructions 를 안 바꾼다(sca-ivs).
        그래서 이번 턴 지침이 user prompt 로 들어가고, 그 바이트는 세션
        문맥에 누적된다."""
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        발자국 = 엔진.footprint_for(request(resume=True, session_id="s1"))
        assert 발자국.instruction_transport == INSTRUCTION_TRANSPORT_USER_PROMPT
        assert 발자국.instruction_replayed_on_resume is True

    def test_gemini_는_언제나_본문에_섞는다(self, tmp_path: Path) -> None:
        엔진 = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        신규 = 엔진.footprint_for(request(resume=False))
        재개 = 엔진.footprint_for(request(resume=True, session_id="s1"))
        assert 신규.instruction_transport == INSTRUCTION_TRANSPORT_USER_PROMPT
        assert 신규.instruction_replayed_on_resume is False
        assert 재개.instruction_replayed_on_resume is True


class Test모든_엔진이_선언한다:
    def test_선언_누락은_계약_위반이다(self, tmp_path: Path) -> None:
        """새 엔진이 선언을 빠뜨리면 그 봇만 집계에서 사라진다."""
        만들기: list[tuple[Any, Any]] = [
            (ClaudeEngine, claude_profile), (CodexEngine, codex_profile), (GeminiEngine, gemini_profile),
        ]
        for 종류, 프로필 in 만들기:
            발자국 = 종류(프로필(tmp_path), SETTINGS).footprint_for(request())
            assert 발자국.instruction_transport in (
                INSTRUCTION_TRANSPORT_NATIVE, INSTRUCTION_TRANSPORT_USER_PROMPT,
            )


class Test감사에_남는다:
    def _돌린다(self, tmp_path: Path, 감사: Any, **요청: Any) -> None:
        from test_engine import FakeCompleted, RecordingEngine, 통과정책

        from slack_cli_agent.engine.runner import EngineRunner

        엔진 = RecordingEngine(claude_profile(tmp_path), SETTINGS)
        EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=감사,
        ).run(엔진, request(**요청))

    def test_크기와_전달방식이_기록된다(self, tmp_path: Path) -> None:
        from test_engine_capability import _감사

        감사 = _감사()
        self._돌린다(tmp_path, 감사, prompt="본문", system_prompt="지침", request_id="req-1")
        종류 = [kind for kind, _ in 감사.기록]
        assert "payload" in 종류
        필드 = next(f for kind, f in 감사.기록 if kind == "payload")
        assert 필드["user_prompt_bytes"] == 6
        assert 필드["instruction_transport"] == INSTRUCTION_TRANSPORT_NATIVE
        assert 필드["request_id"] == "req-1"

    def test_프롬프트_원문은_안_남긴다(self, tmp_path: Path) -> None:
        """감사 파일은 조회 권한이 다른 자리다. 크기만 남기면 충분하다."""
        from test_engine_capability import _감사

        감사 = _감사()
        self._돌린다(tmp_path, 감사, prompt="비밀본문", system_prompt="비밀지침")
        직렬 = repr(감사.기록)
        assert "비밀본문" not in 직렬
        assert "비밀지침" not in 직렬


class Test선언은_실제_명령과_맞는다:
    """선언과 build_command 가 어긋나면 이 측정으로 정한 상한이 틀린 값에
    걸린다 (sca-ygd 리뷰 [중간]). 엔진마다 지침이 실리는 인자가 달라서
    마지막 인자만 보면 codex 신규턴을 못 본다."""

    def _모델이_읽는_바이트(self, cmd: list[str], *, 값을_가진_플래그: tuple[str, ...]) -> int:
        """argv 중 모델에게 그대로 전달되는 텍스트만 더한다. 플래그 이름과
        TOML 따옴표는 CLI 가 벗겨내므로 세지 않는다. 플래그 이름을 엔진마다
        받는 이유는 claude 의 -p 가 값 없는 플래그이기 때문이다."""
        보낸것 = ""
        for i, tok in enumerate(cmd):
            if tok in 값을_가진_플래그:
                보낸것 += cmd[i + 1]
            elif tok.startswith("developer_instructions="):
                보낸것 += tomllib.loads(tok)["developer_instructions"]
            elif tok == "--":
                보낸것 += cmd[i + 1]
        return len(보낸것.encode("utf-8"))

    def test_claude_합계가_실제_명령과_같다(self, tmp_path: Path) -> None:
        엔진, 플래그 = ClaudeEngine(claude_profile(tmp_path), SETTINGS), ("--append-system-prompt",)
        요청 = request(
            system_prompt="지침", prompt="본문", session_id="s1", readable_dirs=(tmp_path,),
        )
        발자국 = 엔진.footprint_for(요청)
        assert 발자국.total_bytes == self._모델이_읽는_바이트(엔진.build_command(요청), 값을_가진_플래그=플래그)

    def test_codex_신규턴_합계가_실제_명령과_같다(self, tmp_path: Path) -> None:
        """지침이 developer_instructions 에 실리는 유일한 경우다."""
        엔진, 플래그 = CodexEngine(codex_profile(tmp_path), SETTINGS), ()
        요청 = request(resume=False, system_prompt="지침", prompt="본문", readable_dirs=(tmp_path,))
        발자국 = 엔진.footprint_for(요청)
        assert 발자국.total_bytes == self._모델이_읽는_바이트(엔진.build_command(요청), 값을_가진_플래그=플래그)

    def test_codex_신규턴은_지침이_없어도_경로_안내를_보낸다(self, tmp_path: Path) -> None:
        """리뷰 경로가 빈 지침으로 세션을 연다. 그때도 읽을 자리는 알려야
        한다 - codex 는 --add-dir 을 안 받아 이 자리가 유일하다 (sca-9u18)."""
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        요청 = request(resume=False, system_prompt="", readable_dirs=(tmp_path,))
        cmd = 엔진.build_command(요청)
        assert any(tok.startswith("developer_instructions=") for tok in cmd)
        assert 엔진.footprint_for(요청).adapter_added_bytes > 0
        assert 엔진.footprint_for(요청).total_bytes == self._모델이_읽는_바이트(cmd, 값을_가진_플래그=())

    def test_codex_신규턴은_알릴_것이_없으면_안_보낸다(self, tmp_path: Path) -> None:
        엔진 = CodexEngine(codex_profile(tmp_path), SETTINGS)
        # 허용목록도 알릴 것에 든다. 제한 없는 요청이라야 전제가 성립한다.
        요청 = request(
            resume=False, system_prompt="", readable_dirs=(),
            tools=ToolSelection.unrestricted(),
        )
        cmd = 엔진.build_command(요청)
        assert not any(tok.startswith("developer_instructions=") for tok in cmd)
        assert 엔진.footprint_for(요청).adapter_added_bytes == 0
        assert 엔진.footprint_for(요청).total_bytes == self._모델이_읽는_바이트(cmd, 값을_가진_플래그=())

    def test_codex_재개턴_합계가_실제_명령과_같다(self, tmp_path: Path) -> None:
        엔진, 플래그 = CodexEngine(codex_profile(tmp_path), SETTINGS), ()
        요청 = request(
            resume=True, session_id="s1", system_prompt="지침", prompt="본문",
            readable_dirs=(tmp_path,),
        )
        발자국 = 엔진.footprint_for(요청)
        assert 발자국.total_bytes == self._모델이_읽는_바이트(엔진.build_command(요청), 값을_가진_플래그=플래그)

    def test_gemini_합계가_실제_명령과_같다(self, tmp_path: Path) -> None:
        엔진, 플래그 = GeminiEngine(gemini_profile(tmp_path), SETTINGS), ("-p",)
        요청 = request(system_prompt="지침", prompt="본문", readable_dirs=(tmp_path,))
        발자국 = 엔진.footprint_for(요청)
        assert 발자국.total_bytes == self._모델이_읽는_바이트(엔진.build_command(요청), 값을_가진_플래그=플래그)


class Test안_보낸_요청은_안_센다:
    """보장을 못 맞춰 막힌 요청은 프로세스가 뜨지 않는다. 그 행이 섞이면
    '실제로 보낸 바이트' 가 아니게 된다 (sca-ygd 리뷰 [중간])."""

    def test_차단된_요청은_전송량을_남기지_않는다(self, tmp_path: Path) -> None:
        from test_engine import FakeCompleted, 통과정책
        from test_engine_capability import _감사, _준비기록엔진

        from slack_cli_agent.engine.capability import ExecutionRequirements, ToolRestriction
        from slack_cli_agent.engine.runner import EngineRunner

        감사 = _감사()
        응답 = EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=감사,
        ).run(
            _준비기록엔진(claude_profile(tmp_path), SETTINGS),
            request(requirements=ExecutionRequirements(
                tool_restriction=ToolRestriction.EXACT_ALLOWLIST,
            )),
        )
        assert 응답.ok is False
        assert [kind for kind, _ in 감사.기록] == ["capability"]


class Test예산_보고가_전송량_기록에_실린다:
    """무엇이 상한에 걸려 빠졌는지는 조합기만 안다. 그 사실이 감사까지 닿아야
    상한을 조정할 근거가 된다 (sca-ygd)."""

    def test_요청이_실어온_값이_그대로_남는다(self, tmp_path: Path) -> None:
        from test_engine import FakeCompleted, 통과정책
        from test_engine_capability import _감사, _준비기록엔진

        from slack_cli_agent.engine.runner import EngineRunner

        감사 = _감사()
        EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=감사,
        ).run(
            _준비기록엔진(claude_profile(tmp_path), SETTINGS),
            request(
                prompt="본문", system_prompt="지침", session_id="s1",
                budget_report={"budget_limited": True, "omitted_document_count": 2},
            ),
        )
        필드 = next(f for kind, f in 감사.기록 if kind == "payload")
        assert 필드["budget_limited"] is True
        assert 필드["omitted_document_count"] == 2

    def test_값이_없으면_필드도_안_남는다(self, tmp_path: Path) -> None:
        """조합기를 안 거친 요청도 있다. 빈 값을 0 으로 적으면 '상한에 안
        걸렸다' 와 '조합기를 안 거쳤다' 가 같은 모습이 된다."""
        from test_engine import FakeCompleted, 통과정책
        from test_engine_capability import _감사, _준비기록엔진

        from slack_cli_agent.engine.runner import EngineRunner

        감사 = _감사()
        EngineRunner(
            SETTINGS,
            subprocess_runner=lambda cmd, cwd, timeout, env=None: FakeCompleted(
                stdout="답변", returncode=0
            ),
            environment_policy=통과정책(),
            audit=감사,
        ).run(
            _준비기록엔진(claude_profile(tmp_path), SETTINGS),
            request(prompt="본문", system_prompt="지침", session_id="s1"),
        )
        필드 = next(f for kind, f in 감사.기록 if kind == "payload")
        assert "budget_limited" not in 필드
