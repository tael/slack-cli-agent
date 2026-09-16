"""채널 설정 쓰기 계약.

원본은 관리 명령으로 채널 설정을 바꾼다(말수 조정, 응답 방식 전환, 채널 등록과
해제). 그 명령을 옮기려면 쓰기가 필요하다.

원본의 save_channels 는 통짜 덮어쓰기라 쓰는 도중 읽으면 잘린 JSON 이 읽힌다.
여기서는 임시 파일에 쓰고 교체한다.
"""

from __future__ import annotations

import json

import pytest

from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.errors import ConfigError


@pytest.fixture
def 설정파일(tmp_path):
    경로 = tmp_path / "channels.json"
    경로.write_text(
        json.dumps({"C1": {"mode": "default", "chat": "normal"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 경로


class Test채널설정쓰기:
    def test_일부항목만바꾸고나머지는유지한다(self, 설정파일) -> None:
        registry = ChannelRegistry(설정파일)
        registry.update("C1", {"chat": "quiet"})

        저장된 = json.loads(설정파일.read_text(encoding="utf-8"))
        assert 저장된["C1"]["chat"] == "quiet"
        assert 저장된["C1"]["mode"] == "default"

    def test_쓴값이곧바로읽힌다(self, 설정파일) -> None:
        """mtime 해상도가 1초인 파일 시스템에서도 쓴 값이 바로 보여야 한다.

        연달아 두 번 바꾸면 두 번째가 같은 초에 일어나 mtime 이 안 바뀔 수 있다.
        그 경우 캐시가 유지되면 첫 번째 값이 읽힌다.
        """
        registry = ChannelRegistry(설정파일)
        registry.update("C1", {"chat": "quiet"})
        registry.update("C1", {"chat": "active"})

        설정 = registry.get("C1")
        assert 설정 is not None
        assert 설정.chat == "active"

    def test_없는채널을바꾸면새로만든다(self, 설정파일) -> None:
        registry = ChannelRegistry(설정파일)
        registry.update("C2", {"mode": "review"})

        설정 = registry.get("C2")
        assert 설정 is not None
        assert 설정.mode == "review"

    def test_알수없는키는보존된다(self, tmp_path) -> None:
        """플러그인이 쓰는 키를 코어의 쓰기가 지우면 안 된다."""
        경로 = tmp_path / "c.json"
        경로.write_text(
            json.dumps({"C1": {"mode": "default", "org_admins": ["U1"]}}),
            encoding="utf-8",
        )
        registry = ChannelRegistry(경로)
        registry.update("C1", {"chat": "quiet"})

        저장된 = json.loads(경로.read_text(encoding="utf-8"))
        assert 저장된["C1"]["org_admins"] == ["U1"]

    def test_채널을해제한다(self, 설정파일) -> None:
        registry = ChannelRegistry(설정파일)
        assert registry.remove("C1") is True
        assert registry.get("C1") is None
        assert json.loads(설정파일.read_text(encoding="utf-8")) == {}

    def test_없는채널해제는거짓을돌려준다(self, 설정파일) -> None:
        registry = ChannelRegistry(설정파일)
        assert registry.remove("C_NONE") is False

    def test_파일이없으면만든다(self, tmp_path) -> None:
        경로 = tmp_path / "새디렉터리" / "c.json"
        registry = ChannelRegistry(경로)
        registry.update("C1", {"mode": "default"})
        assert 경로.exists()

    def test_쓰기는원자적이다(self, 설정파일) -> None:
        """쓰는 도중 읽어도 잘린 JSON 이 안 나온다.

        임시 파일에 쓰고 os.replace 로 교체하면 읽는 쪽은 옛 파일이나 새 파일
        하나를 본다. 원본의 write_text 는 이 보장이 없다.
        """
        registry = ChannelRegistry(설정파일)
        원본크기 = 설정파일.stat().st_size
        큰값 = {"persona": "x" * 100_000}
        registry.update("C1", 큰값)

        읽은것 = json.loads(설정파일.read_text(encoding="utf-8"))
        assert len(읽은것["C1"]["persona"]) == 100_000
        assert 설정파일.stat().st_size > 원본크기
        # 교체 방식이면 임시 파일이 남지 않는다
        assert list(설정파일.parent.glob("*.tmp*")) == []


class Test채널이름:
    """원본은 슬랙에서 조회한 채널 이름을 설정에 저장한다. 목록 표시에 쓴다."""

    def test_이름을읽는다(self, tmp_path) -> None:
        경로 = tmp_path / "c.json"
        경로.write_text(json.dumps({"C1": {"name": "팀-채널"}}), encoding="utf-8")
        설정 = ChannelRegistry(경로).get("C1")
        assert 설정 is not None
        assert 설정.name == "팀-채널"

    def test_이름이없으면채널ID를쓴다(self, tmp_path) -> None:
        """원본도 조회 실패 시 채널 ID 를 이름 자리에 넣는다."""
        경로 = tmp_path / "c.json"
        경로.write_text(json.dumps({"C1": {}}), encoding="utf-8")
        설정 = ChannelRegistry(경로).get("C1")
        assert 설정 is not None
        assert 설정.name == "C1"


class Test못읽는파일을덮지않는다:
    """읽기 경로는 마지막 성공분을 유지하도록 이미 방어돼 있는데 쓰기 경로만
    빠져 있었다. 못 읽은 것을 빈 설정으로 보고 그 위에 한 채널을 얹어 파일을
    통째로 대체하면 다른 채널 설정이 전부 사라진다 (sca-zvk).
    """

    def test_JSON_오타가_있으면_다른_채널을_안_지운다(self, tmp_path) -> None:
        경로 = tmp_path / "channels.json"
        깨진것 = '{"C1": {"chat": "normal"}, "C2": {"chat": "quiet"},}'
        경로.write_text(깨진것, encoding="utf-8")
        registry = ChannelRegistry(경로)

        with pytest.raises(ConfigError, match="채널 설정"):
            registry.update("C3", {"chat": "quiet"})

        assert 경로.read_text(encoding="utf-8") == 깨진것

    def test_해제도_못_읽는_파일에는_손대지_않는다(self, tmp_path) -> None:
        경로 = tmp_path / "channels.json"
        깨진것 = '{"C1": {"chat": "normal"},}'
        경로.write_text(깨진것, encoding="utf-8")
        registry = ChannelRegistry(경로)

        with pytest.raises(ConfigError):
            registry.remove("C1")

        assert 경로.read_text(encoding="utf-8") == 깨진것

    def test_파일이_아예_없으면_새로_만든다(self, tmp_path) -> None:
        """없는 것과 못 읽는 것은 다르다. 첫 등록을 막으면 안 된다."""
        registry = ChannelRegistry(tmp_path / "channels.json")

        registry.update("C1", {"chat": "quiet"})

        assert json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))["C1"]

    def test_최상위가_사전이_아니면_거부한다(self, tmp_path) -> None:
        경로 = tmp_path / "channels.json"
        경로.write_text("[]", encoding="utf-8")
        registry = ChannelRegistry(경로)

        with pytest.raises(ConfigError):
            registry.update("C1", {"chat": "quiet"})
