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
    EngineBinaryCheck,
    EngineHomeCredentialCheck,
    McpServerCheck,
    OwnerSettingsInertCheck,
    PromptFileCheck,
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
    ) -> None:
        self._checks: tuple[PreflightCheck, ...] = (
            WorkdirCheck(extra_dirs=list(extra_workdirs or ())),
            EngineBinaryCheck(),
            EngineHomeCredentialCheck(),
            McpServerCheck(),
            PromptFileCheck(required_names=list(required_prompts or ())),
            # fatal=False: only meant to prevent a misread config, not to block boot.
            OwnerSettingsInertCheck(),
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
