"""사내 도메인 지식 적재 — 프로필 domain_file 배선과 적재 조건."""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.config.profile import Profile
from slack_cli_agent.core.pipeline import _domain_applies
from slack_cli_agent.prompt.knowledge import KnowledgeLoader

MINIMAL_PROFILE = {
    "name": "example",
    "primary_engine": {
        "type": "claude",
        "binary": "claude",
        "model": "sonnet",
        "model_owner": "opus",
    },
    "owner_user_id": "U_OWNER",
    "troubleshoot_channel": "C_TROUBLE",
}


class Test도메인적재조건:
    def test_workdir가_없으면_적재하지_않는다(self) -> None:
        assert _domain_applies(None, Path("/Users/x")) is False

    def test_홈_아래_workdir면_적재하지_않는다(self) -> None:
        home = Path("/Users/x")
        assert _domain_applies(home / "bot" / "chat", home) is False

    def test_홈_밖_workdir면_적재한다(self) -> None:
        """실제 리포에서 일할 때만 사내 도메인 사실을 붙인다 — 원본과 같은 축."""
        assert _domain_applies(Path("/srv/repo"), Path("/Users/x")) is True


class Test프로필_domain_file:
    def test_domain_file를_읽어_경로로_둔다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict({**MINIMAL_PROFILE, "domain_file": str(tmp_path / "AGENTS.md")})
        assert profile.domain_file == tmp_path / "AGENTS.md"

    def test_domain_file가_없으면_None이다(self) -> None:
        assert Profile.from_dict(MINIMAL_PROFILE).domain_file is None

    def test_물결경로를_펼친다(self) -> None:
        profile = Profile.from_dict({**MINIMAL_PROFILE, "domain_file": "~/Jobs/AGENTS.md"})
        assert profile.domain_file is not None
        assert profile.domain_file.is_absolute()


class Test프로필_light_workdir:
    """light_context 채널의 비소유자 턴이 쓸 가벼운 작업 자리(A11). 조직값이라
    도메인·peer 와 같이 프로필에 두고, 없으면 None 으로 기능이 꺼진다."""

    def test_light_workdir를_읽어_경로로_둔다(self, tmp_path: Path) -> None:
        profile = Profile.from_dict({**MINIMAL_PROFILE, "light_workdir": str(tmp_path / "light")})
        assert profile.light_workdir == tmp_path / "light"

    def test_light_workdir가_없으면_None이다(self) -> None:
        assert Profile.from_dict(MINIMAL_PROFILE).light_workdir is None

    def test_물결경로를_펼친다(self) -> None:
        profile = Profile.from_dict({**MINIMAL_PROFILE, "light_workdir": "~/light"})
        assert profile.light_workdir is not None
        assert profile.light_workdir.is_absolute()


class Test도메인지식_적재:
    def _loader(self, tmp_path: Path, *, with_domain: bool) -> KnowledgeLoader:
        persona = tmp_path / "PERSONA.md"
        persona.write_text("나는 구치파치다.", encoding="utf-8")
        domain = None
        if with_domain:
            domain = tmp_path / "AGENTS.md"
            domain.write_text("MASTER_ORDER 는 정산 기준 주문이다.", encoding="utf-8")
        return KnowledgeLoader(persona, tmp_path / "knowledge", domain_file=domain)

    def test_도메인파일이_설정되고_플래그가_켜지면_붙는다(self, tmp_path: Path) -> None:
        text = self._loader(tmp_path, with_domain=True).persona_text(include_domain=True)
        assert "MASTER_ORDER" in text
        assert "구치파치" in text

    def test_플래그가_꺼지면_도메인은_빠진다(self, tmp_path: Path) -> None:
        text = self._loader(tmp_path, with_domain=True).persona_text(include_domain=False)
        assert "MASTER_ORDER" not in text

    def test_도메인파일이_없으면_플래그가_켜져도_페르소나만_나온다(self, tmp_path: Path) -> None:
        text = self._loader(tmp_path, with_domain=False).persona_text(include_domain=True)
        assert "MASTER_ORDER" not in text
        assert "구치파치" in text
