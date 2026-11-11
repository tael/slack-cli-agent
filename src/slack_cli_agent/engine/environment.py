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

import re

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from ..core.errors import ConfigError

#: Fixed values that stop git and gh from waiting on a terminal (C11). A bot
#: turn has no one at the keyboard: a credential prompt or a gh confirmation
#: holds the process until the turn timeout kills it. Ported from mametchi's
#: turn_budget_env (bot.py). Not credentials, so every engine gets them, and
#: they overwrite whatever the parent process had.
NON_INTERACTIVE_ENV: Mapping[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GH_PROMPT_DISABLED": "1",
    "CI": "1",
}

#: Seconds left at the end of a turn for writing the answer. Must equal
#: guard/pretool/turn_budget.DEFAULT_WRAPUP_SEC; a test pins the two together.
TURN_WRAPUP_SEC = 60

#: Claude Code's MCP tool-call limit in ms when the parent sets none. Matches
#: mametchi (bot.py MCP_TOOL_TIMEOUT 300000): slow Snowflake/CSV tools run up to
#: 280 s inside, so the default would cut them off first.
DEFAULT_MCP_TOOL_TIMEOUT_MS = "300000"


#: Comma-separated extra names the turn ID is also exported under, read from
#: the bot process's own environment (the profile's run.sh). Helper scripts
#: written for another runtime read the turn key under their own name --
#: mametchi's mb_card.py writes MAMETCHI_TURN into its audit row -- and would
#: otherwise log an empty turn (C10). The list itself is not passed on.
TURN_ID_ALIASES_ENV = "SCA_TURN_ID_ALIASES"

_ALIAS_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

#: Names an alias may never take even when the policy left them unset: the
#: fixed base env and engine homes (build() sets CODEX_HOME/HOME after
#: _base_env). Everything else the policy fills is protected by
#: _with_turn_id() reading the env it already built.
_RESERVED_ALIAS_NAMES = frozenset({
    "PATH", "LANG", "HOME", "USER", "SHELL", "BOT_PROFILE", "TURN_ID",
    "CODEX_HOME", "TMPDIR",
})


def _with_turn_id(
    env: dict[str, str],
    source_env: Mapping[str, str],
    guarded: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Add TURN_ID ("channel:ts") when the runner stamped one: a turn writes it
    as the apply-request marker's second line (reliability/restart.py), the
    same as mametchi apply.sh with MAMETCHI_TURN. Not a credential.

    Each valid name in SCA_TURN_ID_ALIASES gets the same value, so a wrapper
    launched from the engine's shell finds the turn under the name it reads.
    An alias never replaces a name the policy owns: anything already in env,
    NON_INTERACTIVE_ENV, or `guarded` (the policy's allowlist, which covers
    guard vars that are unset this turn -- an empty SCA_VERIFY_NESTED is what
    keeps the Stop hook's model verify on). Invalid or owned names are skipped
    rather than failing the turn."""
    turn = source_env.get("TURN_ID")
    if not turn:
        return env
    owned = set(env) | set(NON_INTERACTIVE_ENV) | guarded | _RESERVED_ALIAS_NAMES
    env["TURN_ID"] = turn
    for name in (source_env.get(TURN_ID_ALIASES_ENV) or "").split(","):
        name = name.strip()
        if name and _ALIAS_NAME_RE.fullmatch(name) and name not in owned:
            env[name] = turn
    return env


class EngineEnvironmentPolicy(ABC):
    """Contract for choosing env vars for one engine.

    Allowlist vs. denylist differs per engine, so the base class only
    holds the shared shape and leaves selection to subclasses.
    """

    #: Env var name for the bot's own home path. None means this
    #: engine has no home-path concept, so home_dir is never passed
    #: even if set.
    HOME_ENV_VAR: str | None = None

    #: Login files the engine expects inside its home, as (path under
    #: home_dir, path under the operator's own home) pairs. A fresh
    #: per-bot home has none of them and the engine then fails at answer
    #: time rather than at boot, so preflight reads this (sca-kos.6).
    #: Paired so the place to copy from cannot drift from the place it
    #: goes. Empty means the engine keeps credentials elsewhere.
    CREDENTIAL_FILES: tuple[tuple[str, str], ...] = ()

    def __init__(self, profile_name: str, home_dir: Path | None = None) -> None:
        if home_dir is not None and self.HOME_ENV_VAR is None:
            raise ConfigError(
                f"{profile_name} 프로필: 이 엔진은 봇별 홈을 지원하지 않는다. "
                "home_dir 을 지우거나 다른 엔진을 쓴다"
            )
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
    CREDENTIAL_FILES = (("auth.json", ".codex/auth.json"),)

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        # HOME itself, not just CODEX_HOME, is pinned to this bot's home
        # when one is configured. Otherwise a parent process whose HOME
        # points at a different bot's directory (or a person's own home)
        # leaks that path into this bot's process even though CODEX_HOME
        # is correctly overridden below by build().
        home = str(self.home_dir) if self.home_dir is not None else source_env.get("HOME", "")
        return _with_turn_id({
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": home,
            # Helper scripts the bot invokes need to know which bot
            # they're running as — without this, they'd pick the wrong
            # bot's token and channel config.
            "BOT_PROFILE": self.profile_name,
            **NON_INTERACTIVE_ENV,
        }, source_env, _GUARD_VAR_NAMES)


class GeminiEnvironmentPolicy(EngineEnvironmentPolicy):
    """agy's process environment — allowlist, same shape as Codex's.

    agy has no dedicated env var for relocating its config directory
    (confirmed by testing, see docs/agy-실측.md) — it always reads
    ~/.gemini/. Isolation per bot means overwriting HOME itself, so this
    is the one policy where HOME_ENV_VAR is "HOME" rather than an
    engine-specific var name.
    """

    HOME_ENV_VAR = "HOME"
    _AGY_TOKEN = ".gemini/antigravity-cli/antigravity-oauth-token"
    CREDENTIAL_FILES = ((_AGY_TOKEN, _AGY_TOKEN),)

    def _base_env(self, source_env: Mapping[str, str]) -> dict[str, str]:
        return _with_turn_id({
            "PATH": source_env.get("PATH", "/usr/bin:/bin"),
            "LANG": source_env.get("LANG", "ko_KR.UTF-8"),
            "HOME": source_env.get("HOME", ""),
            "BOT_PROFILE": self.profile_name,
            **NON_INTERACTIVE_ENV,
        }, source_env, _GUARD_VAR_NAMES)


def _account_file_token(source_env: Mapping[str, str]) -> tuple[str, str] | None:
    """Re-read the Claude subscription token named by CLAUDE_ACCOUNT_FILE, per turn.

    The launcher used to read the token once into CLAUDE_CODE_OAUTH_TOKEN, so
    switching accounts (a bot account vs a personal one, e.g. when one hits its
    usage limit) needed a restart. When CLAUDE_ACCOUNT_FILE names a file holding
    an account name, the token is looked up as <dir>/<name> in each directory of
    CLAUDE_TOKEN_DIRS (colon-separated) on every turn, so an external switcher
    only rewrites that one file. Any miss keeps the launcher's token. The token
    value is never logged. Returns (account name, token).
    """
    account_file = source_env.get("CLAUDE_ACCOUNT_FILE")
    token_dirs = source_env.get("CLAUDE_TOKEN_DIRS")
    if not account_file or not token_dirs:
        return None
    try:
        name = Path(account_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or "/" in name or name.startswith("."):
        return None
    for d in token_dirs.split(":"):
        if not d:
            continue
        try:
            token = (Path(d) / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return name, token
    return None


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
        # guard/finish_review.py's built-in Stop hook reads these to decide
        # block vs. release (time_left()) and to resolve whether the
        # current channel requires an advisor call (resolve_advisor_channel:
        # POST_CHANNEL is the current channel, ADVISOR_CHANNELS the set that
        # requires one) or a code lookup before a doc-based answer
        # (resolve_code_first_channel: CODE_FIRST_CHANNELS, G13). All read-only
        # facts the hook needs, not credentials, so they're safe to let through
        # the same allowlist as everything else here.
        "TURN_DEADLINE",
        "TURN_DEADLINE_MONO",
        # From turn_budget_env (C11): the wrap-up margin the PreToolUse guard
        # subtracts, and Claude Code's own Bash timeout ceiling for this turn.
        "TURN_WRAPUP_SEC",
        "BASH_MAX_TIMEOUT_MS",
        # MCP tool-call limit; defaulted below when the parent sets none.
        "MCP_TOOL_TIMEOUT",
        "POST_CHANNEL",
        "ADVISOR_CHANNELS",
        "CODE_FIRST_CHANNELS",
        # finish_review.named_turn: the owner's display name, so "<이름> 턴"
        # without 님 is caught too. A name, not a credential.
        "OWNER_NAME",
        # guard/pretool/plan_gate: the response number from which a call needs
        # a `[계획]` block first. Set per turn by the pipeline (W4); a number.
        "PLAN_GATE",
        # guard/plan_start: planner model override (default haiku). A model name.
        "SCA_PLAN_MODEL",
        # Stop hook model verify toggle and model (finish_review.
        # resolve_verify_settings). On by default; these only override.
        "SCA_FINISH_VERIFY",
        "SCA_FINISH_VERIFY_MODEL",
        # Set inside the nested verify claude so its own run never re-verifies.
        "SCA_VERIFY_NESTED",
        # Where the self-restart guard points a turn instead (apply-request
        # marker) and which labels it treats as the bot itself (H3). Paths and
        # launchd labels, not credentials.
        "RESTART_GUARD_MARKER",
        "RESTART_GUARD_LABELS",
        "RESTART_GUARD_SERVICES",
        # TURN_ID ("channel:ts") and its aliases go through _with_turn_id().
    })

    #: Empty on purpose: claude keeps its subscription credentials in the
    #: macOS keychain and CLAUDE_CODE_OAUTH_TOKEN, not under its home.
    CREDENTIAL_FILES: tuple[tuple[str, str], ...] = ()

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
        account = _account_file_token(source_env)
        if account:
            # Claude Code drops CLAUDE_CODE_OAUTH_TOKEN from its Bash children;
            # a shell-side claude wrapper picks the same token by this name.
            env["CLAUDE_ACCOUNT"], env["CLAUDE_CODE_OAUTH_TOKEN"] = account
        env.setdefault("MCP_TOOL_TIMEOUT", DEFAULT_MCP_TOOL_TIMEOUT_MS)
        env.update(NON_INTERACTIVE_ENV)
        return _with_turn_id(env, source_env, self.ADDITIONAL_ALLOWED_VARS)


#: Guard and turn-fact var names the built-in hooks read (finish_review,
#: turn_budget). Codex and Gemini do not pass them through, but an alias must
#: not mint one there either: the names mean the same thing to any hook.
_GUARD_VAR_NAMES = ClaudeEnvironmentPolicy.ADDITIONAL_ALLOWED_VARS


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

    def policy_class(self, name: str) -> type[EngineEnvironmentPolicy] | None:
        """For callers that need a policy's declarations without building one —
        preflight reads CREDENTIAL_FILES before any engine process exists."""
        return self._classes.get(name)

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


def turn_budget_env(
    timeout_sec: float, *, now_wall: float, now_monotonic: float,
) -> dict[str, str]:
    """The turn deadline as env vars for the engine child process (A28).

    The engine's own Stop hook (guard/finish_review.time_left) and the Bash
    PreToolUse guard (guard/pretool/turn_budget) both read TURN_DEADLINE_MONO
    first (sleep-proof, system-wide monotonic clock) and fall back to
    TURN_DEADLINE (wall clock). Both are absolute epochs, not durations, so a
    child that re-reads them mid-turn still computes the right remaining time.

    Ported from mametchi's MAMETCHI_TURN_DEADLINE[_MONO] (bot.py), renamed to
    organization-neutral names. ClaudeEnvironmentPolicy's allowlist already
    carries both names through, so stamping them here is all the child needs to
    finish inside its budget rather than being killed mid-write at timeout.

    TURN_WRAPUP_SEC and BASH_MAX_TIMEOUT_MS come from mametchi too (C11). A
    single Bash call may not use the wrap-up margin, so Claude Code's Bash
    ceiling is the budget minus that margin, floored at 60 s as in bot.py.

    A pure function (clocks are passed in, not read) so a test can assert the
    exact deadline without touching the wall or monotonic clock.
    """
    usable = max(int(timeout_sec) - TURN_WRAPUP_SEC, 60)
    return {
        "TURN_DEADLINE": str(now_wall + timeout_sec),
        "TURN_DEADLINE_MONO": str(now_monotonic + timeout_sec),
        "TURN_WRAPUP_SEC": str(TURN_WRAPUP_SEC),
        "BASH_MAX_TIMEOUT_MS": str(usable * 1000),
    }
