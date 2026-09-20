"""요청이 쓸 수 있는 도구를 세 가지 상태로 표현한다 (sca-0a7).

빈 목록 하나가 '제한 없음' 과 '도구 전부 금지' 를 동시에 뜻해서 호출자가
후자를 표현할 수 없었다. learning 의 야간 배치가 그 자리다 - 주석은 '도구
없음' 이라고 적혀 있는데 실제로는 전체 도구가 열린 채 돌았다.

2026-09-19 실측 근거 : claude 는 --disallowedTools=* 로 도구 집합을 비운다
(디버그 로그의 'Dynamic tool loading' 줄이 사라지고 tool_use 가 4회 모두
0건이다). 2026-09-20 에 이름 기반 거부도 실제로 막는 것을 확인했다 - 사용자
settings 의 allow 를 이긴다(sca-6ewc). codex 와 gemini 에는 대응 수단이 없다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.engine.capability import ToolRestriction
from slack_cli_agent.engine.tool_selection import ToolAccess, ToolSelection


class Test세_상태를_가른다:
    def test_기본은_제한_없음이다(self) -> None:
        """예전 기본값인 빈 튜플이 뜻하던 것이 이것이다."""
        선택 = ToolSelection()
        assert 선택.access is ToolAccess.UNRESTRICTED
        assert 선택.names == ()

    def test_허용목록은_이름을_담는다(self) -> None:
        선택 = ToolSelection.allow(["Read", "Grep"])
        assert 선택.access is ToolAccess.ALLOWLIST
        assert 선택.names == ("Read", "Grep")

    def test_전부_금지는_이름이_없다(self) -> None:
        선택 = ToolSelection.forbid_all()
        assert 선택.access is ToolAccess.FORBIDDEN
        assert 선택.names == ()

    def test_빈_허용목록은_거부한다(self) -> None:
        """빈 목록을 넘기던 실수가 조용히 '제한 없음' 이 되던 자리다.
        의도가 둘 중 무엇인지 호출자가 밝히게 한다."""
        with pytest.raises(ValueError, match="forbid_all"):
            ToolSelection.allow([])


class Test축_수준으로_옮긴다:
    """엔진이 실제로 무엇을 강제하는지는 ToolRestriction 축이 말한다."""

    @pytest.mark.parametrize(
        ("선택", "수준"),
        [
            (ToolSelection(), ToolRestriction.NONE),
            (ToolSelection.allow(["Read"]), ToolRestriction.EXACT_ALLOWLIST),
            (ToolSelection.forbid_all(), ToolRestriction.ALL_FORBIDDEN),
        ],
    )
    def test_요구_수준(self, 선택: ToolSelection, 수준: ToolRestriction) -> None:
        assert 선택.restriction is 수준


class Test전부_금지가_가장_강하다:
    def test_허용목록보다_위다(self) -> None:
        """허용목록의 극한이 0개다. 같은 축에서 더 강한 쪽에 둔다."""
        순서 = tuple(ToolRestriction)
        assert 순서.index(ToolRestriction.ALL_FORBIDDEN) > 순서.index(
            ToolRestriction.EXACT_ALLOWLIST
        )

    def test_허용목록만_강제하는_엔진은_미달로_잡힌다(self) -> None:
        from slack_cli_agent.engine.capability import EngineCapabilities, ExecutionRequirements

        요구 = ExecutionRequirements(tool_restriction=ToolRestriction.ALL_FORBIDDEN)
        실제 = EngineCapabilities(tool_restriction=ToolRestriction.EXACT_ALLOWLIST)
        assert 요구.unmet(실제) == ("tool_restriction",)


class Test엔진이_도구_금지를_다루는_법:
    """세 엔진이 같은 요구를 받는다. 강제할 수 있는 것은 claude 뿐이고,
    나머지 둘은 선언을 올리지 않아 기존 unmet-axis 기계가 처리한다."""

    def _claude(self, tmp_path):
        from test_engine import claude_profile
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        return ClaudeEngine(claude_profile(tmp_path), SETTINGS)

    def test_claude_는_도구_집합을_비우는_인자를_넘긴다(self, tmp_path) -> None:
        """2026-09-19 실측 - 개별 이름 거부는 다른 도구로 우회된다.
        도구 집합 자체가 비는 것은 와일드카드뿐이다."""
        from test_engine import request

        cmd = self._claude(tmp_path).build_command(request(tools=ToolSelection.forbid_all()))
        assert "--disallowedTools" in cmd
        assert cmd[cmd.index("--disallowedTools") + 1] == "*"

    def test_claude_는_전부_금지를_선언한다(self, tmp_path) -> None:
        from test_engine import request

        보장 = self._claude(tmp_path).capabilities_for(request(tools=ToolSelection.forbid_all()))
        assert 보장.tool_restriction is ToolRestriction.ALL_FORBIDDEN

    def test_codex_와_gemini_는_선언을_올리지_않는다(self, tmp_path) -> None:
        """codex 의 --sandbox 는 쓰기 범위만, agy 의 --sandbox 는 터미널만
        제한한다. 도구 사용 자체를 막는 수단이 없다."""
        from test_engine import codex_profile, gemini_profile, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.codex import CodexEngine
        from slack_cli_agent.engine.gemini import GeminiEngine

        요청 = request(tools=ToolSelection.forbid_all())
        codex = CodexEngine(codex_profile(tmp_path), SETTINGS)
        gemini = GeminiEngine(gemini_profile(tmp_path), SETTINGS)
        # codex 는 샌드박스가 꺼진 기본값이라 NONE 이고, agy 는 언제나 NONE 이다.
        assert codex.capabilities_for(요청).tool_restriction is ToolRestriction.NONE
        assert gemini.capabilities_for(요청).tool_restriction is ToolRestriction.NONE


class Test실행_정책이_금지를_요구로_옮긴다:
    def test_전부_금지는_그_수준을_요구한다(self) -> None:
        from slack_cli_agent.auth.execution_policy import ExecutionPolicy

        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection.forbid_all())
        assert 요구.tool_restriction is ToolRestriction.ALL_FORBIDDEN

    def test_제한_없음은_요구가_없다(self) -> None:
        from slack_cli_agent.auth.execution_policy import ExecutionPolicy

        요구 = ExecutionPolicy().requirements_for(config=None, tools=ToolSelection())
        assert 요구.tool_restriction is None


class Test기록에_이름이_있다:
    def test_감사_문구가_있다(self) -> None:
        """이름이 없으면 감사에 열거형 값이 그대로 실린다."""
        from slack_cli_agent.engine.runner import LEVEL_NAMES

        assert LEVEL_NAMES[ToolRestriction.ALL_FORBIDDEN] == "도구 전부 금지"


class Test팩토리를_우회해도_모순이_안_만들어진다:
    """리뷰 지적 2026-09-19 - frozen 만으로는 부족하다. 생성자와
    dataclasses.replace 로 모순 상태를 그대로 만들 수 있었다."""

    def test_허용목록인데_이름이_없으면_거부한다(self) -> None:
        with pytest.raises(ValueError, match="forbid_all"):
            ToolSelection(access=ToolAccess.ALLOWLIST)

    def test_금지인데_이름을_들면_거부한다(self) -> None:
        with pytest.raises(ValueError, match="이름"):
            ToolSelection(access=ToolAccess.FORBIDDEN, names=("Read",))

    def test_제한_없음인데_이름을_들면_거부한다(self) -> None:
        with pytest.raises(ValueError, match="이름"):
            ToolSelection(names=("Read",))

    def test_replace_도_같은_검사를_받는다(self) -> None:
        import dataclasses

        with pytest.raises(ValueError):
            dataclasses.replace(ToolSelection.allow(["Read"]), names=())


class Test축의_뜻을_고정한다:
    """리뷰 지적 - 요구가 허용목록인데 엔진이 전부 금지면 미달로 안 잡힌다.
    이 축은 '얼마나 조였는가' 를 재므로 더 조인 쪽이 약한 요구를 만족하는
    것이 맞다. 그 판정이 해로워지는 조합이 실제로 생기지 않는다는 것을
    여기서 고정한다 - 요구와 선언이 같은 ToolSelection 하나에서 나온다."""

    def test_claude_의_선언은_요청의_선택에서_나온다(self, tmp_path) -> None:
        from test_engine import claude_profile, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.auth.execution_policy import ExecutionPolicy
        from slack_cli_agent.engine.claude import ClaudeEngine

        엔진 = ClaudeEngine(claude_profile(tmp_path), SETTINGS)
        for 선택 in (ToolSelection.unrestricted(), ToolSelection.allow(["Read"]),
                    ToolSelection.forbid_all()):
            요구 = ExecutionPolicy().requirements_for(config=None, tools=선택)
            보장 = 엔진.capabilities_for(request(tools=선택))
            assert 요구.unmet(보장) == ()
            assert 보장.tool_restriction is 선택.restriction


class Test금지일_때_허용목록_인자를_안_넘긴다:
    """실측은 --disallowedTools=* 단독으로 했다. 빈 허용목록을 함께 넘기는
    것은 재보지 않은 조합이라 실측과 같은 모양으로 맞춘다."""

    def test_allowedTools_가_없다(self, tmp_path) -> None:
        from test_engine import claude_profile, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        cmd = ClaudeEngine(claude_profile(tmp_path), SETTINGS).build_command(
            request(tools=ToolSelection.forbid_all())
        )
        assert "--allowedTools" not in cmd

    def test_허용목록일_때는_넘긴다(self, tmp_path) -> None:
        from test_engine import claude_profile, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        cmd = ClaudeEngine(claude_profile(tmp_path), SETTINGS).build_command(
            request(tools=ToolSelection.allow(["Read"]))
        )
        assert cmd[cmd.index("--allowedTools") + 1] == "Read"


class Test정책_출력을_옮기는_자리는_하나다:
    """정책이 낸 이름 목록을 선택으로 옮기는 규칙이 호출자마다 따로 있으면
    한쪽만 빈 목록을 방어한다. 감시 경로가 그래서 ValueError 를 낼 수 있었다
    (리뷰 2026-09-19)."""

    def test_이름이_있으면_허용목록이다(self) -> None:
        선택 = ToolSelection.from_names(("Read", "Grep"))
        assert 선택.access is ToolAccess.ALLOWLIST
        assert 선택.names == ("Read", "Grep")

    def test_이름이_없으면_제한_없음이다(self) -> None:
        """정책이 아무것도 안 낸 것은 금지가 아니다. 금지는 호출자가 밝힌다."""
        assert ToolSelection.from_names(()).access is ToolAccess.UNRESTRICTED

    def test_감시_경로가_빈_목록에도_안_터진다(self) -> None:
        import inspect

        from slack_cli_agent.core import application

        본문 = inspect.getsource(application.Application._watch_run_check)
        assert "ToolSelection.allow(" not in 본문
        assert "from_names" in 본문


class Test강제할_수_없는_엔진은_말로_전한다:
    """claude 만 도구 집합을 비울 수 있다. 나머지 둘이 아무 말도 안 하면
    금지를 요구한 턴이 그 엔진에서는 아무 제약 없이 돈다 (sca-97n).

    이것은 강제가 아니다. 축 선언은 그대로 두고 감사에 강등이 남는다."""

    def _요청(self, **kw):
        from test_engine import request

        return request(**kw)

    def _엔진들(self, tmp_path):
        from test_engine import claude_profile, codex_profile, gemini_profile
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine
        from slack_cli_agent.engine.codex import CodexEngine
        from slack_cli_agent.engine.gemini import GeminiEngine

        return (ClaudeEngine(claude_profile(tmp_path), SETTINGS),
                CodexEngine(codex_profile(tmp_path), SETTINGS),
                GeminiEngine(gemini_profile(tmp_path), SETTINGS))

    def test_claude_는_말로_안_전한다(self, tmp_path) -> None:
        """실제로 막으므로 지침에 한 줄을 더하면 바이트만 늘어난다."""
        claude, _, _ = self._엔진들(tmp_path)
        assert claude.tool_ban_note(self._요청(tools=ToolSelection.forbid_all())) == ""

    def test_codex_와_gemini_는_전한다(self, tmp_path) -> None:
        _, codex, gemini = self._엔진들(tmp_path)
        요청 = self._요청(tools=ToolSelection.forbid_all())
        for 엔진 in (codex, gemini):
            assert "도구" in 엔진.tool_ban_note(요청)

    def test_금지가_아니면_아무_말도_안_한다(self, tmp_path) -> None:
        _, codex, gemini = self._엔진들(tmp_path)
        for 엔진 in (codex, gemini):
            for 선택 in (ToolSelection.unrestricted(), ToolSelection.allow(["Read"])):
                assert 엔진.tool_ban_note(self._요청(tools=선택)) == ""

    def test_codex_는_턴_프롬프트로_전한다(self, tmp_path) -> None:
        """developer_instructions 는 세션 첫 턴에 고정된다. 거기 실으면 금지가
        세션 수명 동안 남아, 도구를 허용한 뒤 턴까지 묶인다."""
        _, codex, _ = self._엔진들(tmp_path)
        cmd = codex.build_command(self._요청(tools=ToolSelection.forbid_all()))
        assert "도구" in cmd[cmd.index("--") + 1]
        assert not any("도구를 하나도" in tok for tok in cmd[: cmd.index("--")])

    def test_codex_는_시스템_지침이_비어도_전한다(self, tmp_path) -> None:
        """리뷰 경로가 빈 시스템 지침으로 돈다. 지침 인자에 실으면 그 경로에서
        금지가 통째로 사라진다."""
        _, codex, _ = self._엔진들(tmp_path)
        cmd = codex.build_command(
            self._요청(tools=ToolSelection.forbid_all(), system_prompt="")
        )
        assert "도구" in cmd[cmd.index("--") + 1]

    def test_codex_첫_턴_금지문은_입력_표식_앞에_온다(self, tmp_path) -> None:
        """표식 뒤는 사용자 입력 자리다. 제약이 그 뒤로 가면 입력이 제약을
        덮어쓰는 것처럼 읽힌다."""
        from slack_cli_agent.engine.base import UNTRUSTED_INPUT_MARK

        _, codex, _ = self._엔진들(tmp_path)
        prompt = codex.build_command(
            self._요청(tools=ToolSelection.forbid_all())
        )[-1]
        assert prompt.index("도구를 하나도") < prompt.index(UNTRUSTED_INPUT_MARK)

    def test_codex_재개_턴의_금지문도_표식_앞이다(self, tmp_path) -> None:
        from slack_cli_agent.engine.base import UNTRUSTED_INPUT_MARK

        _, codex, _ = self._엔진들(tmp_path)
        prompt = codex.build_command(
            self._요청(tools=ToolSelection.forbid_all(), resume=True)
        )[-1]
        assert prompt.index("도구를 하나도") < prompt.index(UNTRUSTED_INPUT_MARK)

    def test_codex_전송량에_그_바이트가_들어간다(self, tmp_path) -> None:
        """빈 시스템 지침이면 전송량을 0 으로 되돌리던 자리다. 금지문을 보내고도
        0 으로 세면 감사 수치가 실제와 어긋난다."""
        _, codex, _ = self._엔진들(tmp_path)
        for 시스템지침 in ("시스템 지침", ""):
            금지 = self._요청(tools=ToolSelection.forbid_all(), system_prompt=시스템지침)
            없음 = self._요청(tools=ToolSelection.unrestricted(), system_prompt=시스템지침)
            차이 = (codex.footprint_for(금지).adapter_added_bytes
                    - codex.footprint_for(없음).adapter_added_bytes)
            assert 차이 > 0
            보낸것 = codex.build_command(금지)[-1]
            안보낸것 = codex.build_command(없음)[-1]
            assert 차이 == len(보낸것.encode("utf-8")) - len(안보낸것.encode("utf-8"))

    def test_금지문이_뒤_입력으로_풀리지_않는다고_적는다(self, tmp_path) -> None:
        """사용자 입력이 제약 해제를 요구하는 것이 가장 흔한 우회다."""
        _, codex, _ = self._엔진들(tmp_path)
        assert "해제" in codex.tool_ban_note(self._요청(tools=ToolSelection.forbid_all()))

    def test_codex_재개_턴에도_실린다(self, tmp_path) -> None:
        """재개는 지침을 프롬프트로 다시 싣는다. 거기에 빠지면 두 번째 턴부터
        금지가 사라진다."""
        _, codex, _ = self._엔진들(tmp_path)
        cmd = codex.build_command(self._요청(tools=ToolSelection.forbid_all(), resume=True))
        assert any("도구" in tok for tok in cmd[cmd.index("--") :])

    def test_gemini_의_프롬프트에_실린다(self, tmp_path) -> None:
        _, _, gemini = self._엔진들(tmp_path)
        cmd = gemini.build_command(self._요청(tools=ToolSelection.forbid_all()))
        assert "도구" in cmd[cmd.index("-p") + 1]

    def test_전송량_계산에_그_바이트가_들어간다(self, tmp_path) -> None:
        """감사가 세는 바이트와 실제로 보낸 바이트가 어긋나면 안 된다."""
        _, _, gemini = self._엔진들(tmp_path)
        금지 = self._요청(tools=ToolSelection.forbid_all())
        없음 = self._요청(tools=ToolSelection.unrestricted())
        차이 = (gemini.footprint_for(금지).adapter_added_bytes
                - gemini.footprint_for(없음).adapter_added_bytes)
        assert 차이 == len(gemini.tool_ban_note(금지).encode("utf-8"))


class Test허용목록은_tools_로_건다:
    """--allowedTools 는 자동 승인을 더할 뿐 목록 밖 도구를 닫지 않는다.

    2026-09-20 실측 (claude 2.1.263). --allowedTools "Read" 만 주고 Bash 를
    시키면 Bash 가 그대로 실행된다. --setting-sources project 와 빈 permissions
    를 함께 줘도 같다. 같은 경로에 deny 를 넣으면 도구 자체가 사라지므로 그
    설정이 읽히지 않아서가 아니다 (sca-6ewc).

    닫는 수단은 --tools 다. "Specify the list of available tools from the
    built-in set" 이라고 도움말이 적고, 실제로 --tools "Read,Grep,Glob" 에서
    Write 호출이 'No such tool available' 로 막혔다. 붙은 도구를 물으면
    Glob, Grep, Read 세 개만 답한다.
    """

    def cmd(self, tmp_path, selection):
        from test_engine import claude_profile, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        return ClaudeEngine(claude_profile(tmp_path), SETTINGS).build_command(
            request(tools=selection)
        )

    def tools_arg(self, cmd) -> str:
        return cmd[cmd.index("--tools") + 1]

    def test_허용한_이름만_붙인다(self, tmp_path) -> None:
        cmd = self.cmd(tmp_path, ToolSelection.allow(["Read", "Grep", "Glob"]))
        assert self.tools_arg(cmd) == "Read,Grep,Glob"

    def test_자동_승인도_함께_넘긴다(self, tmp_path) -> None:
        """--tools 는 도구를 붙이는 것이고 승인은 별개다."""
        cmd = self.cmd(tmp_path, ToolSelection.allow(["Read", "Grep"]))
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"

    def test_MCP_이름은_tools_에서_뺀다(self, tmp_path) -> None:
        """--tools 는 내장 도구 집합만 받는다. MCP 이름을 섞어도 오류는 안 나지만
        그 자리에서 뜻이 없으므로 승인 쪽에만 남긴다."""
        cmd = self.cmd(tmp_path, ToolSelection.allow(["Read", "mcp__slack__channels_list"]))
        assert self.tools_arg(cmd) == "Read"
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,mcp__slack__channels_list"

    def test_MCP_만_허용하면_tools_를_비운다(self, tmp_path) -> None:
        """내장 도구를 하나도 안 허용한 것이므로 빈 값이 맞다. 인자를 빼면
        전체 내장 도구가 열린다."""
        cmd = self.cmd(tmp_path, ToolSelection.allow(["mcp__slack__channels_list"]))
        assert self.tools_arg(cmd) == ""

    def test_전부_금지는_와일드카드_하나를_쓴다(self, tmp_path) -> None:
        """실측한 명령 모양이 그것이다."""
        cmd = self.cmd(tmp_path, ToolSelection.forbid_all())
        assert cmd[cmd.index("--disallowedTools") + 1] == "*"
        assert "--tools" not in cmd

    def test_제한_없음에는_도구_인자가_없다(self, tmp_path) -> None:
        cmd = self.cmd(tmp_path, ToolSelection.unrestricted())
        assert "--tools" not in cmd
        assert "--disallowedTools" not in cmd
        assert "--allowedTools" not in cmd


class Test전역_MCP_서버가_안_붙는다:
    """--strict-mcp-config 가 프로필에 MCP 서버가 있을 때만 붙어 있었다.

    서버가 없는 프로필에서는 사용자 ~/.claude.json 의 전역 서버가 전부 실린다.
    2026-09-20 측정 시점에 9개였다. --tools "Read,Grep,Glob" 로 내장 도구를
    닫아 놓아도 모델이 mcp__playwright__browser_run_code_unsafe 를 대신 호출했다.
    그 호출은 dontAsk 가 거부했지만 승인 규칙에 걸린 것이라, 이름이 허용목록이나
    settings 의 allow 에 들어가면 열린다 (sca-mo4g).
    """

    def cmd(self, tmp_path, **kwargs):
        from test_engine import profile_with, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        profile = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            tmp_path=tmp_path,
            **kwargs,
        )
        return ClaudeEngine(profile, SETTINGS).build_command(request())

    def test_서버가_없어도_붙인다(self, tmp_path) -> None:
        assert "--strict-mcp-config" in self.cmd(tmp_path)

    def test_서버가_있으면_그것만_붙인다(self, tmp_path) -> None:
        cmd = self.cmd(tmp_path, mcp_servers={"jira": {"command": "jira-mcp"}})
        assert "--strict-mcp-config" in cmd
        assert "jira" in cmd[cmd.index("--mcp-config") + 1]

    def test_서버가_없으면_설정_인자를_안_붙인다(self, tmp_path) -> None:
        """빈 mcpServers 를 넘길 자리가 아니다. 제한만 걸면 된다."""
        assert "--mcp-config" not in self.cmd(tmp_path)


class Test프로필_MCP_도구도_허용목록_밖이면_닫는다:
    """--tools 는 내장 도구 집합만 다룬다. 프로필이 MCP 서버를 붙였으면 그
    도구들은 허용목록에 없어도 모델의 도구 목록에 남는다.

    dontAsk 가 승인 단계에서 거부하지만 그것은 승인 규칙이라, 이름이
    --allowedTools 나 settings 의 allow 에 들어가면 열린다. 모델이 존재를
    보고 호출을 시도해 턴을 쓰는 것도 그대로다 (제미나이 리뷰).

    2026-09-20 실측. --tools "Read,Grep,Glob" 만 준 turn 이 붙은 도구를
    나열하면 전역 MCP 도구가 줄줄이 나온다. --disallowedTools "mcp__*" 를
    더하면 Glob, Grep, Read 만 남는다.
    """

    def cmd(self, tmp_path, names, **kwargs):
        from test_engine import profile_with, request
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.claude import ClaudeEngine

        profile = profile_with(
            {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
            tmp_path=tmp_path,
            **kwargs,
        )
        return ClaudeEngine(profile, SETTINGS).build_command(
            request(tools=ToolSelection.allow(names))
        )

    def denied(self, cmd) -> str | None:
        return cmd[cmd.index("--disallowedTools") + 1] if "--disallowedTools" in cmd else None

    def test_MCP_를_안_허용했으면_통째로_닫는다(self, tmp_path) -> None:
        assert self.denied(self.cmd(tmp_path, ["Read", "Grep"])) == "mcp__*"

    def test_서버가_붙어_있어도_닫는다(self, tmp_path) -> None:
        """서버를 붙인 것과 이번 턴이 그 도구를 써도 되는 것은 다르다."""
        cmd = self.cmd(tmp_path, ["Read"], mcp_servers={"jira": {"command": "jira-mcp"}})
        assert self.denied(cmd) == "mcp__*"

    def test_허용한_서버만_남기고_나머지_서버를_닫는다(self, tmp_path) -> None:
        """도구 하나를 허용했다고 전체 MCP 차단을 생략하면 fail-open 이다
        (코덱스 리뷰). 서버 단위까지는 좁힐 수 있다.

        2026-09-20 실측. mcp__playwright__* 를 닫으면 그 서버의 도구만
        사라지고 mcp__playwright-daangn__ 은 남는다. 이름이 겹치는 접두어가
        아니라 서버 단위로 끊긴다.
        """
        cmd = self.cmd(
            tmp_path, ["Read", "mcp__jira__jira_search"],
            mcp_servers={"jira": {"command": "jira-mcp"}, "github": {"command": "gh-mcp"}},
        )
        assert self.denied(cmd) == "mcp__github__*"

    def test_허용한_서버_안의_다른_도구는_못_닫는다(self, tmp_path) -> None:
        """서버가 어떤 도구를 내는지는 붙여 봐야 안다. 그 경계는 승인 목록이
        맡는다. --disallowedTools 로 서버를 닫고 --allowedTools 로 이름 하나를
        되살리는 것은 안 된다 - 와일드카드가 이긴다(실측)."""
        cmd = self.cmd(
            tmp_path, ["mcp__jira__jira_search"],
            mcp_servers={"jira": {"command": "jira-mcp"}},
        )
        assert self.denied(cmd) is None

    def test_서버_이름에_하이픈이_있어도_닫는다(self, tmp_path) -> None:
        cmd = self.cmd(
            tmp_path, ["mcp__jira__jira_search"],
            mcp_servers={"jira": {"command": "j"}, "local-rag": {"command": "r"}},
        )
        assert self.denied(cmd) == "mcp__local-rag__*"

    def test_프로필에_없는_서버를_허용해도_전체를_닫는다(self, tmp_path) -> None:
        """허용 이름이 어느 실제 서버에도 안 붙으면 열어 둘 서버가 없다.
        여집합만 계산하면 빈 목록이 나와 아무것도 안 닫히는 fail-open 이
        된다 (코덱스 리뷰)."""
        cmd = self.cmd(tmp_path, ["Read", "mcp__ghost__x"])
        assert self.denied(cmd) == "mcp__*"

    def test_이름에_도구_부분이_없으면_허용으로_안_친다(self, tmp_path) -> None:
        """mcp__jira 는 서버도 도구도 안 가리킨다. 이것을 jira 허용으로
        읽으면 그 서버 전체가 열린다 (코덱스 리뷰)."""
        cmd = self.cmd(
            tmp_path, ["mcp__jira"],
            mcp_servers={"jira": {"command": "j"}},
        )
        assert self.denied(cmd) == "mcp__*"

    def test_서버_이름에_밑줄_둘이_있어도_맞게_가른다(self, tmp_path) -> None:
        """이름을 왼쪽부터 쪼개면 foo__bar 서버가 foo 로 읽힌다. 프로필의
        서버 이름으로 맞춰야 한다 (코덱스 리뷰)."""
        cmd = self.cmd(
            tmp_path, ["mcp__foo__bar__search"],
            mcp_servers={"foo__bar": {"command": "f"}, "other": {"command": "o"}},
        )
        assert self.denied(cmd) == "mcp__other__*"

    def test_꺼진_서버는_열린_것으로_안_친다(self, tmp_path) -> None:
        """disabled 서버는 --mcp-config 에 안 들어가므로 붙지 않는다.
        그 이름으로 허용해도 열어 둘 서버가 없다."""
        cmd = self.cmd(
            tmp_path, ["mcp__jira__x"],
            mcp_servers={"jira": {"command": "j", "disabled": True}, "gh": {"command": "g"}},
        )
        assert self.denied(cmd) == "mcp__*"

    def test_닫을_서버가_여럿이면_모두_적는다(self, tmp_path) -> None:
        cmd = self.cmd(
            tmp_path, ["mcp__jira__x"],
            mcp_servers={"jira": {"command": "j"}, "b": {"command": "b"}, "a": {"command": "a"}},
        )
        assert self.denied(cmd) == "mcp__a__*,mcp__b__*"


class Test허용목록을_못_거는_엔진은_말로_적는다:
    """codex 와 gemini 는 ALLOWLIST 요청에 아무 인자도 못 건다. claude 만
    --tools 로 실제로 닫는다. 강제가 없는 자리에 최소한 문구는 두어 세 엔진이
    같은 요청을 같은 방향으로 다루게 한다 (sca-f9k0).

    문구는 도구 이름이 아니라 동작으로 쓴다. codex 의 도구는 셸 하나라
    "Read, Grep, Glob 만 쓴다" 가 그 엔진에서 뜻이 없다.
    """

    def engine(self, restriction):
        from test_engine import profile_with
        from test_engine_capability import SETTINGS

        from slack_cli_agent.engine.base import Engine
        from slack_cli_agent.engine.capability import EngineCapabilities

        class _Fake(Engine):
            name = "fake"
            capabilities = EngineCapabilities(tool_restriction=restriction)

            def build_command(self, request):
                return []

            def parse(self, stdout: str, stderr: str, returncode: int):
                raise NotImplementedError

            def new_session_id(self):
                return "s"

            def detect_usage_limit(self, response):
                return None

        profile = profile_with({"type": "claude", "binary": "x", "model": "m"})
        return _Fake(profile, SETTINGS)

    def note(self, restriction, **kwargs):
        from test_engine import request as make_request

        return self.engine(restriction).tool_allow_note(make_request(**kwargs))

    def test_읽기만_허용하면_읽기_전용이라고_적는다(self) -> None:
        note = self.note(ToolRestriction.NONE, tools=ToolSelection.allow(["Read", "Grep"]))
        assert "읽기" in note
        assert "쓰지 않는다" in note or "하지 않는다" in note

    def test_쓰기가_섞이면_허용한_것을_나열한다(self) -> None:
        note = self.note(ToolRestriction.NONE, tools=ToolSelection.allow(["Read", "Write"]))
        assert "Write" in note
        assert "Read" in note

    def test_해제_요구를_따르지_않는다고_적는다(self) -> None:
        """뒤에 오는 입력이 제약을 푸는 형태를 막는다. TOOL_BAN_NOTE 와 같다."""
        note = self.note(ToolRestriction.NONE, tools=ToolSelection.allow(["Read"]))
        assert "해제" in note

    def test_실제로_거는_엔진에는_안_붙인다(self) -> None:
        """claude 는 --tools 로 닫으므로 문구가 중복이다."""
        note = self.note(
            ToolRestriction.EXACT_ALLOWLIST, tools=ToolSelection.allow(["Read"]),
        )
        assert note == ""

    def test_제한_없는_요청에는_안_붙인다(self) -> None:
        assert self.note(ToolRestriction.NONE, tools=ToolSelection()) == ""

    def test_전부_금지에는_안_붙인다(self) -> None:
        """그 자리는 TOOL_BAN_NOTE 가 맡는다. 둘이 함께 나가면 어긋난다."""
        assert self.note(ToolRestriction.NONE, tools=ToolSelection.forbid_all()) == ""

    def test_집계에_이_문구가_들어간다(self) -> None:
        """프롬프트에 실려 나가는 바이트는 전부 세어야 한다."""
        from test_engine import request as make_request

        engine = self.engine(ToolRestriction.NONE)
        req = make_request(tools=ToolSelection.allow(["Read"]))
        assert engine.footprint_for(req).adapter_added_bytes >= len(
            engine.tool_allow_note(req).encode("utf-8")
        )
