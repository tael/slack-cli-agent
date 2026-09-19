"""요청이 쓸 수 있는 도구를 세 가지 상태로 표현한다 (sca-0a7).

빈 목록 하나가 '제한 없음' 과 '도구 전부 금지' 를 동시에 뜻해서 호출자가
후자를 표현할 수 없었다. learning 의 야간 배치가 그 자리다 - 주석은 '도구
없음' 이라고 적혀 있는데 실제로는 전체 도구가 열린 채 돌았다.

2026-09-19 실측 근거 : claude 는 --disallowedTools=* 로 도구 집합을 비운다
(디버그 로그의 'Dynamic tool loading' 줄이 사라지고 tool_use 가 4회 모두
0건이다). 개별 이름 거부는 다른 도구로 우회되므로 강제가 아니다. codex 와
gemini 에는 대응 수단이 없다.
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
