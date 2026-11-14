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
소유자 = "settings-claude.owner.json"
신뢰 = "settings-claude.trusted.json"
운영자전권 = "settings-claude.operator_full.json"

#: 도구를 막지 않는 덧씌움 셋 — 공통 파일의 Bash/Edit/Write/NotebookEdit
#: 비차단을 그대로 둔다. 전면 차단은 general 하나뿐이다.
_비차단_덧씌움 = (소유자, 신뢰, 운영자전권)


class Test본보기내용:
    @pytest.mark.parametrize("이름", [공통, 일반, 소유자, 신뢰, 운영자전권])
    def test_모양_검증을_통과한다(self, 이름: str) -> None:
        """빈 조각(`{}`)도 모양 검증은 통과해야 한다 — load_settings_file 이
        아니라 파일 자체가 유효한 JSON 객체인지 본다."""
        assert isinstance(load_settings_file(_ASSET_DIR / 이름), dict)

    def test_공통_파일에는_도구_전면_차단이_없다(self) -> None:
        """공통 파일은 owner·trusted·operator_full 에도 걸린다. 여기에 Bash 를
        넣으면 소유자가 아무 도구도 못 쓴다."""
        deny = load_settings_file(_ASSET_DIR / 공통)["permissions"]["deny"]
        assert not {"Bash", "Edit", "Write", "NotebookEdit"} & set(deny)

    def test_일반_덧씌움은_도구_넷만_막는다(self) -> None:
        본보기 = load_settings_file(_ASSET_DIR / 일반)
        assert 본보기 == {
            "permissions": {"deny": ["Bash", "Edit", "Write", "NotebookEdit", "Read(~/.claude/**)"]}
        }

    @pytest.mark.parametrize("이름", _비차단_덧씌움)
    def test_도구를_막지_않는_덧씌움은_도구_넷을_안_막는다(self, 이름: str) -> None:
        """owner·trusted·operator_full 덧씌움은 전면 차단 넷(Bash/Edit/Write/
        NotebookEdit) 을 추가로 막지 않는다 — 막으면 각자의 설계 의도(오너
        전권, 이름으로 이미 열린 도구)가 깨진다."""
        본보기 = load_settings_file(_ASSET_DIR / 이름)
        deny = set((본보기.get("permissions") or {}).get("deny") or [])
        assert not {"Bash", "Edit", "Write", "NotebookEdit"} & deny

    def test_오너_덧씌움은_자격증명류만_막는다(self) -> None:
        """gaps_D 3절 — 오너는 Bash/Edit/Write 전부 열리되 자격증명 경로만
        deny. 공통 파일이 이미 ssh/aws/config/claude/Library/mcp.json/
        *credentials*/.env* 를 막으므로, 여기는 공통이 놓치는 자격증명류
        (키체인, *api*key*, *token* 파일, secret* 파일) 만 보탠다."""
        deny = load_settings_file(_ASSET_DIR / 소유자)["permissions"]["deny"]
        assert any("Keychain" in 항목 for 항목 in deny)
        assert any("token" in 항목 for 항목 in deny)
        assert any("secret" in 항목 for 항목 in deny)

    def test_신뢰와_운영자전권_덧씌움은_claude_디렉터리만_막는다(self) -> None:
        """~/.claude 전체 읽기 차단은 공통에 두면 소유자 턴의 세션 기록·memory
        읽기까지 막는다(덧씌움은 deny 를 빼지 못한다). 그래서 소유자 밖 수준
        덧씌움으로 옮겼다 (2026-11-09). 둘 다 그 한 줄만 든다."""
        for 이름 in (신뢰, 운영자전권):
            assert load_settings_file(_ASSET_DIR / 이름) == {
                "permissions": {"deny": ["Read(~/.claude/**)"]}
            }

    def test_claude_디렉터리는_소유자만_읽고_자격증명은_모두_막는다(self) -> None:
        공통본 = load_settings_file(_ASSET_DIR / 공통)
        공통deny = 공통본["permissions"]["deny"]
        assert "Read(~/.claude/**)" not in 공통deny
        assert "Read(~/.claude/.credentials.json)" in 공통deny
        for 이름 in (일반, 신뢰, 운영자전권):
            합본 = merge_settings(공통본, load_settings_file(_ASSET_DIR / 이름))
            assert "Read(~/.claude/**)" in 합본["permissions"]["deny"]
        소유자합본 = merge_settings(공통본, load_settings_file(_ASSET_DIR / 소유자))
        assert "Read(~/.claude/**)" not in 소유자합본["permissions"]["deny"]
        assert "Read(~/.claude/.credentials.json)" in 소유자합본["permissions"]["deny"]

    def test_훅을_두지_않는다(self) -> None:
        """진행 훅은 요청마다 엔진이 붙인다. 본보기에 훅을 두면 한 턴에 둘이
        실행된다."""
        for 이름 in (공통, 일반, 소유자, 신뢰, 운영자전권):
            assert "hooks" not in load_settings_file(_ASSET_DIR / 이름)

    def test_조직_고유값이_없다(self) -> None:
        """설치물이라 사람 이름이 든 홈 경로가 박히면 안 된다."""
        for 이름 in (공통, 일반, 소유자, 신뢰, 운영자전권):
            assert "/Users/" not in (_ASSET_DIR / 이름).read_text(encoding="utf-8")


class Test권한경로표기:
    def test_홈_아래는_물결로_적는다(self) -> None:
        표기 = install_settings.permission_path(Path("/home/u/.shinji"), Path("/home/u"))
        assert 표기 == "~/.shinji"

    def test_홈_밖은_두겹_빗금_절대경로다(self) -> None:
        표기 = install_settings.permission_path(Path("/srv/shinji"), Path("/home/u"))
        assert 표기 == "//srv/shinji"


class Test설치:
    def test_상태_디렉터리에_본보기_다섯_개를_만든다(self, tmp_path: Path) -> None:
        만든것, 건너뛴것 = install_settings.install(tmp_path / ".shinji", home=tmp_path)
        assert [p.name for p in 만든것] == [공통, 일반, 신뢰, 소유자, 운영자전권]
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

    def test_소유자_요청에는_도구_차단이_없어_도구가_열린다(self, tmp_path: Path) -> None:
        상태 = tmp_path / ".shinji"
        install_settings.install(상태, home=tmp_path)
        # owner 덧씌움은 이제 실제 파일이다 — 자격증명류만 더 막고 Bash 는 안 막는다.
        덧씌움 = load_settings_file(상태 / "engine" / 소유자)
        합친것 = merge_settings(load_settings_file(상태 / "engine" / 공통), 덧씌움)
        assert "Bash" not in 합친것["permissions"]["deny"]
        assert any("token" in 항목 for 항목 in 합친것["permissions"]["deny"])

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
