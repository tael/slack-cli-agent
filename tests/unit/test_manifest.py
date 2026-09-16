"""슬랙 앱 매니페스트 계약.

이 시험이 있는 이유는 sca-1v7 이다. 세 봇의 DM 이 꺼져 있었는데 코드는
정상이었고 단위 시험도 전부 통과했다. 결함이 슬랙 앱 설정에 있어서 코드를
보는 시험으로는 닿지 않는 자리였다.

매니페스트 정본을 저장소에 두고 그 정본에 계약을 걸면, 적어도 "우리가
의도한 설정" 쪽은 시험이 지킨다. 실제 앱과 정본이 어긋났는지는 네트워크가
필요해 `slack-app.py diff` 가 따로 본다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.slack.manifest import AppManifest, ManifestContract, ManifestDiff

REPO = Path(__file__).resolve().parents[2]
SLACK_APPS = REPO / "slack-apps"


def _정본_경로들() -> list[Path]:
    """밑줄로 시작하는 것은 봇이 아니라 템플릿이다."""
    return sorted(p for p in SLACK_APPS.glob("*.json") if not p.name.startswith("_"))


def _온전한_매니페스트() -> dict[str, Any]:
    return {
        "display_information": {"name": "예시봇"},
        "features": {
            "bot_user": {"display_name": "example", "always_online": True},
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
        },
        "oauth_config": {"scopes": {"bot": ["im:history", "im:read", "im:write", "chat:write"]}},
        "settings": {"event_subscriptions": {"bot_events": ["app_mention", "message.im"]}},
    }


class TestManifestContract:
    def test_온전한_매니페스트는_위반이_없다(self) -> None:
        assert ManifestContract().violations(AppManifest(_온전한_매니페스트())) == []

    def test_app_home_이_통째로_없으면_잡는다(self) -> None:
        """sca-1v7 에서 실제로 난 형태다. 슬랙은 이 항목이 없으면 Messages
        탭을 끈 것으로 보고, 그러면 소유자가 DM 입력창을 못 연다."""
        data = _온전한_매니페스트()
        del data["features"]["app_home"]
        위반 = ManifestContract().violations(AppManifest(data))
        assert any("messages_tab_enabled" in v for v in 위반)

    def test_메시지탭이_꺼져_있으면_잡는다(self) -> None:
        data = _온전한_매니페스트()
        data["features"]["app_home"]["messages_tab_enabled"] = False
        assert any("messages_tab_enabled" in v for v in ManifestContract().violations(AppManifest(data)))

    def test_메시지탭이_읽기전용이면_잡는다(self) -> None:
        """읽기 전용이면 탭은 열리는데 보낼 수가 없다. 꺼진 것과 결과가 같다."""
        data = _온전한_매니페스트()
        data["features"]["app_home"]["messages_tab_read_only_enabled"] = True
        assert any("read_only" in v for v in ManifestContract().violations(AppManifest(data)))

    def test_message_im_이벤트가_없으면_잡는다(self) -> None:
        data = _온전한_매니페스트()
        data["settings"]["event_subscriptions"]["bot_events"] = ["app_mention"]
        assert any("message.im" in v for v in ManifestContract().violations(AppManifest(data)))

    @pytest.mark.parametrize("scope", ["im:history", "im:read", "im:write", "chat:write"])
    def test_DM_왕복에_필요한_스코프가_없으면_잡는다(self, scope: str) -> None:
        """chat:write 까지 본다. 받기만 하고 답을 못 보내면 DM 이 꺼진 것과
        사용자가 보는 결과가 같은데, 세 정본과 템플릿에서 함께 빠지면
        정책 지문도 계약도 통과한다 (코덱스 리뷰 [높음])."""
        data = _온전한_매니페스트()
        data["oauth_config"]["scopes"]["bot"].remove(scope)
        assert any(scope in v for v in ManifestContract().violations(AppManifest(data)))

    def test_username_이_ASCII_가_아니면_잡는다(self) -> None:
        """apps.manifest.export 는 한글 username 을 그대로 내주는데
        apps.manifest.update 는 bad_username 으로 거부한다. export 한 것을
        되돌려 보내는 흐름이 여기서 막힌다 (sca-1v7 에서 실제로 났다)."""
        data = _온전한_매니페스트()
        data["features"]["bot_user"]["display_name"] = "아스카"
        assert any("display_name" in v for v in ManifestContract().violations(AppManifest(data)))

    def test_위반을_한꺼번에_전부_낸다(self) -> None:
        """하나 고치고 다시 돌리기를 반복하지 않도록 모아서 낸다."""
        위반 = ManifestContract().violations(AppManifest({}))
        assert len(위반) >= 5


class TestAppManifest:
    def test_정책_지문이_같은_정책이면_같다(self) -> None:
        한쪽 = AppManifest(_온전한_매니페스트())
        다른쪽_자료 = _온전한_매니페스트()
        다른쪽_자료["display_information"]["name"] = "다른봇"
        다른쪽_자료["features"]["bot_user"]["display_name"] = "other"
        assert 한쪽.policy_fingerprint() == AppManifest(다른쪽_자료).policy_fingerprint()

    def test_정책_지문이_스코프_차이를_잡는다(self) -> None:
        자료 = _온전한_매니페스트()
        자료["oauth_config"]["scopes"]["bot"].append("files:read")
        assert AppManifest(_온전한_매니페스트()).policy_fingerprint() != AppManifest(자료).policy_fingerprint()

    def test_정책_지문이_순서에_흔들리지_않는다(self) -> None:
        """슬랙이 내주는 순서와 우리가 적은 순서가 달라도 같은 정책이다."""
        자료 = _온전한_매니페스트()
        자료["oauth_config"]["scopes"]["bot"].reverse()
        자료["settings"]["event_subscriptions"]["bot_events"].reverse()
        assert AppManifest(_온전한_매니페스트()).policy_fingerprint() == AppManifest(자료).policy_fingerprint()

    def test_정책_지문이_app_home_차이를_잡는다(self) -> None:
        """이번 결함이 걸린 바로 그 필드다. 지문이 이것을 안 보면 한 봇만
        DM 이 꺼져 있어도 "정책이 같다" 가 통과한다 (뮤테이션으로 확인).
        """
        자료 = _온전한_매니페스트()
        자료["features"]["app_home"]["messages_tab_enabled"] = False
        assert AppManifest(_온전한_매니페스트()).policy_fingerprint() != AppManifest(자료).policy_fingerprint()

    def test_정책_지문이_app_home_누락을_잡는다(self) -> None:
        자료 = _온전한_매니페스트()
        del 자료["features"]["app_home"]
        assert AppManifest(_온전한_매니페스트()).policy_fingerprint() != AppManifest(자료).policy_fingerprint()

    def test_정책_지문이_이벤트_차이를_잡는다(self) -> None:
        자료 = _온전한_매니페스트()
        자료["settings"]["event_subscriptions"]["bot_events"].append("reaction_added")
        assert AppManifest(_온전한_매니페스트()).policy_fingerprint() != AppManifest(자료).policy_fingerprint()


class Test저장소정본:
    def test_정책_원천인_템플릿이_있다(self) -> None:
        """정본은 조직 고유값이라 추적하지 않는다(docs/패키징-경계.md). 그래서
        갓 clone 한 자리에는 정본이 없고, 그때도 정책을 들고 있는 것은
        템플릿이다. 이것까지 없으면 새 봇이 받을 설정의 원천이 사라진다."""
        assert (SLACK_APPS / "_template.json").is_file()

    def test_정본이_전부_계약을_지킨다(self) -> None:
        나쁨 = {
            p.name: ManifestContract().violations(AppManifest.from_path(p))
            for p in [*_정본_경로들(), SLACK_APPS / "_template.json"]
        }
        assert {k: v for k, v in 나쁨.items() if v} == {}

    def test_정본들의_정책_필드가_전부_같다(self) -> None:
        """봇마다 다른 것은 이름과 설명뿐이어야 한다. 실제로 rei 만
        assistant_thread_started 가 있었고 나머지 둘은 없었다 (sca-4zy).
        템플릿도 함께 본다 - 새 봇이 다른 정책으로 태어나면 같은 일이 난다."""
        지문 = {
            p.name: AppManifest.from_path(p).policy_fingerprint()
            for p in [*_정본_경로들(), SLACK_APPS / "_template.json"]
        }
        assert len(set(지문.values())) == 1, 지문

    def test_프로필마다_정본이_있다(self) -> None:
        """새 봇 프로필을 넣고 매니페스트 정본을 안 만들면 여기서 걸린다."""
        프로필 = {
            p.stem for p in (REPO / "profiles").glob("*.json") if not p.name.endswith(".example.json")
        }
        정본 = {p.stem for p in _정본_경로들()}
        assert 프로필 <= 정본, f"정본이 없는 프로필: {프로필 - 정본}"

    def test_정본_파일이_읽히는_JSON_이다(self) -> None:
        for p in [*_정본_경로들(), SLACK_APPS / "_template.json"]:
            json.loads(p.read_text(encoding="utf-8"))


class Test템플릿렌더링:
    """새 봇의 매니페스트를 만드는 자리.

    이 로직이 셸 안에 있으면 시험이 닿지 않아, 템플릿을 안 쓰고 매니페스트를
    다시 손으로 쓰는 변경이 조용히 들어간다. 실제로 그래서 봇마다 설정이
    갈렸다 (sca-4zy).
    """

    def test_이름_두_자리를_각각_채운다(self, tmp_path: Path) -> None:
        template = tmp_path / "t.json"
        template.write_text(
            json.dumps({
                "display_information": {"name": "__DISPLAY__", "description": "__DISPLAY__ 에이전트"},
                "features": {"bot_user": {"display_name": "__NAME__"}},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        m = AppManifest.from_template(template, name="rei", display="아야나미 레이")
        assert m.raw["display_information"]["name"] == "아야나미 레이"
        assert m.raw["display_information"]["description"] == "아야나미 레이 에이전트"
        # 사람이 보는 이름과 슬랙 내부 username 은 다른 필드다. 한쪽 값을
        # 양쪽에 넣으면 apps.manifest.update 가 bad_username 으로 거부한다.
        assert m.bot_username == "rei"

    def test_채우지_못한_자리를_남기지_않는다(self, tmp_path: Path) -> None:
        """치환을 빠뜨리면 __NAME__ 이 그대로 슬랙에 올라간다."""
        template = tmp_path / "t.json"
        template.write_text(json.dumps({"features": {"bot_user": {"display_name": "__MISSING__"}}}), encoding="utf-8")
        with pytest.raises(ValueError, match="__MISSING__"):
            AppManifest.from_template(template, name="rei", display="레이")

    def test_저장소_템플릿으로_만든_것이_계약을_지킨다(self) -> None:
        """템플릿 파일만 보는 것으로는 부족하다 - 치환이 계약 대상 필드를
        망가뜨릴 수도 있다."""
        m = AppManifest.from_template(SLACK_APPS / "_template.json", name="newbot", display="새봇")
        assert ManifestContract().violations(m) == []

    def test_저장소_템플릿으로_만든_것이_기존_봇과_같은_정책이다(self) -> None:
        m = AppManifest.from_template(SLACK_APPS / "_template.json", name="newbot", display="새봇")
        기존 = AppManifest.from_path(_정본_경로들()[0])
        assert m.policy_fingerprint() == 기존.policy_fingerprint()


class Test생성스크립트연결:
    """만든 것과 연결한 것은 다르다. 렌더링 함수가 옳아도 new-bot.sh 가
    그것을 안 부르면 새 봇은 여전히 제각각으로 태어난다."""

    def test_new_bot_이_템플릿_렌더링을_부른다(self) -> None:
        본문 = (REPO / "tools" / "new-bot.sh").read_text(encoding="utf-8")
        assert "from_template" in 본문
        assert "slack-apps/_template.json" in 본문


class TestManifestDiff:
    """저장소 정본과 슬랙에 실제로 올라간 설정을 대조한다.

    정본을 고치고 반영을 빠뜨리면 시험은 전부 통과하는데 봇은 옛 설정으로
    돈다. 계약 시험이 보는 것은 "우리가 의도한 설정" 까지다.
    """

    def test_같으면_차이가_없다(self) -> None:
        assert ManifestDiff().differences(
            AppManifest(_온전한_매니페스트()), AppManifest(_온전한_매니페스트())
        ) == []

    def test_원격에만_있는_키는_차이가_아니다(self) -> None:
        """슬랙은 우리가 안 적은 필드를 채워서 내준다. 그것을 차이로 세면
        매번 차이가 나와 이 명령을 아무도 안 보게 된다."""
        원격 = _온전한_매니페스트()
        원격["settings"]["org_deploy_enabled"] = False
        원격["display_information"]["long_description"] = "슬랙이 붙인 것"
        assert ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격)) == []

    def test_값이_다르면_경로와_함께_잡는다(self) -> None:
        원격 = _온전한_매니페스트()
        원격["features"]["app_home"]["messages_tab_enabled"] = False
        차이 = ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격))
        assert len(차이) == 1
        assert "features.app_home.messages_tab_enabled" in 차이[0]

    def test_정본에_있는_키가_원격에_없으면_잡는다(self) -> None:
        """sca-1v7 이 실제로 이 형태다. app_home 이 통째로 없었다."""
        원격 = _온전한_매니페스트()
        del 원격["features"]["app_home"]
        차이 = ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격))
        assert any("features.app_home" in d and "없다" in d for d in 차이)

    def test_문자열_목록의_순서는_차이가_아니다(self) -> None:
        원격 = _온전한_매니페스트()
        원격["oauth_config"]["scopes"]["bot"].reverse()
        assert ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격)) == []

    def test_문자열_목록의_원소_차이는_잡는다(self) -> None:
        원격 = _온전한_매니페스트()
        원격["settings"]["event_subscriptions"]["bot_events"].append("assistant_thread_started")
        차이 = ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격))
        assert any("bot_events" in d for d in 차이)

    def test_차이를_한꺼번에_전부_낸다(self) -> None:
        원격 = _온전한_매니페스트()
        del 원격["features"]["app_home"]
        원격["features"]["bot_user"]["display_name"] = "다름"
        원격["oauth_config"]["scopes"]["bot"] = []
        assert len(ManifestDiff().differences(AppManifest(_온전한_매니페스트()), AppManifest(원격))) >= 3
