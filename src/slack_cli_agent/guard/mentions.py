"""호칭 보정 — 평문 멘션 치환과 엉뚱한 수신자 제거.

원본 `fix_plain_mentions`, `guard_wrong_addressee` 의 판정 본문을 그대로
옮겼다. 두 함수 다 전역(이름표, `OWNER_USER_ID`)을 인자로 받았는데, 여기서는
`GuardContext` 의 필드로 받는다.
"""

from __future__ import annotations

import re
from typing import ClassVar

from slack_cli_agent.guard.base import GuardContext, GuardResult, OutputGuard

# 답변 첫머리에서 누군가를 부르는 형태. <@U...> 님, <@U...> 씨 따위다.
ADDRESSED_AT_HEAD = re.compile(r"^\s*<@([A-Z0-9]+)>\s*(?:님|씨)?[,.!\s]*")

# 코드 울타리와 인라인 코드. 그 안의 글자는 손대지 않는다.
CODE_SPANS = re.compile(r"```.*?```|`[^`\n]*`", re.S)


class PlainMentionGuard(OutputGuard):
    """평문으로 적은 호칭을 진짜 멘션으로 바꾼다.

    슬랙에서 @김서준 은 그냥 글자다. 링크가 걸리지 않아 불려도 알아채지 못한다.
    사람을 부르려면 <@U0EXAMPLE01> 형태여야 한다.

    2026-08-26 12:02 와 12:05 에 이 봇이 "@김서준 개발팀 님" 으로 적어 올렸다.
    부르는 줄 알았는데 상대에게는 알림이 가지 않았다. 지침으로만 두면 모델이
    어기는 순간 그대로 나간다. 나가기 직전에 코드로 바꾼다.
    """

    name: ClassVar[str] = "plain_mention"

    @staticmethod
    def _fix_plain_mentions(
        body: str, table: dict[str, str]
    ) -> tuple[str, list[str]]:
        """원본 `fix_plain_mentions` 본문 그대로. 이름이 긴 것부터 바꾼다 —

        "김서준" 을 먼저 바꾸면 "김서준 개발팀" 의 뒤 토막이 남는다.
        """
        if not body or not table:
            return body, []

        # 코드 안은 건드리지 않는다. 예시로 적은 것을 멘션으로 바꾸면 엉뚱하게 부른다.
        holes: list[str] = []

        def stash(m: re.Match[str]) -> str:
            holes.append(m.group(0))
            return f"\x00{len(holes) - 1}\x00"

        out = CODE_SPANS.sub(stash, body)

        changed: list[str] = []
        for name in sorted(table, key=len, reverse=True):
            uid = table[name]
            pat = re.compile(r"@" + re.escape(name) + r"(?:\s*(?:님|씨))?")
            if pat.search(out):
                out = pat.sub(f"<@{uid}>", out)
                changed.append(name)

        out = re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], out)
        return out, changed

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        out, changed = self._fix_plain_mentions(body, dict(ctx.mention_names))
        if not changed:
            return GuardResult(body=body, changed=False)
        return GuardResult(body=out, changed=True, detail={"names": changed})


class AddresseeGuard(OutputGuard):
    """말을 건 사람이 아닌 다른 사람을 부르며 답하는 것을 막는다.

    2026-08-25 에 소유자의 혼잣말에 대고 앞 턴 상대였던 멘티를 멘션해 답했다.
    화자 표시와 지침으로 줄이긴 했으나 둘 다 모델이 지켜야 성립한다.
    지켜졌는지 코드로 확인하는 자리가 여기다.

    첫머리 멘션만 본다. 본문 가운데 나오는 멘션은 초안이나 인용일 수 있어
    건드리지 않는다.

    조용히 고치지 않는다 — 지운 것을 본문에 남긴다(원본 `handle_request` 가
    하던 것을 여기로 합쳤다. `guard_wrong_addressee` 자체는 무엇을 지웠는지만
    돌려주고, 발신 문구를 붙이는 것은 호출부의 일이었다).
    """

    name: ClassVar[str] = "wrong_addressee"

    @staticmethod
    def _guard_wrong_addressee(
        body: str, is_owner: bool, asker_id: str, owner_user_id: str
    ) -> tuple[str, str | None]:
        """원본 `guard_wrong_addressee` 본문 그대로."""
        m = ADDRESSED_AT_HEAD.match(body or "")
        if not m:
            return body, None
        target = m.group(1)
        # 말을 건 당사자를 부르는 것은 정상이다
        if target == (asker_id or "") or (is_owner and target == owner_user_id):
            return body, None
        fixed = body[m.end() :].lstrip()
        return fixed, target

    def apply(self, body: str, ctx: GuardContext) -> GuardResult:
        fixed, wrong = self._guard_wrong_addressee(
            body, ctx.is_owner, ctx.asker_id, ctx.owner_user_id
        )
        if wrong is None:
            return GuardResult(body=body, changed=False)
        fixed = fixed + "\n\n(다른 분을 부르는 첫머리를 지웠습니다.)"
        return GuardResult(body=fixed, changed=True, detail={"wrong_target": wrong})
