"""Chooses which environment variables to pass to an engine process.

Ports two spots from the original bot.py:

- codex_environment() (bot.py:1524) — Codex's environment, allowlist
  style. Only a minimal PATH/LANG/HOME plus BOT_PROFILE and the
  engine's own home path (CODEX_HOME) get through; Slack tokens and
  other engines' credentials were never in the list to begin with.
- _run_claude() (bot.py:1678-1681) — Claude's environment, originally
  denylist style: copied the whole environment and stripped only
  ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN to force the OAuth token path.

That denylist shape was re-examined for cross-bot isolation
(2026-09-15, sca-kos.5) by listing this session's own os.environ key
names. It let through CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN (another
Claude Code session's IPC channel), CLAUDE_CODE_SESSION_ID/CHILD_SESSION,
SLACK_MCP_XOXC_TOKEN/XOXD_TOKEN, GEMINI_API_KEY, and other unrelated
credentials — any parent-process variable not on the two-item denylist
reaches the child unfiltered. ClaudeEnvironmentPolicy was switched to
allowlist, the same shape as Codex/Gemini, so a new risky variable
appearing in the parent process can't silently leak in; the OAuth token
path is kept working by explicitly allowing CLAUDE_CODE_OAUTH_TOKEN
through.

Skipping the engine's own home path lets it fall through to the user's
personal config, session, and auth — breaking isolation, which is the
reason this module exists.

Reads only from the caller-supplied source_env, not os.environ
directly, so tests aren't coupled to the real process environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from ..core.errors import ConfigError


class EngineEnvironmentPolicy(ABC):
    """Contract for choosing env vars for one engine.

    Allowlist vs. denylist differs per engine, so the base class only
    holds the shared shape and leaves selection to subclasses.
    """

    #: Env var name for the bot's own home path. None means this
    #: engine has no home-path concept, so home_dir is never passed
    #: even if set.
    HOME_ENV_VAR: str | None = None

    def __init__(self, profile_name: str, home_dir: Path | None = None) -> None:
        self.profile_name = profile_name
        self.home_dir = home_dir

    def build(self, source_env: Mapping[str, str]) -> dict[str, str]:
        """Env vars to pass to this engine's process. source_env is read, never mutated."""
        env = self._base_env(source_env)
        if self.HOME_ENV_VAR and self.home_dir is not None:
            env[self.HOME_ENV_VAR] = str(self.home_dir)
        return env

    @abstractmethod
    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]: ...


class CodexEnvironmentPolicy(EngineEnvironmentPolicy):
    """Codex's process environment — allowlist, same as the original codex_environment()."""

    HOME_ENV_VAR = "CODEX_HOME"

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        # HOME itself, not just CODEX_HOME, is pinned to this bot's home
        # when one is configured. Otherwise a parent process whose HOME
        # points at a different bot's directory (or a person's own home)
        # leaks that path into this bot's process even though CODEX_HOME
        # is correctly overridden below by build().
        home = str(self.home_dir) if self.home_dir is not None else source_env.get("HOME", "")
        return {
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": home,
            # Helper scripts the bot invokes need to know which bot
            # they're running as — without this, they'd pick the wrong
            # bot's token and channel config.
            "BOT_PROFILE": self.profile_name,
        }


class GeminiEnvironmentPolicy(EngineEnvironmentPolicy):
    """agy's process environment — allowlist, same shape as Codex's.

    agy has no dedicated env var for relocating its config directory
    (confirmed by testing, see docs/agy-실측.md) — it always reads
    ~/.gemini/. Isolation per bot means overwriting HOME itself, so this
    is the one policy where HOME_ENV_VAR is "HOME" rather than an
    engine-specific var name.
    """

    HOME_ENV_VAR = "HOME"

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        return {
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": source_env.get("HOME", ""),
            "BOT_PROFILE": self.profile_name,
        }


class ClaudeEnvironmentPolicy(EngineEnvironmentPolicy):
    """Claude's process environment — allowlist, same shape as Codex/Gemini.

    Was a denylist that copied the whole parent environment through
    except ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN. Converted to allowlist
    for cross-bot isolation (see module docstring); CLAUDE_CODE_OAUTH_TOKEN
    is explicitly carried through so the OAuth subscription auth path the
    denylist was originally built for keeps working.
    """

    #: Vars beyond the fixed PATH/LANG/HOME/BOT_PROFILE set that are
    #: individually let through when present, because the engine needs
    #: them to function. Values are still read from source_env, not
    #: hardcoded — only the key names are an allowlist.
    ADDITIONAL_ALLOWED_VARS: frozenset[str] = frozenset({
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Without USER the CLI reports "Not logged in · Please run /login"
        # even with HOME set (checked against the real CLI 2026-09-15).
        # SHELL, LOGNAME and TMPDIR made no difference.
        "USER",
    })

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        env = {
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": source_env.get("HOME", ""),
            "BOT_PROFILE": self.profile_name,
        }
        for key in self.ADDITIONAL_ALLOWED_VARS:
            if key in source_env:
                env[key] = source_env[key]
        return env


class EngineEnvironmentPolicyRegistry:
    """Registry of env-var policy classes by engine name.

    Same pattern as EngineRegistry in engine/registry.py. This repo
    aims to be a general-purpose utility, not tied to one
    organization, so policy classes aren't hardcoded into a dict
    literal here — register() keeps it open to extension.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[EngineEnvironmentPolicy]] = {}

    def register(self, name: str, policy_class: type[EngineEnvironmentPolicy]) -> None:
        """Registers a policy class for an engine name. Overwrites an existing registration."""
        self._classes[name] = policy_class

    def unregister(self, name: str) -> None:
        """Removes a registration; no-op if the name isn't there.

        Used to clean up a policy a test registered, so it doesn't
        leak into the next test. Any test using the module-level
        ``registry`` mutable singleton must restore it in a finally
        block.
        """
        self._classes.pop(name, None)

    def create(
        self, name: str, profile_name: str, home_dir: Path | None,
    ) -> EngineEnvironmentPolicy:
        cls = self._classes.get(name)
        if cls is None:
            known = ", ".join(sorted(self._classes)) or "없음"
            raise ConfigError(f"엔진 {name} 의 환경 변수 정책이 없다. 등록된 엔진: {known}")
        return cls(profile_name=profile_name, home_dir=home_dir)

    def known_names(self) -> list[str]:
        return sorted(self._classes)


def _build_default_registry() -> EngineEnvironmentPolicyRegistry:
    default_registry = EngineEnvironmentPolicyRegistry()
    default_registry.register("codex", CodexEnvironmentPolicy)
    default_registry.register("claude", ClaudeEnvironmentPolicy)
    default_registry.register("gemini", GeminiEnvironmentPolicy)
    return default_registry


#: This module's default registry, used by create_environment_policy().
#: Extend via registry.register(name, policy_class) rather than
#: editing this module's dict literal.
registry = _build_default_registry()


def create_environment_policy(
    engine_name: str, profile_name: str, home_dir: Path | None,
) -> EngineEnvironmentPolicy:
    """Creates the right policy for an engine name.

    Keeps the existing signature used by core/application.py; the
    actual lookup is delegated to the module-level registry.
    """
    return registry.create(engine_name, profile_name=profile_name, home_dir=home_dir)
