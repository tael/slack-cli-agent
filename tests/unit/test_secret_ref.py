"""프로필이 비밀값을 들고 있지 않게 하는 간접 표기 (sca-dn4).

MCP 서버의 env 는 프로필이 자격을 나르는 유일한 자리다. 로드 시 토큰 검사가
그 자리를 면제하므로, 평문으로 적으면 웹 콘솔이 읽고 다시 저장하는 파일에
그대로 남는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.config.profile import McpServerSpec
from slack_cli_agent.config.secret_ref import resolve_mapping
from slack_cli_agent.core.errors import ConfigError


class Test표기해석:
    def test_평범한_값은_그대로_둔다(self) -> None:
        assert resolve_mapping({"API_BASE": "https://x"}, where="서버 a") == {"API_BASE": "https://x"}

    def test_환경변수를_가리키면_그_값을_쓴다(self) -> None:
        결과 = resolve_mapping(
            {"GITHUB_TOKEN": "${env:GH}"}, where="서버 a", environ={"GH": "값"}
        )
        assert 결과 == {"GITHUB_TOKEN": "값"}

    def test_파일을_가리키면_그_내용을_쓴다(self, tmp_path: Path) -> None:
        path = tmp_path / "gh"
        path.write_text("값\n", encoding="utf-8")
        결과 = resolve_mapping({"GITHUB_TOKEN": f"${{file:{path}}}"}, where="서버 a")
        assert 결과 == {"GITHUB_TOKEN": "값"}

    def test_없는_환경변수는_이유와_자리를_들어_실패한다(self) -> None:
        with pytest.raises(ConfigError) as exc:
            resolve_mapping({"GITHUB_TOKEN": "${env:없음}"}, where="서버 a", environ={})
        assert "서버 a" in str(exc.value)
        assert "GITHUB_TOKEN" in str(exc.value)

    def test_없는_파일도_실패한다(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            resolve_mapping({"T": f"${{file:{tmp_path / '없음'}}}"}, where="서버 a")

    def test_빈_값은_못_찾은_것과_같이_다룬다(self, tmp_path: Path) -> None:
        """빈 토큰을 넘기면 MCP 서버가 인증 실패로만 알려 원인이 안 보인다."""
        path = tmp_path / "gh"
        path.write_text("\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            resolve_mapping({"T": f"${{file:{path}}}"}, where="서버 a")

    def test_값_가운데의_표기는_해석하지_않는다(self) -> None:
        """부분 치환을 허용하면 값이 조용히 잘못 조립된다. 전체가 표기일 때만 본다."""
        원본 = {"U": "https://x/${env:GH}"}
        assert resolve_mapping(원본, where="서버 a", environ={"GH": "값"}) == 원본


class Test서버_설정에서:
    def test_env_와_headers_둘_다_해석한다(self) -> None:
        server = McpServerSpec.from_dict(
            "깃허브",
            {"command": "x", "env": {"T": "${env:GH}"}, "headers": {"H": "${env:GH}"}},
        )
        assert server.resolved_env(environ={"GH": "값"}) == {"T": "값"}
        assert server.resolved_headers(environ={"GH": "값"}) == {"H": "값"}

    def test_실패_메시지가_어느_서버인지_말한다(self) -> None:
        server = McpServerSpec.from_dict("깃허브", {"command": "x", "env": {"T": "${env:없음}"}})
        with pytest.raises(ConfigError) as exc:
            server.resolved_env(environ={})
        assert "깃허브" in str(exc.value)
