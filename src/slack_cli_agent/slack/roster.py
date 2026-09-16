"""Builds a roster mapping account handles to real names.

Internal data refers to people by account handle (e.g. alice.kim); if
we don't know who that is, replies mix handles and real names and the
reader has to cross-reference them manually.

Slack's users_list gives `name` (the handle) and `profile.real_name`
(the real name) — no email scope needed to join them.

Includes departed employees (`deleted`) too — historical data actually
needs them more, not less. The full table isn't loaded into every
request; it's written to a file, and this class only writes it.
Telling the model where the file is is prompt.sections.RosterSection's
job.

refresh() does one pass. The periodic loop (the original's
people_loop) doesn't live in this class — an infinite loop inside it
would make unit-testing a single pass impossible. The caller re-runs
refresh() every RuntimeSettings.roster_refresh_sec instead.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Same value as transcript.py's KST; imported separately here to avoid
# depending on that module.
from ..core.timezones import KST

logger = logging.getLogger(__name__)

# Example values used in the usage note written into the roster file — not real people.
_EXAMPLE_HANDLE = "jamie.oh"
_EXAMPLE_NAME = "오제이미"
_EXAMPLE_LEFT_NAME = "최우주 (퇴사)"


@dataclass(frozen=True)
class RosterEntry:
    """One roster row: an account handle, its real name, and departure status."""

    account_handle: str
    display_name: str
    has_left: bool


class RosterBuilder:
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
        """Runs one pass. Returns the number of entries written.

        If the fetch raises or returns nothing, leaves the existing
        file alone and returns 0 — one failed lookup shouldn't wipe
        out the roster and make every reply fall back to bare handles.
        """
        try:
            entries, seen = self._fetch_entries()
        except Exception as exc:  # noqa: BLE001 - same policy as the original: log the failure, don't hide the cause
            logger.warning("명부를 만들지 못했다: %s", exc)
            return 0

        if not entries:
            if seen:
                # 조회는 됐고 제외 규칙이 전부 걸러낸 경우다. 조회 실패와 같은
                # 문구를 쓰면 스코프나 네트워크를 의심하게 된다 (sca-4pf).
                logger.warning(
                    "명부에 넣을 사람이 없다. 조회 %d명 중 규칙에 맞는 사람이 0명이다."
                    " 기존 파일을 그대로 둔다.",
                    seen,
                )
            else:
                logger.warning("명부 조회 결과가 비었다. 기존 파일을 그대로 둔다.")
            return 0

        if not self._write(entries):
            return 0
        logger.info("명부를 갱신했다. %d명", len(entries))
        return len(entries)

    def _fetch_entries(self) -> tuple[list[RosterEntry], int]:
        """Also returns how many members the lookup saw, so an empty roster
        can say whether the lookup came back empty or the exclusion rules
        dropped everyone."""
        rows: dict[str, RosterEntry] = {}
        seen = 0
        cursor = ""
        while True:
            response = self._client.users_list(limit=self._page_size, cursor=cursor or None)
            for member in response.get("members", []):
                seen += 1
                entry = self._to_entry(member)
                if entry is not None:
                    rows[entry.account_handle] = entry
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return list(rows.values()), seen

    @staticmethod
    def _to_entry(member: Mapping[str, Any]) -> RosterEntry | None:
        """Same exclusion rules as the original. Any one of these
        drops the person from the roster:

        - bots/app users have no account-handle concept
        - an empty handle or name leaves nothing to join
        - a handle equal to the name adds no value
        - a handle without a dot isn't the company's handle format
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
            (
                f"{generated_at} KST 기준 {len(entries)}명"
                f" (재직 {active_count}, 퇴사 {len(entries) - active_count})"
            ),
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
