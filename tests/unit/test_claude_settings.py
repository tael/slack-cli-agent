"""봇 전용 settings 를 한 값으로 합치는 자리.

claude 의 --settings 는 값 하나만 받는다(2.1.263 도움말). 봇 기본 settings,
신뢰 수준별 덧씌움, 요청마다 다른 진행 훅 세 조각이 한 JSON 으로 나가야
한다. 출처가 여럿일 때 claude 가 어떻게 합치는지는 공식 문서가 정하지만,
한 값 안에서 합치는 것은 우리 몫이다.
"""

from __future__ import annotations

import pytest

from slack_cli_agent.engine.claude_settings import merge_settings


class Test병합규칙:
    def test_없는_키는_그대로_남는다(self) -> None:
        assert merge_settings({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_중첩_사전은_재귀로_합친다(self) -> None:
        합친것 = merge_settings({"permissions": {"deny": ["Bash"]}}, {"permissions": {"defaultMode": "dontAsk"}})
        assert 합친것 == {"permissions": {"deny": ["Bash"], "defaultMode": "dontAsk"}}

    def test_목록은_이어_붙인다(self) -> None:
        """deny 는 순서에 뜻이 없다. 덮어쓰면 한쪽이 통째로 사라진다."""
        합친것 = merge_settings({"permissions": {"deny": ["Bash"]}}, {"permissions": {"deny": ["Write"]}})
        assert 합친것["permissions"]["deny"] == ["Bash", "Write"]

    def test_같은_항목은_한_번만_남는다(self) -> None:
        합친것 = merge_settings({"deny": ["Bash", "Write"]}, {"deny": ["Write", "Edit"]})
        assert 합친것["deny"] == ["Bash", "Write", "Edit"]

    def test_사전이_든_목록도_중복을_거른다(self) -> None:
        """훅 그룹은 사전이라 집합으로 못 거른다."""
        그룹 = {"matcher": "*", "hooks": []}
        assert merge_settings({"h": [그룹]}, {"h": [dict(그룹)]})["h"] == [그룹]

    def test_훅_그룹은_이어_붙인다(self) -> None:
        """운영자의 PreToolUse 훅과 진행 훅이 둘 다 남아야 한다. 한쪽으로
        덮으면 진행 표시가 조용히 죽는다."""
        기본 = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "운영자"}]}]}}
        훅 = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "진행"}]}]}}
        assert len(merge_settings(기본, 훅)["hooks"]["PreToolUse"]) == 2

    def test_스칼라는_나중_값이_이긴다(self) -> None:
        assert merge_settings({"m": "a"}, {"m": "b"})["m"] == "b"

    def test_원본을_안_고친다(self) -> None:
        기본 = {"permissions": {"deny": ["Bash"]}}
        merge_settings(기본, {"permissions": {"deny": ["Write"]}})
        assert 기본 == {"permissions": {"deny": ["Bash"]}}

    def test_여러_조각을_왼쪽부터_합친다(self) -> None:
        assert merge_settings({"m": "a"}, {"m": "b"}, {"m": "c"})["m"] == "c"

    def test_빈_조각은_건너뛴다(self) -> None:
        assert merge_settings({}, {"a": 1}, {}) == {"a": 1}

    def test_종류가_다르면_나중_것이_이긴다(self) -> None:
        """설정 파일이 잘못 쓰여도 합치다 예외로 죽지 않는다."""
        assert merge_settings({"a": {"b": 1}}, {"a": [1]})["a"] == [1]


class Test파일을_읽는다:
    def test_없는_파일은_빈_조각이다(self, tmp_path) -> None:
        """운영물 부재로 기동이 막히면 안 된다."""
        from slack_cli_agent.engine.claude_settings import load_settings_file

        assert load_settings_file(tmp_path / "없다.json") == {}

    def test_읽은_내용을_그대로_낸다(self, tmp_path) -> None:
        from slack_cli_agent.engine.claude_settings import load_settings_file

        path = tmp_path / "s.json"
        path.write_text('{"permissions": {"deny": ["Bash"]}}', encoding="utf-8")
        assert load_settings_file(path) == {"permissions": {"deny": ["Bash"]}}

    def test_깨진_파일은_ConfigError_다(self, tmp_path) -> None:
        """무시하면 deny 가 빠진 채로 도는데 로그에 실패로 안 남는다."""
        from slack_cli_agent.core.errors import ConfigError
        from slack_cli_agent.engine.claude_settings import load_settings_file

        path = tmp_path / "s.json"
        path.write_text("json 이 아니다", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings_file(path)

    def test_사전이_아니면_ConfigError_다(self, tmp_path) -> None:
        from slack_cli_agent.core.errors import ConfigError
        from slack_cli_agent.engine.claude_settings import load_settings_file

        path = tmp_path / "s.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings_file(path)


class Test모양이_어긋나면_거부한다:
    """종류가 다르면 나중 값이 이기는 병합 규칙 때문에, 덧씌움의 permissions
    가 실수로 목록이면 기본 파일의 deny 가 통째로 사라진다. 합치기 전에
    거른다 (codex 리뷰).
    """

    @staticmethod
    def 쓴다(tmp_path, 내용: str):
        path = tmp_path / "s.json"
        path.write_text(내용, encoding="utf-8")
        return path

    @pytest.mark.parametrize("내용", [
        '{"permissions": []}',
        '{"permissions": "dontAsk"}',
        '{"permissions": {"deny": "Bash"}}',
        '{"permissions": {"allow": {"a": 1}}}',
        '{"permissions": {"ask": 1}}',
        '{"hooks": []}',
        '{"hooks": {"PreToolUse": {"matcher": "*"}}}',
        '{"env": []}',
    ])
    def test_거부한다(self, tmp_path, 내용: str) -> None:
        from slack_cli_agent.core.errors import ConfigError
        from slack_cli_agent.engine.claude_settings import load_settings_file

        with pytest.raises(ConfigError):
            load_settings_file(self.쓴다(tmp_path, 내용))

    @pytest.mark.parametrize("내용", [
        '{"permissions": {"deny": ["Bash"], "allow": [], "ask": []}}',
        '{"hooks": {"PreToolUse": [{"matcher": "*"}]}}',
        '{"env": {"A": "1"}}',
        '{"모르는키": 1}',
    ])
    def test_통과한다(self, tmp_path, 내용: str) -> None:
        from slack_cli_agent.engine.claude_settings import load_settings_file

        assert load_settings_file(self.쓴다(tmp_path, 내용))

    def test_어느_키가_문제인지_적는다(self, tmp_path) -> None:
        """파일 하나에 키가 수십 개다. 이름이 없으면 운영자가 못 찾는다."""
        from slack_cli_agent.core.errors import ConfigError
        from slack_cli_agent.engine.claude_settings import load_settings_file

        with pytest.raises(ConfigError, match="deny"):
            load_settings_file(self.쓴다(tmp_path, '{"permissions": {"deny": "Bash"}}'))
