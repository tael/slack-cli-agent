"""권한 계층 — Principal, AccessPolicy, ToolPolicy."""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.auth.policy import OWNER_EFFORT_MIN, AccessExtension, AccessPolicy
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import ToolPolicy
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.config.profile import Profile

OWNER = "U_OWNER"
STRANGER = "U_STRANGER"
TRUSTED_USER = "U_TRUSTED"

MINIMAL_PROFILE = {
    "name": "example",
    "primary_engine": {
        "type": "claude",
        "binary": "claude",
        "model": "sonnet",
        "model_owner": "opus",
    },
    "owner_user_id": OWNER,
    "troubleshoot_channel": "C_TROUBLE",
}


def make_profile() -> Profile:
    return Profile.from_dict(MINIMAL_PROFILE)


def make_channels(path: Path, data: dict) -> ChannelRegistry:
    path.write_text(json.dumps(data), encoding="utf-8")
    return ChannelRegistry(path)


class TestPrincipalFor:
    def test_소유자는_DM에서_OWNER_등급이다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D123", OWNER)
        assert principal.trust is TrustLevel.OWNER
        assert principal.is_direct_message is True

    def test_소유자는_일반_채널에서도_OWNER_등급이다(self, tmp_path: Path) -> None:
        """모델·effort 상향은 채널과 무관해야 한다 — 원본의 is_owner 와 같은 축."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C_PUBLIC", OWNER)
        assert principal.trust is TrustLevel.OWNER
        assert principal.is_direct_message is False

    def test_채널의_trusted_users에_있으면_TRUSTED다(self, tmp_path: Path) -> None:
        channels = make_channels(
            tmp_path / "c.json", {"C1": {"trusted_users": [TRUSTED_USER]}}
        )
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", TRUSTED_USER)
        assert principal.trust is TrustLevel.TRUSTED

    def test_등록되지_않은_사용자는_GENERAL이다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        assert principal.trust is TrustLevel.GENERAL


class TestFullAuthorityAndMechanism:
    def test_소유자_DM은_최고권한이라_구조를_밝힌다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        assert policy.is_full_authority(principal) is True
        assert policy.may_disclose_mechanism(principal) is True

    def test_소유자라도_일반_채널에서는_최고권한이_아니다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C_PUBLIC", OWNER)
        assert policy.is_full_authority(principal) is False
        assert policy.may_disclose_mechanism(principal) is False

    def test_채널이_구조공개를_켜면_소유자가_아니어도_공개한다(self, tmp_path: Path) -> None:
        channels = make_channels(
            tmp_path / "c.json", {"C1": {"disclose_mechanism": True}}
        )
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", STRANGER)
        assert policy.may_disclose_mechanism(principal) is True
        # 구조 공개와 최고권한은 다르다. 최고권한 없이 구조만 연다
        assert policy.is_full_authority(principal) is False

    def test_토큰_사용_현황은_최고권한_자리에서만_본다(self, tmp_path: Path) -> None:
        channels = make_channels(
            tmp_path / "c.json", {"C1": {"disclose_mechanism": True}}
        )
        policy = AccessPolicy(make_profile(), channels)
        mechanism_open_principal = policy.principal_for("C1", OWNER)
        owner_dm_principal = policy.principal_for("D1", OWNER)
        # 구조공개 채널이라도 DM 이 아니면 사용 현황은 안 본다
        assert policy.may_see_usage(mechanism_open_principal) is False
        assert policy.may_see_usage(owner_dm_principal) is True


class TestModelFor:
    def test_소유자는_채널과_무관하게_소유자용_모델이다(self, tmp_path: Path) -> None:
        channels = make_channels(tmp_path / "c.json", {"C1": {"model": "haiku"}})
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", OWNER)
        assert policy.model_for(principal) == "opus"

    def test_일반_사용자는_채널이_정한_모델을_쓴다(self, tmp_path: Path) -> None:
        channels = make_channels(tmp_path / "c.json", {"C1": {"model": "haiku"}})
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", STRANGER)
        assert policy.model_for(principal) == "haiku"

    def test_채널_설정이_없으면_기본_모델이다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        assert policy.model_for(principal) == "sonnet"


class TestEffortFor:
    def test_ultrathink이_있으면_high로_올린다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        assert policy.effort_for(principal, "ultrathink 로 봐줘") == "high"

    def test_채널값이_소유자_하한보다_낮으면_하한으로_올린다(self, tmp_path: Path) -> None:
        channels = make_channels(tmp_path / "c.json", {"C1": {"effort": "low"}})
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", OWNER)
        assert policy.effort_for(principal, "그냥 물음") == OWNER_EFFORT_MIN

    def test_일반_사용자는_채널값을_그대로_쓴다(self, tmp_path: Path) -> None:
        channels = make_channels(tmp_path / "c.json", {"C1": {"effort": "low"}})
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", STRANGER)
        assert policy.effort_for(principal, "그냥 물음") == "low"

    def test_채널값이_없으면_기본은_medium이다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        assert policy.effort_for(principal, "그냥 물음") == "medium"

    def test_소유자는_채널값이_이미_하한_이상이면_그대로_쓴다(self, tmp_path: Path) -> None:
        channels = make_channels(tmp_path / "c.json", {"C1": {"effort": "xhigh"}})
        policy = AccessPolicy(make_profile(), channels)
        principal = policy.principal_for("C1", OWNER)
        assert policy.effort_for(principal, "그냥 물음") == "xhigh"


class TestAccessExtension:
    class _OrgToolExtension(AccessExtension):
        def __init__(self, admins: set[str]) -> None:
            self._admins = admins

        def applies(self, principal: Principal, prompt: str = "") -> bool:
            return principal.user_id in self._admins

        def extra_tools(self, principal: Principal):
            return ("Bash(ops_ctl.sh:*)",)

    def test_확장이_적용되면_구조공개가_열린다(self, tmp_path: Path) -> None:
        ext = self._OrgToolExtension({STRANGER})
        policy = AccessPolicy(
            make_profile(), make_channels(tmp_path / "c.json", {}), extensions=(ext,)
        )
        principal = policy.principal_for("C1", STRANGER)
        assert policy.may_disclose_mechanism(principal) is True

    def test_확장이_적용되지_않으면_기본값을_따른다(self, tmp_path: Path) -> None:
        ext = self._OrgToolExtension({"다른_사람"})
        policy = AccessPolicy(
            make_profile(), make_channels(tmp_path / "c.json", {}), extensions=(ext,)
        )
        principal = policy.principal_for("C1", STRANGER)
        assert policy.may_disclose_mechanism(principal) is False


class TestToolPolicy:
    BASE = ("Read", "Grep")
    OWNER_EXTRA = ("Bash", "Edit")

    def test_읽기전용이면_기본_도구만_붙는다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(self.BASE, self.OWNER_EXTRA)
        assert tools.tools_for(principal, readonly=True) == "Read,Grep"

    def test_소유자는_소유자용_도구가_더해진다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(self.BASE, self.OWNER_EXTRA)
        assert tools.tools_for(principal) == "Read,Grep,Bash,Edit"

    def test_일반_사용자는_기본_도구만_붙는다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(self.BASE, self.OWNER_EXTRA)
        assert tools.tools_for(principal) == "Read,Grep"

    def test_확장이_적용되면_확장_도구가_더해진다(self, tmp_path: Path) -> None:
        class _Ext(AccessExtension):
            def applies(self, principal: Principal, prompt: str = "") -> bool:
                return principal.user_id == STRANGER

            def extra_tools(self, principal: Principal):
                return ("Bash(ops_ctl.sh:*)",)

        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(self.BASE, self.OWNER_EXTRA, extensions=(_Ext(),))
        assert tools.tools_for(principal) == "Read,Grep,Bash(ops_ctl.sh:*)"

    def test_aside가_아니고_스킬이_켜져있으면_Skill이_더해진다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(self.BASE)
        assert tools.tools_for(principal, skills_enabled=True) == "Read,Grep,Skill"

    def test_aside면_스킬이_켜져있어도_안_붙는다(self, tmp_path: Path) -> None:
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(self.BASE)
        assert (
            tools.tools_for(principal, aside=True, skills_enabled=True) == "Read,Grep"
        )
