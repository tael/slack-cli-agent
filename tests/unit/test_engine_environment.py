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

    def test_does_not_mutate_source_env(self):
        source = _rich_source_env()
        snapshot = dict(source)
        policy = CodexEnvironmentPolicy(profile_name="examplebot", home_dir=Path("/bot/codex-home"))

        policy.build(source)

        assert source == snapshot


class TestClaudeEnvironmentPolicy:
    def test_strips_anthropic_api_key_and_auth_token_only(self):
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_preserves_oauth_token_and_other_vars(self):
        """원본은 차단 목록 방식이라 API 키 인증 경로만 제거하고 나머지는
        그대로 넘긴다. 허용 목록인 Codex 와 계약이 다르다."""
        policy = ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)
        env = policy.build(_rich_source_env())

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-secret"
        assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"

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
        assert {"codex", "claude"}.issubset(set(registry.known_names()))
