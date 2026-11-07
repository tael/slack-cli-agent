"""Concrete preflight checks.

McpServerCheck also checks the shebang interpreter because a LaunchAgent's
narrow PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) can make a `#!/usr/bin/env
node` shebang silently fail, dropping the tool without any error.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar

from ..auth.policy import EFFORT_LEVELS, OWNER_EFFORT_MIN
from ..auth.principal import TrustLevel
from ..config.channel import ChannelConfig, ChannelRegistry
from ..config.profile import EXAMPLE_SUFFIX, Profile
from ..config.profile import KNOWN_KEYS as PROFILE_KNOWN_KEYS
from ..config.settings import RuntimeSettings
from ..core.errors import ConfigError
from ..engine.capability import ToolRestriction
from ..engine.claude_settings import load_merged_settings
from ..engine.environment import registry
from ..engine.mcp_health import broken_mcp_servers
from ..engine.registry import default_registry
from .check import CheckResult, PreflightCheck, PreflightContext

#: Group and other bits. A login file readable past its owner is not isolated.
_SHARED_BITS = stat.S_IRWXG | stat.S_IRWXO


class PromptFileCheck(PreflightCheck):
    """Checks that required prompt files exist and aren't empty.

    Required names come from the caller — the names its `PromptSection`
    list actually needs — since there's no single source file to scan for them.
    """

    name: ClassVar[str] = "prompt_files"

    def __init__(self, required_names: list[str]) -> None:
        self._required_names = tuple(required_names)

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not self._required_names:
            return CheckResult(ok=True, detail="점검할 프롬프트 이름이 없다")
        prompts_dir = ctx.profile.paths.prompts
        bad: list[str] = []
        for raw_name in self._required_names:
            path = prompts_dir / f"{raw_name.lower()}.md"
            if not path.exists():
                bad.append(f"{raw_name} : 파일 없음 {path}")
            elif not path.read_text(encoding="utf-8").strip():
                bad.append(f"{raw_name} : 파일이 비어 있다 {path}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="프롬프트 파일 점검 통과")


class WorkdirCheck(PreflightCheck):
    """Checks the work directory exists, is outside $HOME, and has no CLAUDE.md.

    A missing directory fails silently: subprocess can't set cwd, every
    request fails, but the bot boots fine with nothing logged. A directory
    under $HOME pulls global instructions into every turn's token budget.
    """

    name: ClassVar[str] = "workdir"

    def __init__(self, extra_dirs: list[str] | None = None) -> None:
        self._extra_dirs = tuple(extra_dirs or ())

    def run(self, ctx: PreflightContext) -> CheckResult:
        # Compared as paths, not strings: a string prefix reads
        # /Users/example-work as living inside /Users/example (sca-2wt).
        home = Path.home().resolve()
        targets = [ctx.profile.work_root, *(ctx.profile.work_root / d for d in self._extra_dirs)]
        bad: list[str] = []
        for target in targets:
            if not target.is_dir():
                bad.append(f"작업 자리가 없다 : {target}")
            elif target.resolve().is_relative_to(home):
                bad.append(f"작업 자리가 홈 안에 있다 : {target}")
            elif (target / "CLAUDE.md").exists():
                bad.append(f"작업 자리에 CLAUDE.md 가 있다 : {target / 'CLAUDE.md'}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="작업 자리 점검 통과")


class EngineBinaryCheck(PreflightCheck):
    """Checks that the primary/fallback engine binaries can be resolved now.

    An absolute path is checked for existence; a bare name is resolved via
    `shutil.which`.
    """

    name: ClassVar[str] = "engine_binary"

    def run(self, ctx: PreflightContext) -> CheckResult:
        bad: list[str] = []
        specs = [("1차", ctx.profile.primary_engine)]
        if ctx.profile.fallback_engine:
            specs.append(("2차", ctx.profile.fallback_engine))
        for label, spec in specs:
            if spec.binary.is_absolute():
                if not spec.binary.exists():
                    bad.append(f"{label} 엔진 실행 파일이 없다 : {spec.binary}")
            elif shutil.which(str(spec.binary)) is None:
                bad.append(f"{label} 엔진 실행 파일을 PATH 에서 못 찾는다 : {spec.binary}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="엔진 실행 파일 점검 통과")


#: Slack user IDs start with U (person) or W (Enterprise Grid person).
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")


class ChannelUserToolsCheck(PreflightCheck):
    """Warns about a per-user tool table nobody would notice is broken.

    A mistyped key grants nothing and logs nothing — the person just never
    gets the tool. Tool names themselves aren't checked: what counts as a
    valid one differs per engine (sca-34o).
    """

    name: ClassVar[str] = "channel_user_tools"

    def run(self, ctx: PreflightContext) -> CheckResult:
        bad: list[str] = []
        for config in ChannelRegistry(ctx.profile.paths.channels).all().values():
            for user, tools in sorted(config.user_tools.items()):
                if not _USER_ID_RE.match(user):
                    bad.append(f"{config.name} user_tools 키가 사용자 ID 형식이 아니다 : {user}")
                elif not tools:
                    bad.append(f"{config.name} user_tools 의 도구 목록이 비어 있다 : {user}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad), fatal=False)
        return CheckResult(ok=True, detail="채널 사용자별 도구 표 점검 통과")


class UsageCheckCommandCheck(PreflightCheck):
    """Checks the operator's usage check command can actually be run.

    A wrong path only shows up as an hourly warning in the log, which nobody
    reads. `fatal=False` — the bot answers fine without this check (sca-3p7).
    """

    name: ClassVar[str] = "usage_check_command"

    def run(self, ctx: PreflightContext) -> CheckResult:
        command = ctx.profile.usage_check_command
        if not command:
            return CheckResult(ok=True, detail="사용량 확인 명령이 없다")
        binary = Path(command[0]).expanduser()
        if binary.is_absolute():
            if not binary.exists():
                return CheckResult(
                    ok=False, detail=f"사용량 확인 실행 파일이 없다 : {binary}", fatal=False
                )
            if not os.access(binary, os.X_OK):
                return CheckResult(
                    ok=False, detail=f"사용량 확인 실행 권한이 없다 : {binary}", fatal=False
                )
        elif shutil.which(str(binary)) is None:
            return CheckResult(
                ok=False,
                detail=f"사용량 확인 실행 파일을 PATH 에서 못 찾는다 : {binary}",
                fatal=False,
            )
        return CheckResult(ok=True, detail="사용량 확인 명령 점검 통과")


class ProfileUnknownKeysCheck(PreflightCheck):
    """Warns about profile keys the code never reads.

    `from_dict` takes the keys it knows and drops the rest, and
    `RuntimeSettings.override` ignores unknown `settings` keys the same way.
    A key that is one character off therefore boots fine with its feature
    off, which looks exactly like not configuring it at all -- sca-39c was
    that shape. `fatal=False`: a typo is not a reason to keep the bot down
    (sca-4dr).
    """

    name: ClassVar[str] = "profile_unknown_keys"

    def run(self, ctx: PreflightContext) -> CheckResult:
        source = ctx.profile.source_file
        if source is None:
            return CheckResult(ok=True, detail="파일에서 읽은 프로필이 아니다")
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # The profile in hand parsed once already, so this is a file that
            # changed or went away since. Boot is not the place to judge it.
            return CheckResult(ok=True, detail=f"프로필 원본을 다시 읽지 못했다 : {source}")
        if not isinstance(data, dict):
            return CheckResult(ok=True, detail="프로필 원본이 객체가 아니다")
        unknown = [key for key in sorted(data) if key not in PROFILE_KNOWN_KEYS]
        settings_block = data.get("settings")
        if isinstance(settings_block, dict):
            known_settings = set(RuntimeSettings.__dataclass_fields__)
            unknown += [
                f"settings.{key}" for key in sorted(settings_block) if key not in known_settings
            ]
        if unknown:
            return CheckResult(
                ok=False,
                detail="프로필에서 코드가 안 읽는 키가 있다 : " + ", ".join(unknown),
                fatal=False,
            )
        return CheckResult(ok=True, detail="프로필 키 점검 통과")


class OwnerSettingsInertCheck(PreflightCheck):
    """Warns once at boot about channel settings that are silently ignored for the owner.

    `EngineSpec.model_for_owner()` and `OWNER_EFFORT_MIN` always win over a
    channel's `model`/`effort` for the owner, so a value in channels.json can
    look active while never actually applying. `fatal=False` — this only
    prevents misreading the config, it doesn't block boot.

    Engine-agnostic on purpose: AccessPolicy overrides the owner's model and
    effort whatever the engine is, so an engine exception here would only hide
    the same dead setting for that bot.
    """

    name: ClassVar[str] = "owner_settings_inert"

    def run(self, ctx: PreflightContext) -> CheckResult:
        model_owner = ctx.profile.primary_engine.model_for_owner()
        registry = ChannelRegistry(ctx.profile.paths.channels)
        dead: list[str] = []
        for config in registry.all().values():
            if config.model and config.model != model_owner:
                dead.append(f"{config.name} model={config.model}")
            if config.effort in EFFORT_LEVELS and (
                EFFORT_LEVELS.index(config.effort) < EFFORT_LEVELS.index(OWNER_EFFORT_MIN)
            ):
                dead.append(f"{config.name} effort={config.effort}")
        if dead:
            return CheckResult(ok=False, detail="; ".join(dead), fatal=False)
        return CheckResult(ok=True, detail="소유자에게 적용되는 채널 설정 점검 통과")



class ChannelSettingsReadableCheck(PreflightCheck):
    """Warns at boot about channel values the read path could not read.

    The read path parses every channel on every request, so it falls back to
    the safe side for a bad value rather than stopping unrelated channels
    (sca-xe0). That leaves only a log line, which nobody opens. This is the
    one place the operator is told the file says something the code ignores.

    fatal=False -- the affected channel already runs on the strict fallback,
    and taking the bot down for one typo is what this issue removed.
    """

    name: ClassVar[str] = "channel_settings_readable"

    def run(self, ctx: PreflightContext) -> CheckResult:
        path = ctx.profile.paths.channels
        if not path.exists():
            return CheckResult(ok=True, detail="채널 설정 파일이 없다")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Reading the file itself is another check's job.
            return CheckResult(ok=True, detail=f"채널 설정 원본을 다시 읽지 못했다 : {path}")
        if not isinstance(raw, dict):
            return CheckResult(ok=True, detail="채널 설정의 최상위가 객체가 아니다")
        rejected: list[str] = []
        for channel_id, data in raw.items():
            if not isinstance(data, Mapping):
                continue
            rejected.extend(ChannelConfig.lenient_from_dict(str(channel_id), data)[1])
        if rejected:
            return CheckResult(ok=False, detail="; ".join(rejected), fatal=False)
        return CheckResult(ok=True, detail="채널 설정 값 점검 통과")


class ToolAllowlistEnforcementCheck(PreflightCheck):
    """Warns when the configured engine cannot enforce a tool allowlist.

    The caller builds the same allowed_tools list for every engine, but only
    claude passes it to the CLI as an allowlist. Codex takes a sandbox mode --
    a different axis, not a weaker allowlist -- and agy enforces nothing. The
    fact belongs at configuration time: recording it per request would repeat
    the same sentence on every request that bot ever serves (sca-dyb.11).

    A claude profile with an MCP server attached is reported too: the
    allowlist drops to server granularity for every request on it (sca-qqtl),
    and calling that "enforced" states more than the bot can hold.

    fatal=False -- running without an allowlist is a choice an operator may
    have made knowingly, and blocking boot would take the bot down for it.
    An engine this package does not ship (a plugin's) is not judged: reading
    "not registered here" as "enforces nothing" would be a false warning.
    """

    name: ClassVar[str] = "tool_allowlist_enforcement"

    def run(self, ctx: PreflightContext) -> CheckResult:
        engines = default_registry()
        weak: list[str] = []
        specs = [("1차", ctx.profile.primary_engine)]
        if ctx.profile.fallback_engine:
            specs.append(("2차", ctx.profile.fallback_engine))
        for label, spec in specs:
            engine_class = engines.engine_class(spec.type)
            if engine_class is None:
                continue
            actual = engine_class.configured_capabilities(
                spec, ctx.profile.mcp_servers
            ).tool_restriction
            if actual is ToolRestriction.EXACT_ALLOWLIST:
                continue
            if actual is ToolRestriction.SERVER_SCOPED_ALLOWLIST:
                # Named apart from the weaker engines: the allowlist does hold
                # for the built-in tools here, and what an operator would act on
                # is the MCP server, not the engine choice.
                weak.append(
                    f"{label} {spec.type} : MCP 서버가 붙어 있어 도구 제한이 {actual} 로"
                    " 내려간다 — 허용목록이 이름한 도구를 가진 서버는 통째로 열린다"
                )
                continue
            weak.append(f"{label} {spec.type} : 도구 제한이 {actual} 라 허용목록이 그대로 적용되지 않는다")
        if weak:
            return CheckResult(ok=False, detail="; ".join(weak), fatal=False)
        return CheckResult(ok=True, detail="도구 허용목록 강제 점검 통과")


class McpCredentialCheck(PreflightCheck):
    """Checks that every ${env:...}/${file:...} in an MCP server resolves now.

    Resolution happens when the engine command is built, so an unresolved
    reference would surface as one failed request at a time rather than as
    a configuration problem (sca-dn4).
    """

    name: ClassVar[str] = "mcp_credentials"

    def run(self, ctx: PreflightContext) -> CheckResult:
        bad: list[str] = []
        for server in ctx.profile.mcp_servers.values():
            for resolve in (server.resolved_env, server.resolved_headers):
                try:
                    resolve()
                except ConfigError as exc:
                    bad.append(str(exc))
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="MCP 자격 표기 점검 통과")


class ProfilePermissionCheck(PreflightCheck):
    """Warns when a profile carrying MCP credentials is readable past its owner.

    Tokens are rejected at load time, so a plain profile has nothing to
    hide -- warning on those would put a permanent warning on every bot
    whose profile lives in a repository. An MCP server's `env`/`headers`
    is the case the load check cannot see into, and only that one is
    judged here. `fatal=False`: a chmod fixes it, taking the bot down does not.
    """

    name: ClassVar[str] = "profile_permissions"

    def run(self, ctx: PreflightContext) -> CheckResult:
        path = ctx.profile.source_file
        if path is None or not path.is_file():
            return CheckResult(ok=True, detail="읽어 온 프로필 파일이 없다")
        if not any(spec.env or spec.headers for spec in ctx.profile.mcp_servers.values()):
            return CheckResult(ok=True, detail="프로필에 가릴 값이 없다")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & _SHARED_BITS:
            return CheckResult(
                ok=False,
                detail=f"프로필을 소유자 밖에서도 읽을 수 있다 : {path} ({mode:o}) - chmod 600 으로 바꾼다",
                fatal=False,
            )
        return CheckResult(ok=True, detail="프로필 파일 권한 점검 통과")


class McpServerCheck(PreflightCheck):
    """Checks that MCP servers can boot right now.

    After sleep/wake, paths and binaries aren't guaranteed to still be
    valid, so this catches a tool going silently missing before it can
    answer with one quietly gone.
    """

    name: ClassVar[str] = "mcp_server"

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not ctx.profile.mcp_servers:
            return CheckResult(ok=True, detail="선언된 MCP 서버 없음", fatal=False)
        broken = broken_mcp_servers(ctx.profile.mcp_servers)
        if broken:
            return CheckResult(ok=False, detail="; ".join(broken))
        return CheckResult(ok=True, detail="MCP 기동 점검 통과")


class EngineSettingsCheck(PreflightCheck):
    """Checks the bot's own claude settings load before a request needs them.

    claude reads these through --settings, which build_command assembles per
    request. A broken file therefore passes boot and fails every request
    after it, and the requester only sees a failure reaction. The original
    exits at startup when its settings file is missing (bot.py:7133).

    Absent is a warning, not fatal: a freshly installed bot has no
    operational files yet. Present but unreadable is fatal -- the operator
    wrote a deny list and it is not reaching the engine.
    """

    name: ClassVar[str] = "engine_settings"

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not self._uses_claude(ctx.profile):
            return CheckResult(ok=True, detail="claude 를 안 쓰는 프로필")
        paths = ctx.profile.paths
        common = paths.engine_settings("claude")
        try:
            # Merged rather than per file: the deny baseline lives on the
            # document a turn runs under, so reading the files one by one
            # would leave it to be taken at that level's first request
            # instead of here (sca-e34p).
            for level in TrustLevel:
                load_merged_settings(common, paths.engine_settings(f"claude.{level.name.lower()}"))
        except ConfigError as e:
            return CheckResult(ok=False, detail=str(e))
        if not paths.engine_settings("claude").exists():
            return CheckResult(
                ok=False,
                detail=f"봇 전용 settings 가 없습니다 : {paths.engine_settings('claude')}",
                fatal=False,
            )
        return CheckResult(ok=True, detail="봇 전용 settings 점검 통과")

    @staticmethod
    def _uses_claude(profile: Profile) -> bool:
        specs = [profile.primary_engine]
        if profile.fallback_engine:
            specs.append(profile.fallback_engine)
        return any(spec.type == "claude" for spec in specs)


class EngineHomeCredentialCheck(PreflightCheck):
    """Checks that a per-bot engine home actually holds that engine's login.

    Splitting CODEX_HOME/HOME per bot works, but a fresh directory has no
    login in it and the engine then fails only when a request arrives —
    the bot looks alive and answers nothing. Which files an engine needs
    is declared on its environment policy (sca-kos.6).
    """

    name: ClassVar[str] = "engine_home_credentials"

    def run(self, ctx: PreflightContext) -> CheckResult:
        bad: list[str] = []
        specs = [("1차", ctx.profile.primary_engine)]
        if ctx.profile.fallback_engine:
            specs.append(("2차", ctx.profile.fallback_engine))
        for label, spec in specs:
            if spec.home_dir is None:
                continue
            policy = registry.policy_class(spec.type)
            if policy is None:
                # Engine registration is EngineBinaryCheck's concern; an
                # unknown engine must not turn into a credential failure.
                continue
            try:
                # Built, not just read: an engine that has no per-bot home
                # concept rejects home_dir here, and reading CREDENTIAL_FILES
                # alone would call that profile bootable (코덱스 리뷰).
                policy(profile_name=ctx.profile.name, home_dir=spec.home_dir)
            except ConfigError as exc:
                bad.append(f"{label} 엔진 : {exc}")
                continue
            bad.extend(self._missing(label, spec.home_dir, policy.CREDENTIAL_FILES))
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="엔진 홈 자격 점검 통과")

    @staticmethod
    def _missing(label: str, home_dir: Path, credentials: tuple[tuple[str, str], ...]) -> list[str]:
        bad: list[str] = []
        for relative, source in credentials:
            path = home_dir / relative
            if not path.is_file():
                bad.append(f"{label} 엔진 홈에 자격 파일이 없다 : {path} (~/{source} 를 복사한다)")
                continue
            mode = path.stat().st_mode
            if mode & _SHARED_BITS:
                bad.append(f"{label} 엔진 자격 파일의 권한을 600 으로 바꿔라 : {path}")
            elif not mode & stat.S_IRUSR:
                # Nobody-can-read is as broken as everybody-can-read: the
                # engine fails at request time either way (코덱스 리뷰).
                bad.append(f"{label} 엔진 자격 파일을 소유자가 읽을 수 없다 : {path}")
        return bad


# ---------------------------------------------------------------------------
# 마메치 check.py 기동 점검의 이식. 아래 9종은 그 소스를 읽고 관측 동작에
# 맞춰 일반화한 것이다 — 조직 특화 값(어떤 낱말이 어떤 도구를 약속하는지,
# 어떤 서버 이름이 같은 시스템을 가리키는지, 등급 체계 자체)은 생성자
# 주입점으로 남겨 조직이 채우도록 하고, 공통 골격에는 박지 않는다.
# ---------------------------------------------------------------------------


def _undefined_name_lines(tree: ast.AST) -> dict[str, int]:
    """Names referenced (``Load``) that nothing in the module ever defines.

    Mirrors the two-pass walk the original ``check.py`` does over ``bot.py``:
    collect every name assignment, def, import, argument, except-binding and
    comprehension target first, then flag any ``Load`` not in that set. A
    plain compile only catches a bad name once execution reaches that line;
    this catches it before the process starts (코덱스 리뷰 — 원본 참고).
    """
    import builtins as _builtins

    defined = set(dir(_builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                defined.update(a.arg for a in getattr(args, "args", []))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.comprehension):
            for inner in ast.walk(node.target):
                if isinstance(inner, ast.Name):
                    defined.add(inner.id)

    missing: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined:
            missing.setdefault(node.id, node.lineno)
    return missing


#: Modules the long-running commands import only after the gate has passed.
#: `cli._default_application` pulls in `core.application` lazily. That module
#: does not import the Slack SDK at module level: `Application.from_profile`
#: imports `slack_sdk.WebClient` and the ingress connector imports
#: `slack_sdk.socket_mode` inside the call, so both are listed explicitly.
CORE_BOOT_MODULES: tuple[str, ...] = (
    "slack_cli_agent.core.application",
    "slack_sdk",
    "slack_sdk.socket_mode",
    "slack_sdk.socket_mode.response",
)


class CoreImportCheck(PreflightCheck):
    """Imports the core modules boot needs, before boot.

    worker and ingress import `core.application` only after the suite reports
    기동 가능. A broken core (a syntax error, a missing dependency, an
    exception at module level) therefore passed every check and then died with
    an ImportError, exit code 1, which launchd answers with a restart every
    10 seconds. Restarting does not fix a broken install, so the failure has
    to surface here, where a fatal result becomes EX_CONFIG (78) and run.sh
    leaves a block marker.
    """

    name: ClassVar[str] = "core_import"

    def __init__(self, modules: Sequence[str] = CORE_BOOT_MODULES) -> None:
        self._modules = tuple(modules)

    def run(self, ctx: PreflightContext) -> CheckResult:
        bad: list[str] = []
        for module in self._modules:
            try:
                importlib.import_module(module)
            except Exception as exc:  # noqa: BLE001 - any module-level failure blocks boot
                bad.append(f"{module} : {exc!r}")
        if bad:
            return CheckResult(ok=False, detail="코어 모듈 import 실패 - " + "; ".join(bad))
        return CheckResult(ok=True, detail="코어 모듈 import 점검 통과")


class CompileCheck(PreflightCheck):
    """`py_compile`s every configured plugin module before boot.

    Parity with the original `apply.sh`/`restart.sh` gate `python -m
    py_compile bot.py`, which refused to restart the bot on a syntax error.
    The org-written code imported at boot here is the set of `profile.plugins`
    modules, not one monolithic file, so each is compiled in turn.

    This sits deliberately *before* `PluginAstCheck` in the suite — the same
    order `apply.sh` ran `py_compile` and then `check.py`. It matters: an
    `ast.parse` of broken source raises `SyntaxError`, and PluginAstCheck
    treats that as "can't read, not my job" and passes (its concern is
    undefined names, not whether the file compiles). So without this check a
    plugin that does not even parse sails through preflight and blows up at
    import — long after boot has reported 기동 가능.

    Degrades to a pass when a module can't be found or its source can't be
    read, for the same reason PluginAstCheck does: locating and loading the
    module is `PluginLoader`'s job and must not be reported twice. A syntax
    error, by contrast, is exactly what this check exists to catch.
    """

    name: ClassVar[str] = "plugin_compile"

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not ctx.profile.plugins:
            return CheckResult(ok=True, detail="컴파일할 플러그인이 없다")
        bad: list[str] = []
        for entry in ctx.profile.plugins:
            module_path = entry.partition(":")[0]
            try:
                spec = importlib.util.find_spec(module_path)
            except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
                continue
            if spec is None or not spec.origin or not spec.origin.endswith(".py"):
                continue
            try:
                source = Path(spec.origin).read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                compile(source, spec.origin, "exec")
            except SyntaxError as exc:
                where = f"{exc.lineno}행" if exc.lineno else "위치 미상"
                bad.append(f"{module_path} : 구문 오류 - {exc.msg} ({where})")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="플러그인 컴파일 점검 통과")


class PluginAstCheck(PreflightCheck):
    """Checks every configured plugin module for a Load of a name nothing defines.

    A syntax error is caught at compile time; an undefined name is not — it
    passes `python -m py_compile` and blows up the first time the branch that
    references it actually runs, which may be long after boot (sca parity
    with the original `check.py`'s scan of `bot.py`). Here the equivalent
    org-written code loaded at boot is a plugin module, not a single
    monolithic file, so each `profile.plugins` entry is scanned in turn.

    Degrades to a pass when a module can't be found or its source can't be
    read: locating the module is `PluginLoader`'s job, and this check must
    not duplicate that failure under a different name.
    """

    name: ClassVar[str] = "plugin_ast_names"

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not ctx.profile.plugins:
            return CheckResult(ok=True, detail="점검할 플러그인이 없다")
        bad: list[str] = []
        for entry in ctx.profile.plugins:
            module_path = entry.partition(":")[0]
            try:
                spec = importlib.util.find_spec(module_path)
            except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
                continue
            if spec is None or not spec.origin or not spec.origin.endswith(".py"):
                continue
            try:
                source = Path(spec.origin).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=spec.origin)
            except (OSError, SyntaxError):
                continue
            missing = _undefined_name_lines(tree)
            if missing:
                detail = ", ".join(
                    f"{name} ({lineno}행)"
                    for name, lineno in sorted(missing.items(), key=lambda kv: kv[1])
                )
                bad.append(f"{module_path} : 정의되지 않은 이름 참조 - {detail}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="플러그인 이름 참조 점검 통과")


class SkillSymlinkCheck(PreflightCheck):
    """Checks that a skill symlink under `paths.skill_files` actually resolves.

    A skill granted through the engine's permission system but missing its
    directory leaves the tool with nowhere to read its instructions from —
    the skill silently has nothing in it. Only symlinks are inspected: a
    copied-in directory isn't the failure mode this guards against, and
    scanning it too would turn every ordinary install into a warning.
    """

    name: ClassVar[str] = "skill_symlinks"

    def run(self, ctx: PreflightContext) -> CheckResult:
        root = ctx.profile.paths.skill_files
        if not root.is_dir():
            return CheckResult(ok=True, detail="스킬 설치 자리가 없다")
        bad: list[str] = []
        seen_any = False
        for entry in sorted(root.iterdir()):
            if not entry.is_symlink():
                continue
            seen_any = True
            if not entry.exists():
                bad.append(f"{entry.name} : 심링크가 깨졌다 (대상 없음)")
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                bad.append(f"{entry.name} : SKILL.md 가 없다 : {skill_md}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        if not seen_any:
            return CheckResult(ok=True, detail="점검할 스킬 심링크가 없다")
        return CheckResult(ok=True, detail="스킬 심링크 점검 통과")


class CodexConfigLoadCheck(PreflightCheck):
    """Checks that a configured Codex home's config actually loads.

    `config.toml` existing proves nothing about whether Codex can parse it
    with the arguments this bot would actually pass — a bad MCP server
    block only shows up when something calls `codex mcp list` against it.
    Left unchecked, the first real request is where that surfaces, as a
    silently missing tool rather than a boot failure (parity with the
    original `check_codex_config_loads()`, minus the org-specific extra
    CLI flags it passed — those are the caller's own arguments to add via
    a subclass or a plugin-supplied check, not this package's concern).
    """

    name: ClassVar[str] = "codex_config_load"

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def run(self, ctx: PreflightContext) -> CheckResult:
        specs = [ctx.profile.primary_engine]
        if ctx.profile.fallback_engine:
            specs.append(ctx.profile.fallback_engine)
        targets = [spec for spec in specs if spec.type == "codex" and spec.home_dir is not None]
        if not targets:
            return CheckResult(ok=True, detail="봇별 홈을 가른 codex 엔진이 없다")
        bad: list[str] = []
        for spec in targets:
            home_dir = spec.home_dir
            assert home_dir is not None  # narrowed by the filter above
            config = home_dir / "config.toml"
            if not config.exists():
                bad.append(f"Codex 설정 없음 : {config}")
                continue
            if spec.binary.is_absolute():
                if not spec.binary.exists():
                    bad.append(f"codex 실행 파일이 없다 : {spec.binary}")
                    continue
                exe = str(spec.binary)
            else:
                found = shutil.which(str(spec.binary))
                if found is None:
                    bad.append(f"codex 실행 파일을 PATH 에서 못 찾는다 : {spec.binary}")
                    continue
                exe = found
            try:
                done = subprocess.run(
                    [exe, "mcp", "list"],
                    env={**os.environ, "CODEX_HOME": str(home_dir)},
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired:
                bad.append(f"Codex 설정 읽기가 {self._timeout}초 안에 끝나지 않았다 : {config}")
                continue
            except OSError as exc:
                bad.append(f"Codex 설정을 실행하지 못했다 : {exc}")
                continue
            if done.returncode != 0:
                tail = (done.stderr or done.stdout).strip().replace("\n", " ")[-300:]
                bad.append(f"Codex 설정을 읽지 못한다 ({config}) : {tail}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="Codex 설정 로드 점검 통과")


class McpScriptOwnershipCheck(PreflightCheck):
    """Checks that a local MCP script doesn't hardcode another bot's state path.

    An MCP server runs as its own process, separate from this one, so it
    never receives the profile object a bot-side check reads from. Any path
    it needs is written straight into the script, and copying one bot's
    server to start another's carries that path along silently -- the clone
    posts to Slack, or returns data, under the original bot's identity
    (parity with the original `check_mcp_ownership()`; sca found this exact
    shape costing six days of undetected cross-posting).

    Comparison needs another profile to compare against, which only exists
    once this profile was itself loaded from a file -- a profile built in
    memory (`Profile.from_dict`, every test) has nothing on disk to find
    siblings next to, and the check correctly has nothing to say then.
    """

    name: ClassVar[str] = "mcp_script_ownership"

    def run(self, ctx: PreflightContext) -> CheckResult:
        scripts = [
            Path(spec.command)
            for spec in ctx.profile.mcp_servers.values()
            if not spec.is_remote and spec.command and Path(spec.command).suffix == ".py"
            and Path(spec.command).is_file()
        ]
        if not scripts:
            return CheckResult(ok=True, detail="점검할 로컬 MCP 스크립트가 없다")
        others = self._other_state_dir_names(ctx.profile)
        if not others:
            return CheckResult(ok=True, detail="비교할 다른 프로필이 없다")
        bad: list[str] = []
        for script in scripts:
            text = script.read_text(encoding="utf-8", errors="replace")
            for other_name, state_name in others:
                if state_name and state_name in text:
                    bad.append(
                        f"{script.name} 이 {other_name} 의 자리 {state_name} 를 참조한다. "
                        "그 봇의 자격으로 나간다"
                    )
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="MCP 스크립트 명의 점검 통과")

    @staticmethod
    def _other_state_dir_names(profile: Profile) -> list[tuple[str, str]]:
        source = profile.source_file
        if source is None:
            return []
        pairs: list[tuple[str, str]] = []
        for path in sorted(source.parent.glob("*.json")):
            if path == source or path.name.endswith(EXAMPLE_SUFFIX):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, Mapping):
                continue
            other_name = str(data.get("name") or path.stem)
            if other_name == profile.name:
                continue
            state_dir = data.get("state_dir")
            state_name = Path(str(state_dir)).expanduser().name if state_dir else f".{other_name}"
            pairs.append((other_name, state_name))
        return pairs


class PromptToolConsistencyCheck(PreflightCheck):
    """Checks that a feature a prompt file promises has its tool in the
    common tool list -- the one every requester gets, not just the owner's.

    A prompt is read by every principal, so a feature it claims to perform
    ("I can search the web") has to work for every principal too. If the
    tool that feature needs only lives in an owner-only extra list, the
    first non-owner who asks gets a refusal the bot then has to explain,
    which risks explaining internal configuration to them (parity with the
    original `check_prompt_tools()`). Which words promise which tools is
    org-specific persona writing, not something this package should guess
    at, so both the promise table and what counts as "the common tool list"
    are constructor arguments -- empty by default, meaning nothing is
    checked until the caller supplies them.
    """

    name: ClassVar[str] = "prompt_tool_consistency"

    def __init__(
        self,
        promises: Mapping[str, Sequence[str]] | None = None,
        base_tools: Sequence[str] = (),
    ) -> None:
        self._promises = {word: tuple(tools) for word, tools in (promises or {}).items()}
        self._base_tools = tuple(base_tools)

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not self._promises:
            return CheckResult(ok=True, detail="점검할 프롬프트-도구 약속이 없다")
        prompts_dir = ctx.profile.paths.prompts
        if not prompts_dir.is_dir():
            return CheckResult(ok=True, detail="프롬프트 자리가 없다")
        bad: list[str] = []
        for path in sorted(prompts_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for word, tools in self._promises.items():
                if word not in text:
                    continue
                for tool in tools:
                    if tool in self._base_tools:
                        continue
                    bad.append(
                        f"{path.name} : '{word}' 를 약속하는데 {tool} 이 공통 도구 목록에 없다"
                    )
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="프롬프트 약속 대 도구 점검 통과")


class DuplicateMcpSystemCheck(PreflightCheck):
    """Checks that no more than one enabled MCP server points at the same
    backend system.

    When two servers answer for the same system, which one a request lands
    on is whichever the model happens to pick, and the two don't always
    return the same shape of answer -- a field present on one is silently
    missing from the other's response (parity with the original
    `check_duplicate_systems()`, which hardcoded `jira`/`confluence`). Which
    server names refer to the same system is org knowledge this package has
    no way to infer from a bare name, so the grouping table is a
    constructor argument -- empty by default, meaning nothing is grouped
    until the caller supplies it.
    """

    name: ClassVar[str] = "duplicate_mcp_systems"

    def __init__(self, groups: Mapping[str, Sequence[str]] | None = None) -> None:
        self._groups = {system: tuple(patterns) for system, patterns in (groups or {}).items()}

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not self._groups:
            return CheckResult(ok=True, detail="점검할 중복 시스템 그룹이 없다")
        enabled = [name for name, spec in ctx.profile.mcp_servers.items() if not spec.disabled]
        bad: list[str] = []
        for system, patterns in self._groups.items():
            hits = sorted({
                name for name in enabled
                for pattern in patterns
                if pattern.lower() in name.lower()
            })
            if len(hits) > 1:
                bad.append(f"{system} 조회 MCP 서버가 {hits} 로 둘 이상 열려 있다. 하나만 남긴다")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="MCP 서버 중복 점검 통과")


class FullDiskAccessCheck(PreflightCheck):
    """Warns, without blocking boot, when the engine's own process can't
    read past macOS's TCC-guarded folders, and tracks the binary path that
    was checked.

    A turn that touches a protected folder makes macOS pop a "python3.13
    wants to access another app's data" dialog, and that read then blocks
    until a person at the keyboard clicks it -- on a headless bot, nobody
    ever will (parity with the original `check_fda()`). Granting Full Disk
    Access ahead of time avoids the dialog, but the grant is tied to one
    binary's exact path, and `uv`-managed interpreters move when `uv`
    re-provisions one, silently invalidating a grant that still looks fine
    in System Settings. This never blocks boot -- it has nothing to offer
    but the path and a warning.
    """

    name: ClassVar[str] = "full_disk_access"

    def __init__(
        self,
        *,
        binary: Path | None = None,
        tcc_db: Path | None = None,
        platform_name: str | None = None,
        now: str | None = None,
    ) -> None:
        self._binary = binary or Path(os.path.realpath(sys.executable))
        self._tcc_db = tcc_db or (Path.home() / "Library/Application Support/com.apple.TCC/TCC.db")
        self._platform = platform_name if platform_name is not None else sys.platform
        self._now = now

    def run(self, ctx: PreflightContext) -> CheckResult:
        if self._platform != "darwin":
            return CheckResult(ok=True, detail="macOS 가 아니라 해당 없음", fatal=False)
        fda: bool | None
        try:
            with open(self._tcc_db, "rb") as f:
                f.read(16)
            fda = True
        except PermissionError:
            fda = False
        except OSError:
            fda = None

        record_path = ctx.profile.state_dir / "fda_binary.json"
        try:
            prev = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}

        notes: list[str] = []
        if prev.get("path") and prev["path"] != str(self._binary):
            notes.append(
                f"봇 python 바이너리가 바뀌었다 : {prev['path']} → {self._binary}. "
                "전체 디스크 접근 권한을 새 경로에 다시 줘야 한다"
            )
        if fda is False:
            notes.append(f"전체 디스크 접근 권한 없음 : {self._binary}. 보호 폴더 접근 시 허용 창이 뜰 수 있다")

        try:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(
                    {
                        "path": str(self._binary),
                        "fda": fda,
                        "checked": self._now or time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        if notes:
            return CheckResult(ok=False, detail="; ".join(notes), fatal=False)
        status = "있음" if fda else "없음" if fda is False else "판정 불가"
        return CheckResult(ok=True, detail=f"전체 디스크 접근 점검 : {self._binary} → {status}")


class ScriptCapabilityGradeCheck(PreflightCheck):
    """Checks that every top-level script in a directory carries a tier in
    a capabilities registry, and that the registry doesn't name a tier
    outside the allowed set or a script that isn't actually there.

    A shell tool with no declared tier is reachable by whatever default the
    permission system falls back to, which on more than one occasion was
    wide open rather than closed -- a new operational script four different
    times reached everyone's turn before anyone meant it to (parity with the
    original `check_capabilities()`; the cross-check against a specific
    allowlist constant it also did is org-specific wiring left for the
    caller's own check). The directory and registry file are constructor
    arguments; either being unset means there's nothing declared yet to
    check against, not a failure.
    """

    name: ClassVar[str] = "script_capability_grade"

    DEFAULT_VALID_TIERS: ClassVar[tuple[str, ...]] = ("all", "owner", "internal")

    def __init__(
        self,
        scripts_dir: Path | None = None,
        capabilities_file: Path | None = None,
        valid_tiers: Sequence[str] | None = None,
    ) -> None:
        self._scripts_dir = scripts_dir
        self._capabilities_file = capabilities_file
        self._valid_tiers = tuple(valid_tiers) if valid_tiers else self.DEFAULT_VALID_TIERS

    def run(self, ctx: PreflightContext) -> CheckResult:
        if self._scripts_dir is None or self._capabilities_file is None:
            return CheckResult(ok=True, detail="점검할 스크립트 등급 설정이 없다")
        if not self._capabilities_file.is_file():
            return CheckResult(ok=False, detail=f"등급 등록부가 없다 : {self._capabilities_file}")
        try:
            raw = json.loads(self._capabilities_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return CheckResult(ok=False, detail=f"등급 등록부를 읽지 못했다 : {exc}")
        caps = raw.get("capabilities", {}) if isinstance(raw, Mapping) else {}

        bad: list[str] = []
        if self._scripts_dir.is_dir():
            for path in sorted(self._scripts_dir.iterdir()):
                if path.suffix not in (".sh", ".py") or ".bak" in path.name:
                    continue
                if path.name.startswith("test_"):
                    continue
                if path.name not in caps:
                    bad.append(f"{path.name} 의 등급이 등록부에 없다")
        for script_name, cap in caps.items():
            if not isinstance(cap, Mapping):
                bad.append(f"{script_name} 의 등록 내용이 올바르지 않다")
                continue
            tier = cap.get("tier")
            if tier not in self._valid_tiers:
                bad.append(f"{script_name} 의 등급이 올바르지 않다 : {tier}")
            elif not cap.get("external") and not (self._scripts_dir / script_name).exists():
                bad.append(f"등록부에 있는 {script_name} 이 자리에 없다 : {self._scripts_dir / script_name}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="스크립트 등급 점검 통과")


class SlackApiRegistrationCheck(PreflightCheck):
    """Checks that every raw `api_call("...")` name found in the given
    source files is registered, documented, and never one marked as
    banned.

    Calling the Slack Web API by a hand-typed method name skips whatever
    validation the higher-level client would have done -- it passes syntax
    and name-reference checks just as easily misspelled as correct, and a
    method that silently doesn't work in the context it's called from (a
    thread, say) only shows up once nobody is watching the error logs
    (parity with the original `check_slack_api()`). Which files make these
    calls and where the registry lives are constructor arguments; with
    neither given there's nothing to scan yet, not a failure.
    """

    name: ClassVar[str] = "slack_api_registration"

    _CALL_RE = re.compile(r'api_call\(\s*"([A-Za-z0-9_.]+)"')

    def __init__(
        self,
        source_files: Sequence[Path] = (),
        registry_file: Path | None = None,
    ) -> None:
        self._source_files = tuple(source_files)
        self._registry_file = registry_file

    def run(self, ctx: PreflightContext) -> CheckResult:
        if not self._source_files and self._registry_file is None:
            return CheckResult(ok=True, detail="점검할 슬랙 API 등록부가 없다")
        if self._registry_file is None or not self._registry_file.is_file():
            return CheckResult(
                ok=False, detail=f"등록부가 없다 : {self._registry_file}"
            )
        try:
            registry_data = json.loads(self._registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return CheckResult(ok=False, detail=f"등록부를 읽지 못했다 : {exc}")
        banned = registry_data.get("_banned") or registry_data.get("_쓰지_않는다") or {}
        known = {
            method: item
            for method, item in registry_data.items()
            if not str(method).startswith("_")
        }

        called: set[str] = set()
        for path in self._source_files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            called.update(self._CALL_RE.findall(text))

        bad: list[str] = []
        for method in sorted(called):
            if method in banned:
                reason = banned[method].get("이유", "") if isinstance(banned[method], Mapping) else ""
                bad.append(f"쓰지 않기로 한 API 를 부른다 : {method} ({reason})")
            elif method not in known:
                bad.append(f"등록부에 없는 API 를 부른다 : {method}")
        for method, item in known.items():
            if not isinstance(item, Mapping) or not item.get("doc") or not item.get("verified"):
                bad.append(f"문서 주소나 실측 날짜가 비었다 : {method}")

        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="슬랙 API 등록 점검 통과")
