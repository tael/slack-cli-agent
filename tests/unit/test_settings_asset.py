"""동봉한 엔진 settings 본보기와 그것을 까는 명령.

settings 는 운영물이라 저장소에 없고, 없으면 엔진이 빈 조각으로 돌아
permissions.deny 가 하나도 안 걸린다(sca-j2zp). 본보기 내용이 실제로
load_settings_file 의 모양 검증을 통과하는지, 깐 뒤 합친 결과에 deny 가
남는지를 손 확인이 아니라 여기서 본다.

원본 runtime/bot-settings*.json 을 그대로 쓰면 안 되는 이유도 함께 건다 -
그 파일의 PreToolUse 훅은 원본 고정 경로의 progress_hook.py 를 인자 없이
부르는데, 이 판은 요청마다 진행 훅을 따로 붙이므로 둘 다 실행된다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.claude import progress_hook_settings
from slack_cli_agent.engine.claude_settings import load_settings_file, merge_settings

_REPO = Path(__file__).resolve().parents[2]
_ASSET_DIR = _REPO / "src" / "slack_cli_agent" / "assets" / "engine"

_SPEC = importlib.util.spec_from_file_location(
    "install_settings", _REPO / "tools" / "install-settings.py"
)
assert _SPEC and _SPEC.loader
install_settings = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install_settings)

공통 = "settings-claude.json"
일반 = "settings-claude.general.json"


class Test본보기내용:
    @pytest.mark.parametrize("이름", [공통, 일반])
    def test_모양_검증을_통과한다(self, 이름: str) -> None:
        assert load_settings_file(_ASSET_DIR / 이름)

    def test_공통_파일에는_도구_전면_차단이_없다(self) -> None:
        """공통 파일은 owner·trusted 에도 걸린다. 여기에 Bash 를 넣으면
        소유자가 아무 도구도 못 쓴다."""
        deny = load_settings_file(_ASSET_DIR / 공통)["permissions"]["deny"]
        assert not {"Bash", "Edit", "Write", "NotebookEdit"} & set(deny)

    def test_일반_덧씌움은_도구_넷만_막는다(self) -> None:
        본보기 = load_settings_file(_ASSET_DIR / 일반)
        assert 본보기 == {"permissions": {"deny": ["Bash", "Edit", "Write", "NotebookEdit"]}}

    def test_훅을_두지_않는다(self) -> None:
        """진행 훅은 요청마다 엔진이 붙인다. 본보기에 훅을 두면 한 턴에 둘이
        실행된다."""
        for 이름 in (공통, 일반):
            assert "hooks" not in load_settings_file(_ASSET_DIR / 이름)

    def test_조직_고유값이_없다(self) -> None:
        """설치물이라 사람 이름이 든 홈 경로가 박히면 안 된다."""
        for 이름 in (공통, 일반):
            assert "/Users/" not in (_ASSET_DIR / 이름).read_text(encoding="utf-8")


class Test권한경로표기:
    def test_홈_아래는_물결로_적는다(self) -> None:
        표기 = install_settings.permission_path(Path("/home/u/.shinji"), Path("/home/u"))
        assert 표기 == "~/.shinji"

    def test_홈_밖은_두겹_빗금_절대경로다(self) -> None:
        표기 = install_settings.permission_path(Path("/srv/shinji"), Path("/home/u"))
        assert 표기 == "//srv/shinji"


class Test설치:
    def test_상태_디렉터리에_두_파일을_만든다(self, tmp_path: Path) -> None:
        만든것, 건너뛴것 = install_settings.install(tmp_path / ".shinji", home=tmp_path)
        assert [p.name for p in 만든것] == [공통, 일반]
        assert 건너뛴것 == []

    def test_이미_있는_파일을_덮지_않는다(self, tmp_path: Path) -> None:
        상태 = tmp_path / ".shinji"
        (상태 / "engine").mkdir(parents=True)
        운영물 = 상태 / "engine" / 공통
        운영물.write_text('{"permissions": {"deny": ["Read(//etc/**)"]}}', encoding="utf-8")
        만든것, 건너뛴것 = install_settings.install(상태, home=tmp_path)
        assert 운영물 in 건너뛴것
        assert 운영물 not in 만든것
        assert json.loads(운영물.read_text(encoding="utf-8"))["permissions"]["deny"] == [
            "Read(//etc/**)"
        ]

    def test_자리표시자가_상태_경로로_바뀐다(self, tmp_path: Path) -> None:
        상태 = tmp_path / ".shinji"
        install_settings.install(상태, home=tmp_path)
        deny = load_settings_file(상태 / "engine" / 공통)["permissions"]["deny"]
        assert "Read(~/.shinji/credentials.json)" in deny
        assert not any(install_settings.PLACEHOLDER in 항목 for 항목 in deny)

    def test_모르는_엔진은_거부한다(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            install_settings.install(tmp_path / ".shinji", engine="codex", home=tmp_path)


class Test합친결과:
    def test_일반_요청은_도구_넷이_막힌다(self, tmp_path: Path) -> None:
        """엔진이 실제로 하는 합침과 같은 순서로 본다 - 공통, 수준별 덧씌움,
        요청별 진행 훅."""
        상태 = tmp_path / ".shinji"
        install_settings.install(상태, home=tmp_path)
        합친것 = merge_settings(
            load_settings_file(상태 / "engine" / 공통),
            load_settings_file(상태 / "engine" / 일반),
            progress_hook_settings(tmp_path / "progress.log"),
        )
        권한 = 합친것["permissions"]
        assert 권한["defaultMode"] == "dontAsk"
        assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(권한["deny"])
        assert "Read(~/.ssh/**)" in 권한["deny"]
        assert len(합친것["hooks"]["PreToolUse"]) == 1

    def test_소유자_요청에는_덧씌움이_없어_도구가_열린다(self, tmp_path: Path) -> None:
        상태 = tmp_path / ".shinji"
        install_settings.install(상태, home=tmp_path)
        # owner·trusted 덧씌움 파일은 없다. load_settings_file 이 빈 조각을 낸다.
        덧씌움 = load_settings_file(상태 / "engine" / "settings-claude.owner.json")
        합친것 = merge_settings(load_settings_file(상태 / "engine" / 공통), 덧씌움)
        assert "Bash" not in 합친것["permissions"]["deny"]

    def test_깨진_운영물은_조용히_넘어가지_않는다(self, tmp_path: Path) -> None:
        깨진것 = tmp_path / "settings-claude.json"
        깨진것.write_text('{"permissions": {"deny": {}}}', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings_file(깨진것)


class Test까다로운_경로:
    """따옴표·백슬래시·개행이 든 상태 경로에서도 JSON 이 깨지지 않는다(sca-pp19).

    자리표시자를 원문 문자열에 그대로 끼워 넣으면 이런 글자가 JSON 문법을
    깨뜨린다. 값 단위로 치환해야 한다.
    """

    @pytest.mark.parametrize(
        "이름",
        [
            '.shi"nji',
            ".shi\\nji",
            ".shi\nnji",
            ".shi nji",
        ],
    )
    def test_깨지는_글자가_들어도_설치된다(self, tmp_path: Path, 이름: str) -> None:
        상태 = tmp_path / 이름
        install_settings.install(상태, home=tmp_path)
        deny = load_settings_file(상태 / "engine" / 공통)["permissions"]["deny"]
        assert f"Read(~/{이름}/credentials.json)" in deny

    def test_상대_경로는_절대_경로로_기록된다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        install_settings.install(Path(".shinji"), home=tmp_path)
        deny = load_settings_file(tmp_path / ".shinji" / "engine" / 공통)["permissions"]["deny"]
        assert "Read(~/.shinji/credentials.json)" in deny
