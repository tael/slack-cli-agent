"""표시 이름 조회.

원본 이 봇은 요청자의 사용자 ID 를 사람이 읽을 이름으로 바꿔 답변에 쓴다.
`bot.py` 의 `asker_display_name` 과 전역 `_asker_cache`, `_name_to_id` 가
하던 일을 클래스 하나로 옮긴 것이다.

조회 실패는 조용히 빈 문자열로 다루고 그 결과도 캐시한다. 실패한 사용자를
매번 다시 조회하지 않는다는 것이 원본의 판단이다.
"""

from __future__ import annotations

from typing import Any


class DisplayNameResolver:
    """사용자 ID 로 표시 이름을 찾고, 거꾸로 이름에서 ID 를 찾는 표도 함께 쌓는다."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[str, str] = {}
        self._name_to_id: dict[str, str] = {}

    def resolve(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._cache:
            self._cache[user_id] = self._lookup(user_id)
        name = self._cache[user_id]
        if name:
            self._name_to_id.setdefault(name, user_id)
            # "홍길동 개발팀" 처럼 소속이 붙어 있으면 앞 토막도 함께 담는다
            head = name.split()[0]
            if head and head != name:
                self._name_to_id.setdefault(head, user_id)
        return name

    def _lookup(self, user_id: str) -> str:
        try:
            info = self._client.users_info(user=user_id)
            profile = info["user"].get("profile") or {}
            return (
                profile.get("real_name")
                or profile.get("display_name")
                or info["user"].get("name")
                or ""
            )
        except Exception:
            return ""

    def __call__(self, user_id: str) -> str:
        return self.resolve(user_id)

    def name_table(self) -> dict[str, str]:
        return dict(self._name_to_id)

    def register(self, name: str, user_id: str) -> None:
        self._name_to_id.setdefault(name, user_id)
