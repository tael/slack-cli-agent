"""RosterBuilder — 계정 핸들과 사람 이름을 잇는 명부를 만든다.

원본 `bot.py` 의 `build_people()`(4550행 근처)을 옮긴 것이다. 사내 데이터는
사람을 계정 핸들(예: alice.kim)로 남기는데, 그 핸들이 누구인지 모르면 답변이
핸들과 실명을 섞어 쓰게 되고 읽는 사람이 같은 사람인지 대조해야 한다.

슬랙 `users_list` 의 `name` 이 계정 핸들이고 `profile.real_name` 이 실명이다.
이메일 스코프 없이 이 둘만으로 잇는다.

퇴사자(`deleted`)도 담는다. 과거 데이터에 남은 이름이라 오히려 더 필요하다.
표 전체는 매 요청에 싣지 않고 파일로 둔다 — 그 이유로 이 클래스는 표 내용을
돌려주지 않고 파일에 쓰기만 한다. 모델에게 경로를 알리는 일은
`prompt.sections.RosterSection` 몫이다.

`refresh()` 한 번이 한 회차다. 주기 반복(원본 `people_loop`)은 이 클래스
안에 두지 않는다 — 무한 루프를 클래스 안에 두면 단위 시험으로 한 회차만
검증할 수 없다. 반복은 호출부가 `RuntimeSettings.roster_refresh_sec` 간격으로
`refresh()` 를 다시 부르는 방식으로 돌린다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 시각 표기 기준. transcript.py 의 KST 와 값은 같지만, 이 모듈은 그쪽에
# 의존하지 않기 위해 따로 둔다.
KST = timezone(timedelta(hours=9))

# 명부 파일 사용 안내에 넣는 예시. 실제 인물이 아닌 가상 핸들이다.
_EXAMPLE_HANDLE = "jamie.oh"
_EXAMPLE_NAME = "오제이미"
_EXAMPLE_LEFT_NAME = "최우주 (퇴사)"


@dataclass(frozen=True)
class RosterEntry:
    """명부 한 줄. 계정 핸들 하나와 그 사람의 실명, 퇴사 여부."""

    account_handle: str
    display_name: str
    has_left: bool


class RosterBuilder:
    """슬랙 사용자 목록에서 계정 핸들과 실명 표를 만들어 파일로 쓴다."""

    def __init__(
        self,
        client: Any,
        output_path: Path,
        page_size: int = 200,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._output_path = output_path
        self._page_size = page_size
        self._now = now

    def refresh(self) -> int:
        """한 회차를 돌린다. 담은 인원 수를 돌려준다.

        조회가 예외를 내거나 결과가 비면 기존 파일을 그대로 두고 0을
        돌려준다. 조회 한 번의 실패로 명부가 사라지면 그 뒤 모든 답변이
        핸들만 쓰게 된다 — 그것을 막는 것이 이 정책의 목적이다.
        """
        try:
            entries = self._fetch_entries()
        except Exception as exc:  # noqa: BLE001 - 원본과 같은 정책. 실패 사유를 가리지 않고 남긴다
            logger.warning("명부를 만들지 못했다: %s", exc)
            return 0

        if not entries:
            logger.warning("명부 조회 결과가 비었다. 기존 파일을 그대로 둔다.")
            return 0

        if not self._write(entries):
            return 0
        logger.info("명부를 갱신했다. %d명", len(entries))
        return len(entries)

    def _fetch_entries(self) -> list[RosterEntry]:
        """`users_list` 를 커서 페이지네이션으로 끝까지 조회한다."""
        rows: dict[str, RosterEntry] = {}
        cursor = ""
        while True:
            response = self._client.users_list(limit=self._page_size, cursor=cursor or None)
            for member in response.get("members", []):
                entry = self._to_entry(member)
                if entry is not None:
                    rows[entry.account_handle] = entry
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return list(rows.values())

    @staticmethod
    def _to_entry(member: Mapping[str, Any]) -> RosterEntry | None:
        """원본과 같은 제외 기준. 하나라도 걸리면 그 사람은 명부에 넣지 않는다.

        - 봇·앱 사용자는 계정 핸들 개념이 없다
        - 핸들 또는 실명이 비어 있으면 이을 대상이 없다
        - 핸들과 실명이 같으면 표에 넣어도 도움이 안 된다
        - 핸들에 점이 없으면 계정 핸들 형태가 아니다(사내 규칙)
        """
        if member.get("is_bot") or member.get("is_app_user"):
            return None
        account_handle = (member.get("name") or "").strip()
        display_name = ((member.get("profile") or {}).get("real_name") or "").strip()
        if not account_handle or not display_name or account_handle == display_name:
            return None
        if "." not in account_handle:
            return None
        return RosterEntry(
            account_handle=account_handle,
            display_name=display_name,
            has_left=bool(member.get("deleted")),
        )

    def _write(self, entries: list[RosterEntry]) -> bool:
        lines = self._render(sorted(entries, key=lambda e: e.account_handle))
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            logger.error("명부 파일을 쓰지 못했다: %s", exc)
            return False
        return True

    def _render(self, entries: list[RosterEntry]) -> list[str]:
        active_count = sum(1 for entry in entries if not entry.has_left)
        generated_at = datetime.fromtimestamp(self._now(), KST).strftime("%Y-%m-%d %H:%M")
        lines = [
            "# 계정 핸들과 사람 이름",
            "",
            f"{generated_at} KST 기준 {len(entries)}명"
            f" (재직 {active_count}, 퇴사 {len(entries) - active_count})",
            "",
            "사내 데이터는 사람을 계정 핸들로 남긴다. 그 핸들이 누구인지 여기서 찾는다.",
            "",
            "쓰는 법",
            "",
            f"- 답변에 계정 핸들을 쓸 때 이름을 나란히 적는다. 예: {_EXAMPLE_HANDLE} ({_EXAMPLE_NAME})",
            "- 표 안에서는 핸들만 써도 된다. 표 밖 문장에서 사람을 가리킬 때 이름을 쓴다",
            "- 핸들과 이름을 한 답변에서 섞어 쓰지 않는다. 같은 사람인지 대조하게 만든다",
            "- 여기 없는 핸들은 지어내지 않는다. 핸들 그대로 쓰고 모른다고 밝힌다",
            f"- 퇴사자를 쓸 때는 이름 뒤에 (퇴사) 를 붙인다. 예: {_EXAMPLE_LEFT_NAME}",
            "- 퇴사자를 담당자로 안내하지 않는다. 과거 기록을 읽을 때만 쓴다",
            "",
            "| 계정 핸들 | 이름 | 상태 |",
            "|---|---|---|",
        ]
        for entry in entries:
            status = "퇴사" if entry.has_left else ""
            lines.append(f"| {entry.account_handle} | {entry.display_name} | {status} |")
        return lines
