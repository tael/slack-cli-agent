"""`tools/slack-app.py diff` 의 실행 경로.

ManifestDiff 단위 시험이 통과해도 이 명령이 export 결과를 그것에 넘기지
않거나 종료코드를 안 내면 아무 소용이 없다. 부품을 만든 것과 연결한 것은
다르고, 단위 시험은 그 연결을 안 본다 (코덱스 리뷰).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]


def _도구() -> Any:
    """하이픈이 든 파일명이라 일반 import 로는 못 읽는다."""
    spec = importlib.util.spec_from_file_location("slack_app_tool", REPO / "tools" / "slack-app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _정본(tmp_path: Path) -> Path:
    path = tmp_path / "bot.json"
    path.write_text(
        json.dumps({
            "display_information": {"name": "예시봇"},
            "features": {"app_home": {"messages_tab_enabled": True}},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def 도구(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _도구()
    # 설정 토큰 갱신은 네트워크와 디스크를 건드린다. 이 시험이 보는 것은
    # export 결과가 대조로 흘러가는지까지다.
    monkeypatch.setattr(module, "rotate", lambda ws: "토큰")
    return module


def _export를(도구: Any, monkeypatch: pytest.MonkeyPatch, 응답: dict[str, Any]) -> list[str]:
    부른방법: list[str] = []

    def 가짜_post(method: str, data: dict[str, str], token: str | None = None) -> dict[str, Any]:
        부른방법.append(method)
        return 응답

    monkeypatch.setattr(도구, "_post", 가짜_post)
    return 부른방법


class TestDiff명령:
    def test_같으면_종료코드_0_이다(
        self, 도구: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        정본 = _정본(tmp_path)
        원격 = json.loads(정본.read_text(encoding="utf-8"))
        # 슬랙이 덧붙이는 필드가 있어도 차이가 아니다.
        원격["settings"] = {"org_deploy_enabled": False}
        방법 = _export를(도구, monkeypatch, {"ok": True, "manifest": 원격})

        with pytest.raises(SystemExit) as exc:
            도구.main(["slack-app.py", "diff", "ws", "A123", str(정본)])

        assert exc.value.code == 0
        assert 방법 == ["apps.manifest.export"]
        assert "정본과 같다" in capsys.readouterr().out

    def test_다르면_종료코드_1_과_차이를_낸다(
        self, 도구: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """sca-1v7 이 실제로 이 형태다 - 슬랙 쪽에 app_home 이 없었다."""
        정본 = _정본(tmp_path)
        _export를(도구, monkeypatch, {
            "ok": True,
            "manifest": {"display_information": {"name": "예시봇"}, "features": {}},
        })

        with pytest.raises(SystemExit) as exc:
            도구.main(["slack-app.py", "diff", "ws", "A123", str(정본)])

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "features.app_home" in out
        assert "차이 1건" in out

    def test_export_가_실패하면_사유와_함께_멈춘다(
        self, 도구: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """실패를 '차이 없음' 으로 읽으면 이 명령은 아무것도 안 보면서
        통과만 낸다."""
        _export를(도구, monkeypatch, {"ok": False, "error": "app_not_found"})

        with pytest.raises(SystemExit) as exc:
            도구.main(["slack-app.py", "diff", "ws", "A123", str(_정본(tmp_path))])

        assert exc.value.code != 0
        assert "app_not_found" in str(exc.value)

    def test_넘긴_app_id_로_export_한다(
        self, 도구: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """인자를 뒤바꿔 넘겨도 위 시험들은 통과한다."""
        받은: dict[str, str] = {}

        def 가짜_post(method: str, data: dict[str, str], token: str | None = None) -> dict[str, Any]:
            받은.update(data)
            return {"ok": True, "manifest": json.loads(_정본(tmp_path).read_text(encoding="utf-8"))}

        monkeypatch.setattr(도구, "_post", 가짜_post)
        with pytest.raises(SystemExit):
            도구.main(["slack-app.py", "diff", "ws", "A999", str(_정본(tmp_path))])

        assert 받은["app_id"] == "A999"
