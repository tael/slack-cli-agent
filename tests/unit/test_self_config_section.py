"""SelfConfigSection: the owner's turn is told where this bot's own settings are.

2026-11-16 asuka: asked to switch its model, the turn looked only inside the
state dir, hit the denied engine/ directory and asked the owner which file to
edit. The model lives in the profile file, outside the state dir.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.prompt.sections import CompositionContext, SelfConfigSection


def _profile(tmp: Path) -> Profile:
    data = {
        "name": "asuka",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "claude-sonnet-5"},
        "owner_user_id": "U_OWNER",
        "troubleshoot_channel": "C1",
        "state_dir": str(tmp / ".asuka"),
    }
    return replace(Profile.from_dict(data), source_file=tmp / "profiles" / "asuka.json")


def _ctx(trust: TrustLevel, **kw: object) -> CompositionContext:
    principal = Principal(user_id="U_OWNER", channel="C1", trust=trust, is_direct_message=False)
    return CompositionContext(principal=principal, **kw)  # type: ignore[arg-type]


def test_owner_turn_names_profile_file_and_model_key(tmp_path: Path) -> None:
    section = SelfConfigSection(_profile(tmp_path))
    ctx = _ctx(TrustLevel.OWNER)
    assert section.applies_to(ctx)
    text = section.render(ctx)
    assert str(tmp_path / "profiles" / "asuka.json") in text
    assert "primary_engine.model" in text
    assert "claude-sonnet-5" in text
    assert str(tmp_path / ".asuka" / "channels.json") in text


def test_restart_goes_through_apply_request_marker(tmp_path: Path) -> None:
    text = SelfConfigSection(_profile(tmp_path)).render(_ctx(TrustLevel.OWNER))
    assert str(tmp_path / ".asuka" / "apply-request") in text
    assert "$TURN_ID" in text


def test_not_shown_to_non_owner(tmp_path: Path) -> None:
    section = SelfConfigSection(_profile(tmp_path))
    assert not section.applies_to(_ctx(TrustLevel.GENERAL))


def test_not_shown_on_aside_turn(tmp_path: Path) -> None:
    section = SelfConfigSection(_profile(tmp_path))
    assert not section.applies_to(_ctx(TrustLevel.OWNER, postmortem=True))
