from slack_cli_agent.engine import claude
from slack_cli_agent.engine.claude_settings import merge_settings


def test_enabled_only_for_listed_channel():
    env = {"SCA_AGENT_VERIFY_CHANNELS": "C0CBQA67AAH, C1"}
    assert claude.agent_verify_enabled("C0CBQA67AAH", env)
    assert not claude.agent_verify_enabled("C01EWFYL077", env)
    assert not claude.agent_verify_enabled("", env)
    assert not claude.agent_verify_enabled("C0CBQA67AAH", {})


def test_agent_hook_is_added_beside_command_stop_hook():
    merged = merge_settings(claude.guard_hook_settings(), claude.agent_verify_hook_settings())
    types = [h["type"] for entry in merged["hooks"]["Stop"] for h in entry["hooks"]]
    assert "agent" in types and "command" in types
