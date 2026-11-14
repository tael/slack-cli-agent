"""The standard check set, in one place.

The list lived inside the `preflight` command, so anything else that wanted
to run the same checks — the boot gate on the long-running commands — had to
build its own copy. Two copies drift: a check added here would not reach the
gate, and the gate would call a profile bootable that the command rejects
(sca-s5r).

Result formatting lives here for the same reason: a failure must read the
same whether a person ran the command or a service refused to start.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from ..config.profile import Profile
from .check import CheckResult, PreflightCheck, PreflightContext
from .checks import (
    ChannelSettingsReadableCheck,
    ChannelUserToolsCheck,
    CodexConfigLoadCheck,
    CompileCheck,
    CoreImportCheck,
    DuplicateMcpSystemCheck,
    EngineBinaryCheck,
    EngineHomeCredentialCheck,
    EngineSettingsCheck,
    FullDiskAccessCheck,
    McpCredentialCheck,
    McpScriptOwnershipCheck,
    McpOwnDirCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    PluginAstCheck,
    ProfilePermissionCheck,
    ProfileUnknownKeysCheck,
    PromptFileCheck,
    PromptToolConsistencyCheck,
    ScriptCapabilityGradeCheck,
    SkillSymlinkCheck,
    SlackApiRegistrationCheck,
    SourceTreeCheck,
    ToolAllowlistEnforcementCheck,
    UsageCheckCommandCheck,
    WorkdirCheck,
)
from .runner import PreflightReport, PreflightRunner


class PreflightSuite:
    """Builds the standard checks and runs them against one profile.

    `required_prompts` and `extra_workdirs` come from the command's options;
    boot passes neither, which is the same set a bare `preflight` runs.
    """

    def __init__(
        self,
        required_prompts: Sequence[str] | None = None,
        extra_workdirs: Sequence[str] | None = None,
        *,
        checks: Sequence[PreflightCheck] | None = None,
        # 아래 네 항목은 조직 지식(어떤 낱말이 어떤 도구를 약속하는지, 어떤
        # 서버 이름이 같은 시스템을 가리키는지, 등급 체계와 그 대상 자리)
        # 없이는 아무것도 보지 않는 점검들의 주입점이다. B6 는 공통 골격만
        # 신는다 — 값을 채우는 것은 그 값을 아는 쪽(조직 설정, 플러그인)의
        # 몫이다.
        prompt_tool_promises: Mapping[str, Sequence[str]] | None = None,
        prompt_tool_base_tools: Sequence[str] = (),
        duplicate_mcp_groups: Mapping[str, Sequence[str]] | None = None,
        capability_scripts_dir: Path | None = None,
        capability_registry_file: Path | None = None,
        slack_api_source_files: Sequence[Path] = (),
        slack_api_registry_file: Path | None = None,
    ) -> None:
        # Injection point for tests that aren't about the gate: they swap the
        # check list and keep the real verdict and formatting. Without it a
        # double has to overwrite a private name, which breaks silently when
        # the storage here changes (sca-mb7).
        if checks is not None:
            self._checks: tuple[PreflightCheck, ...] = tuple(checks)
            return
        self._checks = (
            WorkdirCheck(extra_dirs=list(extra_workdirs or ())),
            EngineBinaryCheck(),
            EngineHomeCredentialCheck(),
            McpServerCheck(),
            McpOwnDirCheck(),
            # fatal 은 EngineSettingsCheck 가 결과마다 정한다 - 부재는 경고, 깨짐은 기동 정지.
            EngineSettingsCheck(),
            PromptFileCheck(required_names=list(required_prompts or ())),
            # fatal=False: only meant to prevent a misread config, not to block boot.
            ChannelSettingsReadableCheck(),
            OwnerSettingsInertCheck(),
            # fatal=False for the same reason: a chmod fixes it, taking the bot down does not.
            ProfilePermissionCheck(),
            # fatal=False: 허용목록이 안 걸리는 엔진을 쓰는 것은 운영자의 선택일 수
            # 있다. 그 사실을 설정 시점에 알리는 것이 목적이다.
            ToolAllowlistEnforcementCheck(),
            UsageCheckCommandCheck(),
            # fatal=False: 오타 하나로 봇을 못 뜨게 하지 않는다. 미설정과
            # 오타가 같은 모습이 되는 것을 기동 시점에 알리는 것이 목적이다.
            ProfileUnknownKeysCheck(),
            ChannelUserToolsCheck(),
            # 표기가 안 풀리면 MCP 서버는 빈 자격으로 인증 실패만 낸다. 기동에서 막는다.
            McpCredentialCheck(),
            # 기동 명령은 게이트 통과 뒤에야 core.application 을 import 한다. 여기서
            # 먼저 시도하지 않으면 깨진 코어가 기동 가능으로 통과하고 종료코드 1 로
            # 죽어 launchd 재기동이 반복된다.
            CoreImportCheck(),
            # fatal=False: 운영 트리가 origin/main 에서 벗어난 것을 기동 시점에 보이는
            # 것이 목적이다. 원격 조회 실패도 경고로만 남긴다.
            SourceTreeCheck(),
            # 아래부터 B6 — 마메치 check.py 와 짝 맞춘 9종.
            # CompileCheck 는 PluginAstCheck 앞 — apply.sh 가 py_compile 를 먼저
            # 돌리고 그 다음 check.py(이름 검사)를 돌린 순서 그대로다. 깨진 구문은
            # ast.parse 가 SyntaxError 로 터지고 PluginAstCheck 는 그것을 건너뛰므로,
            # 컴파일 점검이 먼저 서야 기동 전에 걸린다.
            CompileCheck(),
            PluginAstCheck(),
            SkillSymlinkCheck(),
            CodexConfigLoadCheck(),
            McpScriptOwnershipCheck(),
            # fatal=False: 전체 디스크 접근은 권한 창 하나를 줄이는 사전 조치일
            # 뿐이다. 없다고 기동을 막을 이유가 아니다.
            FullDiskAccessCheck(),
            # 아래 네 점검은 조직 지식이 없으면 아무것도 보지 않는다(생성자 기본값).
            PromptToolConsistencyCheck(
                promises=prompt_tool_promises, base_tools=prompt_tool_base_tools
            ),
            DuplicateMcpSystemCheck(groups=duplicate_mcp_groups),
            ScriptCapabilityGradeCheck(
                scripts_dir=capability_scripts_dir,
                capabilities_file=capability_registry_file,
            ),
            SlackApiRegistrationCheck(
                source_files=slack_api_source_files,
                registry_file=slack_api_registry_file,
            ),
        )

    @property
    def checks(self) -> tuple[PreflightCheck, ...]:
        return self._checks

    def run(self, profile: Profile) -> PreflightReport:
        return PreflightRunner(self._checks).run_all(PreflightContext(profile=profile))

    def messages(self, report: PreflightReport) -> Iterator[tuple[CheckResult | None, str]]:
        """One line per check, then the verdict. Yielded rather than printed so
        the boot gate can put the same wording in a log at the right level,
        instead of reimplementing the format (코덱스 리뷰). The paired result is
        None on the verdict line.
        """
        for check, result in zip(self._checks, report.results, strict=True):
            if result.ok:
                mark = "통과"
            elif result.fatal:
                mark = "실패"
            else:
                mark = "경고"
            yield result, f"[{mark}] {check.name} : {result.detail}"
        yield None, "기동 가능" if report.bootable else "기동 불가"

    def report_to(self, report: PreflightReport, stdout: TextIO) -> None:
        for _, line in self.messages(report):
            print(line, file=stdout)
