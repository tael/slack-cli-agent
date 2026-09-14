"""구체 점검 4종.

01-source-analysis.md 20절, 03-TRD.md 8절이 정한 기본 점검이다.

`McpServerCheck` 는 원본 `bot.py` 의 `mcp_ready()` 를 그대로 옮긴 것이다(이식
대상, 03-TRD.md 0-1절). 함수 본문은 고치지 않고 클래스로 감쌌다. 원본은
LaunchAgent 의 좁은 PATH(`/usr/bin:/bin:/usr/sbin:/sbin`) 때문에
`#!/usr/bin/env node` 셰뱅이 조용히 실패해 도구만 사라지는 사고
(2026-08-25, MCP 서버 8개)를 막으려고 셰뱅 인터프리터까지 확인한다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import ClassVar

from ..auth.policy import EFFORT_LEVELS, OWNER_EFFORT_MIN
from ..config.channel import ChannelRegistry
from .check import CheckResult, PreflightCheck, PreflightContext

_SHEBANG_ENV_RE = re.compile(r"#!\s*/usr/bin/env\s+(\S+)")


class PromptFileCheck(PreflightCheck):
    """프롬프트 파일이 전부 있고 비어 있지 않은가.

    원본은 `bot.py` 소스를 정규식으로 훑어 `prompt_text("NAME")` 호출을
    찾아 이름 목록을 만든다(손으로 관리하지 않기 위해서). 이 패키지는
    단일 소스 파일이 없으므로, 필요한 이름 목록을 호출부가 넘긴다 —
    `PromptSection` 목록이 실제로 요구하는 이름이 그 목록이다.
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
    """작업 디렉터리가 있고, 홈 밖이고, `CLAUDE.md` 가 없는가.

    자리가 없으면 subprocess 가 cwd 를 잡지 못해 모든 요청이 실패하는데
    봇은 정상 기동하고 로그도 안 남는다. 홈 안이면 전역 지침이 매 턴
    실려 토큰이 든다.
    """

    name: ClassVar[str] = "workdir"

    def __init__(self, extra_dirs: list[str] | None = None) -> None:
        self._extra_dirs = tuple(extra_dirs or ())

    def run(self, ctx: PreflightContext) -> CheckResult:
        home = str(Path.home())
        targets = [ctx.profile.work_root, *(ctx.profile.work_root / d for d in self._extra_dirs)]
        bad: list[str] = []
        for target in targets:
            if not target.is_dir():
                bad.append(f"작업 자리가 없다 : {target}")
            elif str(target).startswith(home):
                bad.append(f"작업 자리가 홈 안에 있다 : {target}")
            elif (target / "CLAUDE.md").exists():
                bad.append(f"작업 자리에 CLAUDE.md 가 있다 : {target / 'CLAUDE.md'}")
        if bad:
            return CheckResult(ok=False, detail="; ".join(bad))
        return CheckResult(ok=True, detail="작업 자리 점검 통과")


class EngineBinaryCheck(PreflightCheck):
    """1차·2차 엔진 실행 파일을 지금 찾을 수 있는가.

    절대경로면 존재를, 이름만 있으면(PATH 로 찾는 것) `shutil.which` 로
    확인한다.
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


class OwnerSettingsInertCheck(PreflightCheck):
    """소유자 요청에는 적용되지 않는 채널 설정을 기동 때 한 번 알린다.

    이식 대상. 원본 `bot.py` 의 `warn_inert_owner_settings()` 를 preflight
    구조에 맞춰 옮겼다. 원본은 `log.info` 로만 알렸고, 여기서는 CheckResult 로
    돌려주되 `fatal=False` 라 기동을 막지 않는다.

    model 은 `EngineSpec.model_for_owner()` 가, effort 는 `OWNER_EFFORT_MIN`
    이 소유자 요청에 우선한다. 그래서 `channels.json` 에 적어 둔 값이
    소유자에게는 그대로 죽는다. 설정 파일만 보면 그 값으로 도는 것처럼
    읽혀 오독을 만든다.

    Codex 엔진은 모델 이름 체계가 달라 이 점검이 헛돈다. 그쪽은 건너뛴다.
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


class McpServerCheck(PreflightCheck):
    """MCP 서버들이 지금 기동 가능한 상태인지 본다.

    이식 대상. 원본 `bot.py` 의 `mcp_ready()` 본문을 그대로 옮겼다 — 잠자고
    깨어난 뒤에는 경로와 실행 파일이 그대로인지 확신할 수 없어, 도구가
    조용히 빠진 채로 답하는 것을 막는다.
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
        """원본 `mcp_ready()` 본문. 수정하지 않았다."""
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
