"""B15: peer-bot revival guidance.

A bot that has gone down cannot announce its own death. The person asks the
surviving peer, which must be told — in words — how to look at the peer's
state, pid, logs and how to restart it. Permission without this note means the
model assumes everything outside its own state dir is blocked and never tries
(mametchi peer_recovery_note, bot.py:2614).
"""

from __future__ import annotations

from pathlib import Path

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.profile import PeerProfile, Profile
from slack_cli_agent.prompt.sections import CompositionContext, PeerRecoverySection


def _peer() -> PeerProfile:
    return PeerProfile(
        name="mametchi",
        display_name="마메치",
        state_dir=Path("/home/op/.mametchi"),
        work_root=Path("/home/op/.mametchi/work"),
        launch_label="local.mametchi",
    )


def _ctx(trust: TrustLevel, *, dm: bool = True) -> CompositionContext:
    principal = Principal(user_id="U_OWNER", channel="C1", trust=trust, is_direct_message=dm)
    return CompositionContext(principal=principal)


def test_peer_profile_parsed_from_dict() -> None:
    data = {
        "name": "guchipachi",
        "primary_engine": {"type": "codex", "binary": "codex", "model": "gpt-5"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "peer": {
            "name": "mametchi",
            "display_name": "마메치",
            "state_dir": "/home/op/.mametchi",
            "work_root": "/home/op/.mametchi/work",
            "launch_label": "local.mametchi",
        },
    }
    profile = Profile.from_dict(data)
    assert profile.peer is not None
    assert profile.peer.name == "mametchi"
    assert profile.peer.display_name == "마메치"
    assert profile.peer.state_dir == Path("/home/op/.mametchi")
    assert profile.peer.launch_label == "local.mametchi"


def test_no_peer_block_means_none() -> None:
    data = {
        "name": "guchipachi",
        "primary_engine": {"type": "codex", "binary": "codex", "model": "gpt-5"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
    }
    assert Profile.from_dict(data).peer is None


def test_peer_defaults_work_root_under_state_dir() -> None:
    data = {
        "name": "guchipachi",
        "primary_engine": {"type": "codex", "binary": "codex", "model": "gpt-5"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
        "peer": {"name": "mametchi", "state_dir": "/home/op/.mametchi"},
    }
    peer = Profile.from_dict(data).peer
    assert peer is not None
    # display_name falls back to name; work_root falls back to <state_dir>/work
    assert peer.display_name == "mametchi"
    assert peer.work_root == Path("/home/op/.mametchi/work")
    assert peer.launch_label == "local.mametchi"


def test_peer_requires_name_and_state_dir() -> None:
    import pytest

    from slack_cli_agent.core.errors import ConfigError

    base = {
        "name": "guchipachi",
        "primary_engine": {"type": "codex", "binary": "codex", "model": "gpt-5"},
        "owner_user_id": "U1",
        "troubleshoot_channel": "C1",
    }
    with pytest.raises(ConfigError):
        Profile.from_dict({**base, "peer": {"name": "mametchi"}})  # no state_dir
    with pytest.raises(ConfigError):
        Profile.from_dict({**base, "peer": {"state_dir": "/x"}})  # no name


def test_section_renders_for_owner_in_dm() -> None:
    section = PeerRecoverySection(_peer())
    ctx = _ctx(TrustLevel.OWNER, dm=True)
    assert section.applies_to(ctx) is True
    text = section.render(ctx)
    # Names the peer and every place the model must look.
    assert "마메치" in text
    assert "/home/op/.mametchi" in text
    assert "/home/op/.mametchi/work" in text
    # Launch label drives both status and restart, with the per-role split.
    assert "local.mametchi" in text
    assert "kickstart" in text
    # pid files are the two service roles, not a single bot.pid.
    assert "ingress.pid" in text
    assert "worker.pid" in text
    # The guardrail: do not write to the peer except to revive it.
    assert "되살리" in text


def test_section_absent_without_peer() -> None:
    section = PeerRecoverySection(None)
    assert section.applies_to(_ctx(TrustLevel.OWNER, dm=True)) is False
    assert section.render(_ctx(TrustLevel.OWNER, dm=True)) == ""


def test_section_hidden_from_non_owner() -> None:
    section = PeerRecoverySection(_peer())
    # A general user must not learn how to reach into the peer's state dir.
    assert section.applies_to(_ctx(TrustLevel.GENERAL, dm=True)) is False
    # Owner but not in a DM / without granted authority: mechanism stays closed.
    owner_public = _ctx(TrustLevel.OWNER, dm=False)
    assert section.applies_to(owner_public) is False


def test_section_shown_to_owner_with_granted_authority() -> None:
    section = PeerRecoverySection(_peer())
    principal = Principal(user_id="U_OWNER", channel="C1", trust=TrustLevel.OWNER, is_direct_message=False)
    ctx = CompositionContext(principal=principal, authority_granted=True)
    assert section.applies_to(ctx) is True
