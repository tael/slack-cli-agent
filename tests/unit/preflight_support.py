"""기동 게이트를 다루지 않는 시험이 쓰는 대역.

worker 와 ingress 는 기동 전에 실제 PreflightSuite 를 돈다(sca-xay). 게이트가
대상이 아닌 시험까지 통과 가능한 프로필을 갖추게 하면, 무엇을 보려던
시험인지와 무관하게 점검이 늘 때마다 같이 깨진다.

검사 목록만 갈아 끼우고 판정과 출력은 진짜 것을 그대로 쓴다 - 대역이 진짜가
하는 일을 안 하면 회귀가 샌다.
"""

from __future__ import annotations

from typing import ClassVar

from slack_cli_agent.preflight.check import CheckResult, PreflightCheck, PreflightContext
from slack_cli_agent.preflight.suite import PreflightSuite


class 고정점검(PreflightCheck):
    name: ClassVar[str] = "고정"

    def __init__(self, *, ok: bool, fatal: bool = True) -> None:
        self._ok = ok
        self._fatal = fatal

    def run(self, ctx: PreflightContext) -> CheckResult:
        return CheckResult(ok=self._ok, detail="시험용 고정 결과", fatal=self._fatal)


def 고정_suite(*, bootable: bool) -> PreflightSuite:
    return PreflightSuite(checks=(고정점검(ok=bootable),))


def 통과하는_suite() -> PreflightSuite:
    return 고정_suite(bootable=True)


def 막는_suite() -> PreflightSuite:
    return 고정_suite(bootable=False)
