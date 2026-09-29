"""권한 계층 — Principal, AccessPolicy, ToolPolicy."""

from __future__ import annotations

import json
from pathlib import Path

from slack_cli_agent.auth.policy import OWNER_EFFORT_MIN, AccessExtension, AccessPolicy
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.auth.tools import READ_ONLY_TOOLS, SKILL_TOOL, ToolPolicy
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

    def test_읽기전용은_기본_도구에_있어도_쓰기_도구를_뺀다(self, tmp_path: Path) -> None:
        """'읽기 전용' 이 빼는 것은 소유자 추가분과 확장분뿐이었다. 프로필이
        settings.base_tools 에 Bash 를 넣으면 조회만 해야 하는 턴에도 그대로
        들어갔다(sca-gy0)."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(("Read", "Bash", "Grep", "Write", "Edit"), self.OWNER_EXTRA)
        assert tools.tools_for(principal, readonly=True) == "Read,Grep"

    def test_읽기전용이_아니면_기본_도구를_그대로_둔다(self, tmp_path: Path) -> None:
        """거르는 자리는 읽기 전용 턴뿐이다. 일반 턴의 도구를 줄이면 답을 못 한다."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(("Read", "Bash"), self.OWNER_EXTRA)
        assert tools.tools_for(principal) == "Read,Bash"

    def test_모르는_도구는_읽기_전용으로_치지_않는다(self, tmp_path: Path) -> None:
        """MCP 도구처럼 이름만 보고는 판단할 수 없는 것이 들어온다. 허용목록에
        없으면 뺀다 — 목록을 부정형으로 두면 새 쓰기 도구가 조용히 통과한다."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(("Read", "mcp__github__create_pull_request"), self.OWNER_EXTRA)
        assert tools.tools_for(principal, readonly=True) == "Read"

    def test_읽기전용_턴에는_스킬을_안_붙인다(self, tmp_path: Path) -> None:
        """스킬은 무엇이든 실행할 수 있어 읽기 전용 보장을 깬다."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(self.BASE, self.OWNER_EXTRA)
        붙은것 = tools.tool_list_for(principal, readonly=True, skills_enabled=True)
        assert SKILL_TOOL not in 붙은것

    def test_읽기전용에서_전부_걸러지면_빈_목록이_아니다(self, tmp_path: Path) -> None:
        """빈 목록은 금지가 아니라 금지의 부재다. 읽기 전용에서 전부 걸러져
        빈 목록이 되면 아무것도 안 닫힌 턴이 된다(sca-6ewc)."""
        policy = AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", {}))
        principal = policy.principal_for("D1", OWNER)
        tools = ToolPolicy(("Bash", "Write"), self.OWNER_EXTRA)
        assert tools.tool_list_for(principal, readonly=True) == READ_ONLY_TOOLS

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


class Test채널별사용자도구:
    """채널 설정에 적힌 사용자에게만 도구를 더 붙인다.

    원본 bot.py 의 airflow_admins / is_airflow_admin(110) 을 일반화한 것이다.
    airflow_ctl.sh 는 그 표의 한 값이 된다 (sca-ww4). 소유자는 이미 소유자
    도구가 붙으므로 따지지 않는다 - 원본과 같다.
    """

    def _policy(self, tmp_path: Path) -> AccessPolicy:
        channels = {"C1": {"user_tools": {STRANGER: ["Bash(airflow_ctl.sh:*)"]}}}
        return AccessPolicy(make_profile(), make_channels(tmp_path / "c.json", channels))

    def test_명단에_있으면_그_도구가_붙는다(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(("Read",), channel_tools=policy.channel_tools_for)

        assert tools.tools_for(principal) == "Read,Bash(airflow_ctl.sh:*)"

    def test_명단에_없는_사용자에게는_안_붙는다(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        principal = policy.principal_for("C1", TRUSTED_USER)
        tools = ToolPolicy(("Read",), channel_tools=policy.channel_tools_for)

        assert tools.tools_for(principal) == "Read"

    def test_다른_채널에서는_안_붙는다(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        principal = policy.principal_for("C2", STRANGER)
        tools = ToolPolicy(("Read",), channel_tools=policy.channel_tools_for)

        assert tools.tools_for(principal) == "Read"

    def test_읽기_전용_턴에는_안_붙는다(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        principal = policy.principal_for("C1", STRANGER)
        tools = ToolPolicy(("Read",), channel_tools=policy.channel_tools_for)

        assert tools.tools_for(principal, readonly=True) == "Read"

    def test_소유자에게는_따지지_않는다(self, tmp_path: Path) -> None:
        policy = self._policy(tmp_path)
        principal = policy.principal_for("C1", OWNER)
        tools = ToolPolicy(("Read",), ("Bash",), channel_tools=policy.channel_tools_for)

        assert tools.tools_for(principal) == "Read,Bash"
