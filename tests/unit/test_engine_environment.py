"""엔진 프로세스에 넘길 환경 변수 선별 시험.

원본 bot.py:1524 codex_environment() 와 bot.py:1678-1681 _run_claude() 의
환경 변수 처리를 이관한 것을 검증한다. 자격증명 유출 방지가 목적이므로
제외 대상이 실제로 빠지는 것을 확인하는 데 집중한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.engine.environment import (
    ClaudeEnvironmentPolicy,
    CodexEnvironmentPolicy,
    create_environment_policy,
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
        with pytest.raises(Exception):
            create_environment_policy("unknown", profile_name="examplebot", home_dir=None)
