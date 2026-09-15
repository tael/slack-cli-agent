"""웹 콘솔의 프로필·채널 편집 계층."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.web.editor import ChannelEditor, ProfileEditor

MINIMAL = {
    "name": "example",
    "primary_engine": {"type": "claude", "binary": "claude", "model": "m"},
    "owner_user_id": "U1",
    "troubleshoot_channel": "C1",
}


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestProfileEditorNames:
    def test_검색_경로에서_찾은_프로필_이름을_정렬해서_돌려준다(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        write(d1 / "zeta.json", {**MINIMAL, "name": "zeta"})
        write(d2 / "alpha.json", {**MINIMAL, "name": "alpha"})

        editor = ProfileEditor([d1, d2])

        assert editor.names() == ["alpha", "zeta"]

    def test_같은_이름이_여러_검색경로에_있으면_한번만_낸다(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        write(d1 / "example.json", MINIMAL)
        write(d2 / "example.json", MINIMAL)

        editor = ProfileEditor([d1, d2])

        assert editor.names() == ["example"]

    def test_검색_경로가_없으면_빈_목록(self, tmp_path: Path) -> None:
        editor = ProfileEditor([tmp_path / "없음"])

        assert editor.names() == []


    def test_예시_프로필은_목록에서_뺀다(self, tmp_path: Path) -> None:
        """`*.example.json` 은 저장소에 올리는 견본이다. 실제 봇이 아니므로
        콘솔 봇 목록에 섞이면 안 된다."""
        base = tmp_path / "profiles"
        base.mkdir()
        (base / "asuka.json").write_text("{}", encoding="utf-8")
        (base / "example.example.json").write_text("{}", encoding="utf-8")

        assert ProfileEditor([base]).names() == ["asuka"]


class TestProfileEditorRead:
    def test_원본_JSON_그대로_돌려준다(self, tmp_path: Path) -> None:
        write(tmp_path / "example.json", MINIMAL)
        editor = ProfileEditor([tmp_path])

        data = editor.read("example")

        assert data == MINIMAL

    def test_없는_이름을_읽으면_예외(self, tmp_path: Path) -> None:
        editor = ProfileEditor([tmp_path])

        with pytest.raises(ConfigError):
            editor.read("없음")


class TestProfileEditorSave:
    def test_검증을_통과하면_빈_오류_목록을_돌려주고_파일을_쓴다(self, tmp_path: Path) -> None:
        write(tmp_path / "example.json", MINIMAL)
        editor = ProfileEditor([tmp_path])

        errors = editor.save("example", {**MINIMAL, "display_name": "새 이름"})

        assert errors == []
        saved = json.loads((tmp_path / "example.json").read_text(encoding="utf-8"))
        assert saved["display_name"] == "새 이름"

    def test_검증에_실패하면_오류_목록을_돌려주고_파일을_안_바꾼다(self, tmp_path: Path) -> None:
        path = tmp_path / "example.json"
        write(path, MINIMAL)
        editor = ProfileEditor([tmp_path])
        before = path.read_text(encoding="utf-8")

        broken = {**MINIMAL, "owner_user_id": "", "troubleshoot_channel": ""}
        errors = editor.save("example", broken)

        assert errors != []
        assert path.read_text(encoding="utf-8") == before

    def test_구조가_아예_틀린_데이터도_ConfigError_메시지를_오류로_담는다(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "example.json"
        write(path, MINIMAL)
        editor = ProfileEditor([tmp_path])
        before = path.read_text(encoding="utf-8")

        errors = editor.save("example", {"name": "example"})

        assert errors != []
        assert path.read_text(encoding="utf-8") == before

    def test_없는_이름은_검색경로_첫번째_디렉터리에_새로_만든다(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        editor = ProfileEditor([d1, d2])

        errors = editor.save("new", {**MINIMAL, "name": "new"})

        assert errors == []
        assert (d1 / "new.json").is_file()
        assert not (d2 / "new.json").exists()

    def test_저장은_임시파일을_거쳐_os_replace_로_교체한다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "example.json"
        write(path, MINIMAL)
        editor = ProfileEditor([tmp_path])
        calls: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def spy_replace(src: object, dst: object) -> None:
            calls.append((Path(src), Path(dst)))  # type: ignore[arg-type]
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy_replace)

        editor.save("example", {**MINIMAL, "display_name": "이름2"})

        assert len(calls) == 1
        src, dst = calls[0]
        assert src != dst
        assert src.name != path.name
        assert dst == path
        assert not (tmp_path / "example.json.tmp").exists()

    def test_os_replace_전에_실패하면_원본은_그대로다(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "example.json"
        write(path, MINIMAL)
        editor = ProfileEditor([tmp_path])
        before = path.read_text(encoding="utf-8")

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("디스크 가득 참 흉내")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError):
            editor.save("example", {**MINIMAL, "display_name": "이름2"})

        assert path.read_text(encoding="utf-8") == before


class TestChannelEditor:
    def test_list_는_channel_id_를_포함한_평평한_딕셔너리_목록(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"name": "일반", "mode": "default"}})
        registry = ChannelRegistry(path)
        editor = ChannelEditor(registry)

        result = editor.list()

        assert len(result) == 1
        assert result[0]["channel_id"] == "C1"
        assert result[0]["name"] == "일반"

    def test_update_은_기존_ChannelRegistry_update_를_그대로_쓴다(self, tmp_path: Path) -> None:
        path = tmp_path / "channels.json"
        write(path, {"C1": {"name": "일반"}})
        registry = ChannelRegistry(path)
        editor = ChannelEditor(registry)

        result = editor.update("C1", {"mode": "silent"})

        assert result["mode"] == "silent"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["C1"]["mode"] == "silent"
