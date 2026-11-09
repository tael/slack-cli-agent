"""Claude 계정 파일을 매 턴 다시 읽어 토큰을 고르는 동작 시험.

2026-11-07 : 봇 계정과 개인 계정을 한도에 따라 자동으로 바꾸려면 재기동 없이
계정 파일 하나만 바꿔 다음 턴에 반영돼야 한다.
"""

from __future__ import annotations

from slack_cli_agent.engine.environment import ClaudeEnvironmentPolicy


def _policy():
    return ClaudeEnvironmentPolicy(profile_name="examplebot", home_dir=None)


def _source(tmp_path, account):
    tokens = tmp_path / "tokens"
    tokens.mkdir(exist_ok=True)
    (tokens / "bot").write_text("tok-bot\n")
    (tokens / "personal").write_text("tok-personal\n")
    acct = tmp_path / "claude_account"
    acct.write_text(account + "\n")
    return {
        "PATH": "/usr/bin", "HOME": str(tmp_path),
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-launch",
        "CLAUDE_ACCOUNT_FILE": str(acct),
        "CLAUDE_TOKEN_DIRS": str(tmp_path / "missing") + ":" + str(tokens),
    }


def test_계정_파일의_토큰을_쓴다(tmp_path):
    env = _policy().build(_source(tmp_path, "personal"))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-personal"


def test_계정_파일을_바꾸면_다음_빌드에_반영된다(tmp_path):
    src = _source(tmp_path, "personal")
    policy = _policy()
    assert policy.build(src)["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-personal"
    (tmp_path / "claude_account").write_text("bot")
    assert policy.build(src)["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-bot"


def test_토큰이_없는_계정이면_기동_토큰을_유지한다(tmp_path):
    env = _policy().build(_source(tmp_path, "nobody"))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-launch"


def test_경로_문자가_든_계정_이름은_무시한다(tmp_path):
    env = _policy().build(_source(tmp_path, "../tokens/bot"))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-launch"


def test_설정이_없으면_기존_동작이다(tmp_path):
    src = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok-launch"}
    assert _policy().build(src)["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-launch"


def test_계정_변수는_자식_환경에_넘기지_않는다(tmp_path):
    env = _policy().build(_source(tmp_path, "bot"))
    assert "CLAUDE_ACCOUNT_FILE" not in env and "CLAUDE_TOKEN_DIRS" not in env


def test_이번_턴에_고른_계정_이름을_자식_환경에_넘긴다(tmp_path):
    """봇 턴의 셸이 다시 띄운 claude 는 Claude Code 가 토큰 변수를 지운 환경에서
    돈다. 셸 claude 래퍼가 계정 이름으로 같은 토큰을 고르게 이름을 넘긴다
    (2026-11-09 마메치 sca 전환 준비)."""
    src = _source(tmp_path, "personal")
    policy = _policy()
    assert policy.build(src)["CLAUDE_ACCOUNT"] == "personal"
    (tmp_path / "claude_account").write_text("bot")
    assert policy.build(src)["CLAUDE_ACCOUNT"] == "bot"


def test_토큰을_못_찾은_계정_이름은_넘기지_않는다(tmp_path):
    env = _policy().build(_source(tmp_path, "nobody"))
    assert "CLAUDE_ACCOUNT" not in env


def test_계정_파일이_없으면_계정_이름도_없다(tmp_path):
    src = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok-launch", "CLAUDE_ACCOUNT": "stale"}
    assert "CLAUDE_ACCOUNT" not in _policy().build(src)
