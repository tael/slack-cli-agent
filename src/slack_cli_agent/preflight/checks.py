"""Concrete preflight checks.

McpServerCheck also checks the shebang interpreter because a LaunchAgent's
narrow PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) can make a `#!/usr/bin/env
node` shebang silently fail, dropping the tool without any error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import ClassVar

from ..auth.policy import EFFORT_LEVELS, OWNER_EFFORT_MIN
from ..config.channel import ChannelRegistry
from ..core.errors import ConfigError
from ..engine.capability import ToolRestriction
from ..engine.environment import registry
from ..engine.registry import default_registry
from .check import CheckResult, PreflightCheck, PreflightContext

_SHEBANG_ENV_RE = re.compile(r"#!\s*/usr/bin/env\s+(\S+)")

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
        # /Users/taelkim-work as living inside /Users/taelkim (sca-2wt).
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


class OwnerSettingsInertCheck(PreflightCheck):
    """Warns once at boot about channel settings that are silently ignored for the owner.

    `EngineSpec.model_for_owner()` and `OWNER_EFFORT_MIN` always win over a
    channel's `model`/`effort` for the owner, so a value in channels.json can
    look active while never actually applying. `fatal=False` — this only
    prevents misreading the config, it doesn't block boot.

    Codex uses a different model-naming scheme, so it's skipped here.
    """

    name: ClassVar[str] = "owner_settings_inert"

    def run(self, ctx: PreflightContext) -> CheckResult:
        if ctx.profile.primary_engine.type == "codex":
            return CheckResult(ok=True, detail="codex 엔진은 점검 대상이 아니다")
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



class ToolAllowlistEnforcementCheck(PreflightCheck):
    """Warns when the configured engine cannot enforce a tool allowlist.

    The caller builds the same allowed_tools list for every engine, but only
    claude passes it to the CLI as an allowlist. Codex takes a sandbox mode --
    a different axis, not a weaker allowlist -- and agy enforces nothing. The
    fact belongs at configuration time: recording it per request would repeat
    the same sentence on every request that bot ever serves (sca-dyb.11).

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
            actual = engine_class.capabilities.tool_restriction
            if actual is ToolRestriction.EXACT_ALLOWLIST:
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
        mcp_config = ctx.profile.paths.mcp_config
        if not mcp_config.exists():
            return CheckResult(ok=True, detail=f"MCP 설정 없음 : {mcp_config}", fatal=False)
        broken = self._mcp_ready(mcp_config)
        if broken:
            return CheckResult(ok=False, detail="; ".join(broken))
        return CheckResult(ok=True, detail="MCP 기동 점검 통과")

    @staticmethod
    def _mcp_ready(mcp_config: Path) -> list[str]:
        broken = []
        try:
            servers = json.loads(mcp_config.read_text()).get("mcpServers", {})
        except (OSError, json.JSONDecodeError) as e:
            return [f"MCP 설정을 읽지 못했습니다 : {e}"]
        for name, cfg in servers.items():
            command = cfg.get("command")
            if not command:
                continue
            real = shutil.which(command) or command
            if not os.path.exists(real):
                broken.append(f"{name} : 실행 파일 없음")
                continue
            try:
                with open(real, "rb") as f:
                    first = f.readline(256).decode("utf-8", "replace").strip()
            except OSError:
                continue
            m = _SHEBANG_ENV_RE.match(first)
            if m and not shutil.which(m.group(1)):
                broken.append(f"{name} : {m.group(1)} 를 PATH 에서 못 찾음")
        return broken


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
