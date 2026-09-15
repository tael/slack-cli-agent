"""엔진 프로세스에 넘길 환경 변수 선별 시험.

원본 bot.py:1524 codex_environment() 와 bot.py:1678-1681 _run_claude() 의
환경 변수 처리를 이관한 것을 검증한다. 자격증명 유출 방지가 목적이므로
제외 대상이 실제로 빠지는 것을 확인하는 데 집중한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.core.errors import ConfigError
from slack_cli_agent.engine.environment import (
    ClaudeEnvironmentPolicy,
    CodexEnvironmentPolicy,
    EngineEnvironmentPolicy,
    EngineEnvironmentPolicyRegistry,
    GeminiEnvironmentPolicy,
    create_environment_policy,
    registry,
)


def _rich_source_env() -> dict[str, str]:
    """슬랙 토큰·다른 엔진 자격증명이 섞인 실행 환경을 흉내낸다."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "HOME": "/Users/someone",
        "SLACK_BOT_TOKEN": "xoxb-secret",
        "SLACK_APP_TOKEN": "xapp-secret",
        "ANTHROPIC_API_KEY": "sk-ant-api-secret",
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-auth-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-secret",
        "CODEX_HOME": "/Users/someone/.codex",
    }


class TestCodexEnvironmentPolicy:
    def test_allowlist_excludes_slack_and_other_engine_credentials(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/codex-home"))
        env = policy.build(_rich_source_env())

        assert "SLACK_BOT_TOKEN" not in env
        assert "SLACK_APP_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_pins_codex_home_to_bot_specific_path_not_user_path(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/codex-home"))
        env = policy.build(_rich_source_env())

        # 실행 환경에 사용자 개인 CODEX_HOME 이 있어도 봇 전용 경로로 덮어써야 한다.
        assert env["CODEX_HOME"] == "/bot/codex-home"
        assert env["CODEX_HOME"] != _rich_source_env()["CODEX_HOME"]

    def test_carries_bot_profile_name_for_downstream_scripts(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["BOT_PROFILE"] == "examplebot"

    def test_carries_path_and_lang_from_source(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert env["LANG"] == "en_US.UTF-8"

    def test_defaults_path_and_lang_when_source_missing_them(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build({})

        assert env["PATH"] == "/usr/bin:/bin"
        assert env["LANG"] == "ko_KR.UTF-8"

    def test_omits_codex_home_when_profile_has_none(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert "CODEX_HOME" not in env

    def test_pins_home_to_bot_home_not_just_codex_home(self):
        """CODEX_HOME 만 덮어쓰고 HOME 은 그대로 두면, 부모 프로세스의 HOME 이
        다른 봇 또는 사용자 개인 홈을 가리킬 때 그 경로가 그대로 샌다
        (sca-kos.5). HOME 도 봇 전용 경로로 덮어써야 한다."""
        source = dict(_rich_source_env())
        source["HOME"] = "/Users/other-bot-home"
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/codex-home"))

        env = policy.build(source)

        assert env["HOME"] == "/bot/codex-home"
        assert env["HOME"] != source["HOME"]

    def test_falls_back_to_source_home_when_profile_has_no_home_dir(self):
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["HOME"] == "/Users/someone"

    def test_does_not_mutate_source_env(self):
        source = _rich_source_env()
        snapshot = dict(source)
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/codex-home"))

        policy.build(source)

        assert source == snapshot


class TestGeminiEnvironmentPolicy:
    def test_allowlist_excludes_slack_and_other_engine_credentials(self):
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/gemini-home"))
        env = policy.build(_rich_source_env())

        assert "SLACK_BOT_TOKEN" not in env
        assert "SLACK_APP_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "CODEX_HOME" not in env

    def test_pins_home_to_bot_specific_path_not_user_path(self):
        """agy has no dedicated relocation var, so isolation overwrites HOME itself."""
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/gemini-home"))
        env = policy.build(_rich_source_env())

        assert env["HOME"] == "/bot/gemini-home"
        assert env["HOME"] != _rich_source_env()["HOME"]

    def test_pins_home_away_from_a_different_bots_home_in_source_env(self):
        """부모 프로세스의 HOME 이 다른 봇의 홈을 가리켜도(예: 오기동·설정
        실수) 이 봇의 home_dir 가 이겨야 한다."""
        source = dict(_rich_source_env())
        source["HOME"] = "/Users/other-bot-home"
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/gemini-home"))

        env = policy.build(source)

        assert env["HOME"] == "/bot/gemini-home"
        assert env["HOME"] != source["HOME"]

    def test_carries_bot_profile_name_for_downstream_scripts(self):
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["BOT_PROFILE"] == "examplebot"

    def test_defaults_path_and_lang_when_source_missing_them(self):
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build({})

        assert env["PATH"] == "/usr/bin:/bin"
        assert env["LANG"] == "ko_KR.UTF-8"

    def test_falls_back_to_source_home_when_profile_has_none(self):
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["HOME"] == "/Users/someone"

    def test_does_not_mutate_source_env(self):
        source = _rich_source_env()
        snapshot = dict(source)
        policy = GeminiEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/gemini-home"))

        policy.build(source)

        assert source == snapshot


class TestClaudeEnvironmentPolicy:
    def test_strips_anthropic_api_key_and_auth_token(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_allowlist_excludes_slack_and_other_engine_credentials(self):
        """차단 목록에서 허용 목록으로 바꿨다(sca-kos.5) — 부모 프로세스의
        슬랙 토큰·다른 엔진 자격증명·세션 IPC 값이 새지 않아야 한다."""
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert "SLACK_BOT_TOKEN" not in env
        assert "SLACK_APP_TOKEN" not in env
        assert "CODEX_HOME" not in env

    def test_allowlist_excludes_other_claude_session_ipc_vars(self):
        """실측(2026-09-15)에서 CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN 이 이
        세션의 os.environ 에 실제로 있었다 — 봇 프로세스로 새면 다른 세션의
        IPC 채널에 연결될 위험이 있다."""
        source = dict(_rich_source_env())
        source["CLAUDE_CODE_MESSAGING_SOCKET"] = "/tmp/claude-messaging.sock"
        source["CLAUDE_CODE_MESSAGING_TOKEN"] = "messaging-secret"
        source["CLAUDE_CODE_SESSION_ID"] = "other-session-id"
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)

        env = policy.build(source)

        assert "CLAUDE_CODE_MESSAGING_SOCKET" not in env
        assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
        assert "CLAUDE_CODE_SESSION_ID" not in env

    def test_preserves_oauth_token_for_subscription_auth(self):
        """허용 목록으로 바뀌어도, 원래 차단 목록의 목적이던 구독 OAuth 인증
        경로는 계속 동작해야 한다."""
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-secret"

    def test_carries_path_and_lang_from_source(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert env["LANG"] == "en_US.UTF-8"

    def test_defaults_path_and_lang_when_source_missing_them(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build({})

        assert env["PATH"] == "/usr/bin:/bin"
        assert env["LANG"] == "ko_KR.UTF-8"

    def test_omits_oauth_token_when_source_missing_it(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build({})

        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_carries_bot_profile_name_for_downstream_scripts(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["BOT_PROFILE"] == "examplebot"

    def test_does_not_mutate_source_env(self):
        source = _rich_source_env()
        snapshot = dict(source)
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)

        policy.build(source)

        assert source == snapshot


class TestCreateEnvironmentPolicy:
    def test_selects_codex_policy_by_engine_name(self):
        policy = create_environment_policy("codex", profile_name="examplebot", home_dir=None)
        assert isinstance(policy, CodexEnvironmentPolicy)

    def test_selects_claude_policy_by_engine_name(self):
        policy = create_environment_policy("claude", profile_name="examplebot", home_dir=None)
        assert isinstance(policy, ClaudeEnvironmentPolicy)

    def test_selects_gemini_policy_by_engine_name(self):
        policy = create_environment_policy("gemini", profile_name="examplebot", home_dir=None)
        assert isinstance(policy, GeminiEnvironmentPolicy)

    def test_unknown_engine_name_raises(self):
        with pytest.raises(ConfigError):
            create_environment_policy("unknown", profile_name="examplebot", home_dir=None)

    def test_unknown_engine_name_lists_known_engines_in_error(self):
        with pytest.raises(ConfigError, match="codex.*claude|claude.*codex"):
            create_environment_policy("unknown", profile_name="examplebot", home_dir=None)


class _FakeEnvironmentPolicy(EngineEnvironmentPolicy):
    """등록 시험용 가짜 정책. 아무 변수도 넘기지 않는다."""

    def _base_env(self, source_env):
        return {}


class TestEngineEnvironmentPolicyRegistry:
    """레지스트리 자체의 등록·해제 동작을 검증한다.

    모듈 전역 ``registry`` 를 직접 건드리는 시험은 반드시 ``finally`` 에서
    ``unregister()`` 로 되돌린다. 그러지 않으면 등록한 것이 다음 시험에
    남는다.
    """

    def test_register_opens_up_a_new_engine_name(self):
        local_registry = EngineEnvironmentPolicyRegistry()
        local_registry.register("fake", _FakeEnvironmentPolicy)

        policy = local_registry.create("fake", profile_name="examplebot", home_dir=None)

        assert isinstance(policy, _FakeEnvironmentPolicy)

    def test_unregister_removes_a_previously_registered_name(self):
        local_registry = EngineEnvironmentPolicyRegistry()
        local_registry.register("fake", _FakeEnvironmentPolicy)

        local_registry.unregister("fake")

        with pytest.raises(ConfigError):
            local_registry.create("fake", profile_name="examplebot", home_dir=None)

    def test_unregister_unknown_name_is_a_no_op(self):
        local_registry = EngineEnvironmentPolicyRegistry()

        local_registry.unregister("never-registered")  # 예외 없이 넘어가야 한다.

    def test_known_names_lists_registered_engines_sorted(self):
        local_registry = EngineEnvironmentPolicyRegistry()
        local_registry.register("zeta", _FakeEnvironmentPolicy)
        local_registry.register("alpha", _FakeEnvironmentPolicy)

        assert local_registry.known_names() == ["alpha", "zeta"]

    def test_separate_registry_instances_do_not_share_registrations(self):
        """인스턴스마다 독립된 dict 를 쥐어야 한다 — 클래스 변수로 공유하면
        한 인스턴스의 등록이 다른 인스턴스에도 보인다."""
        registry_a = EngineEnvironmentPolicyRegistry()
        registry_b = EngineEnvironmentPolicyRegistry()

        registry_a.register("fake", _FakeEnvironmentPolicy)

        assert "fake" not in registry_b.known_names()


class TestDefaultRegistryExtension:
    """모듈 전역 ``registry`` 로 확장할 때의 시험 격리를 검증한다."""

    def test_registering_on_default_registry_makes_create_environment_policy_see_it(self):
        try:
            registry.register("fake", _FakeEnvironmentPolicy)
            policy = create_environment_policy("fake", profile_name="examplebot", home_dir=None)
            assert isinstance(policy, _FakeEnvironmentPolicy)
        finally:
            registry.unregister("fake")

        # 정리 후에는 다시 알 수 없는 엔진이어야 한다 — 이 시험이 다음 시험을 오염시키지 않는다.
        with pytest.raises(ConfigError):
            create_environment_policy("fake", profile_name="examplebot", home_dir=None)

    def test_previous_test_registration_did_not_leak_into_this_test(self):
        """앞 시험이 등록한 "fake" 가 정리됐는지 독립적으로 확인한다.

        시험 실행 순서가 바뀌어도 이 시험 하나만으로 오염 여부를 판정할 수
        있게, known_names() 에 "fake" 가 없는 것까지 함께 본다.
        """
        assert "fake" not in registry.known_names()
        assert {"codex", "claude", "gemini"}.issubset(set(registry.known_names()))


class TestClaudeAuthNeedsUser:
    """allowlist 로 바꿀 때 USER 를 빠뜨려 claude 가 '로그인되지 않음' 으로
    실패했다. 2026-09-15 에 실제 CLI 로 확인했다 — PATH/LANG/HOME 만으로는
    인증이 안 되고 USER 를 더하면 된다. SHELL·LOGNAME·TMPDIR 은 무관했다.
    """

    def test_USER가_전달된다(self) -> None:
        policy = ClaudeEnvironmentPolicy(profile_name="봇A")
        env = policy.build({"PATH": "/bin", "USER": "someone"})
        assert env["USER"] == "someone"

    def test_USER가_없으면_키를_안_만든다(self) -> None:
        policy = ClaudeEnvironmentPolicy(profile_name="봇A")
        env = policy.build({"PATH": "/bin"})
        assert "USER" not in env
