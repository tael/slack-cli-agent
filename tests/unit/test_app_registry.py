"""앱 레지스트리와 일일 감사(sca-4eo).

매니페스트 대조 도구는 있었지만 사람이 손으로 돌려야 했다. 값을 남기는 도구에는
그 값을 읽는 계기가 필요하다. 전체 봇을 자동 순회하려면 어느 워크스페이스의 어느
app ID 가 어느 정본에 대응하는지가 어딘가에 있어야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.slack.app_registry import (
    AppEntry,
    AppRegistry,
    AuditFinding,
    audit,
    read_bot_token,
)


class Test레지스트리:
    def test_등록한_항목을_이름으로_되찾는다(self, tmp_path: Path) -> None:
        registry = AppRegistry(tmp_path / "registry.json")
        registry.register(AppEntry("shinji", "example", "A01", tmp_path / "shinji.json"))
        항목 = registry.get("shinji")
        assert 항목 is not None
        assert 항목.app_id == "A01"
        assert 항목.workspace == "example"

    def test_없는_파일이면_빈_목록이다(self, tmp_path: Path) -> None:
        """레지스트리가 없는 것은 오류가 아니다. 아직 봇을 안 만든 설치본이다."""
        assert AppRegistry(tmp_path / "없다.json").entries() == []

    def test_같은_이름을_다시_등록하면_덮어쓴다(self, tmp_path: Path) -> None:
        """앱을 다시 만들면 app ID 가 바뀐다. 중복 항목이 남으면 감사가 없는
        앱을 조회해 매번 실패한다."""
        registry = AppRegistry(tmp_path / "registry.json")
        registry.register(AppEntry("shinji", "example", "A01", tmp_path / "s.json"))
        registry.register(AppEntry("shinji", "example", "A02", tmp_path / "s.json"))
        항목 = registry.get("shinji")
        assert 항목 is not None
        assert 항목.app_id == "A02"
        assert len(registry.entries()) == 1

    def test_이름순으로_낸다(self, tmp_path: Path) -> None:
        """감사 보고의 줄 순서가 실행마다 달라지면 차이를 눈으로 못 읽는다."""
        # 파일을 직접 쓴다. register 가 정렬해 저장하므로 그것을 거치면
        # entries() 의 정렬이 아니라 저장 쪽 정렬을 재게 된다.
        path = tmp_path / "registry.json"
        path.write_text(json.dumps({
            name: {"workspace": "example", "app_id": "A1", "manifest": f"{name}.json"}
            for name in ("shinji", "rei", "asuka")
        }, ensure_ascii=False), encoding="utf-8")
        assert [항목.name for 항목 in AppRegistry(path).entries()] == ["asuka", "rei", "shinji"]

    def test_app_id_가_담기므로_소유자만_읽게_한다(self, tmp_path: Path) -> None:
        """조직 고유값이다. 토큰과 같은 디렉터리에 같은 권한으로 둔다."""
        path = tmp_path / "registry.json"
        AppRegistry(path).register(AppEntry("shinji", "example", "A01", tmp_path / "s.json"))
        assert path.stat().st_mode & 0o777 == 0o600

    def test_망가진_파일은_빈_목록으로_읽는다(self, tmp_path: Path) -> None:
        """손으로 고치다 깨진 파일 하나가 감사 전체를 멎게 하면 안 된다."""
        path = tmp_path / "registry.json"
        path.write_text("{ 깨짐", encoding="utf-8")
        assert AppRegistry(path).entries() == []

    def test_저장_형식이_사람이_읽을_수_있는_json이다(self, tmp_path: Path) -> None:
        path = tmp_path / "registry.json"
        AppRegistry(path).register(AppEntry("신지", "example", "A01", tmp_path / "s.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["신지"]["app_id"] == "A01"
        assert "신지" in path.read_text(encoding="utf-8")


class Test감사:
    def _항목(self, name: str) -> AppEntry:
        return AppEntry(name, "example", f"A-{name}", Path(f"/tmp/{name}.json"))

    def test_차이가_없으면_보고할_것이_없다(self) -> None:
        결과 = audit([self._항목("shinji")], compare=lambda entry: [])
        assert 결과 == []

    def test_차이를_봇_이름과_함께_낸다(self) -> None:
        결과 = audit([self._항목("shinji")], compare=lambda entry: ["features.app_home 없음"])
        assert 결과 == [
            AuditFinding(name="shinji", differences=("features.app_home 없음",), error=""),
        ]

    def test_한_봇이_실패해도_나머지를_계속_본다(self) -> None:
        """워크스페이스 하나의 토큰이 만료된 것이 다른 봇의 감사를 막으면,
        그 뒤 봇들은 조회 실패와 이상 없음이 구분되지 않는다."""
        def compare(entry: AppEntry) -> list[str]:
            if entry.name == "rei":
                raise RuntimeError("토큰 만료")
            return []

        결과 = audit([self._항목("asuka"), self._항목("rei"), self._항목("shinji")], compare=compare)
        assert [(항목.name, 항목.error) for 항목 in 결과] == [("rei", "토큰 만료")]

    def test_실패는_차이_없음과_구분된다(self) -> None:
        결과 = audit([self._항목("rei")], compare=_던진다)
        assert 결과[0].differences == ()
        assert 결과[0].error


def _던진다(entry: AppEntry) -> list[str]:
    raise RuntimeError("조회 실패")


class Test봇토큰읽기:
    """app ID 를 잃어버린 봇은 자기 봇 토큰으로 되찾을 수 있다. 토큰 자리는
    봇마다 다르다 - 옛 형태(.slack_bot_token)와 credentials.json 둘 다 있다."""

    def test_옛_형태_파일에서_읽는다(self, tmp_path: Path) -> None:
        (tmp_path / ".slack_bot_token").write_text("xoxb-옛것\n", encoding="utf-8")
        assert read_bot_token(tmp_path) == "xoxb-옛것"

    def test_credentials_json_에서_읽는다(self, tmp_path: Path) -> None:
        (tmp_path / "credentials.json").write_text(
            json.dumps({"bot_token": "xoxb-새것"}), encoding="utf-8")
        assert read_bot_token(tmp_path) == "xoxb-새것"

    def test_둘_다_있으면_옛_형태를_쓴다(self, tmp_path: Path) -> None:
        """run.sh 가 그 순서로 환경변수를 넘긴다. 여기서 다른 것을 고르면
        실제로 도는 봇과 다른 앱을 조회하게 된다."""
        (tmp_path / ".slack_bot_token").write_text("xoxb-옛것", encoding="utf-8")
        (tmp_path / "credentials.json").write_text(
            json.dumps({"bot_token": "xoxb-새것"}), encoding="utf-8")
        assert read_bot_token(tmp_path) == "xoxb-옛것"

    def test_아무_데도_없으면_빈_문자열이다(self, tmp_path: Path) -> None:
        assert read_bot_token(tmp_path) == ""
