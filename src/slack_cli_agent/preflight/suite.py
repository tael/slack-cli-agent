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

from collections.abc import Iterator, Sequence
from typing import TextIO

from ..config.profile import Profile
from .check import CheckResult, PreflightCheck, PreflightContext
from .checks import (
    ChannelSettingsReadableCheck,
    ChannelUserToolsCheck,
    EngineBinaryCheck,
    EngineHomeCredentialCheck,
    EngineSettingsCheck,
    McpCredentialCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    ProfilePermissionCheck,
    ProfileUnknownKeysCheck,
    PromptFileCheck,
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
