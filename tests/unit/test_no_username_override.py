"""봇 글에 username 을 덮어쓰지 않는다 (2026-11-10).

username 을 주면 슬랙이 그 글을 user 없는 bot_message 로 저장한다. 같은 봇이
username 없이 올린 글(봇 사용자 명의)과 한 스레드에 섞이면 같은 이름의 발신자가
둘로 보인다. 봇 사용자 이름은 users.profile.set 으로 이미 한글이라 덮어쓸 이유가 없다.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "slack_cli_agent"
_POSTING = re.compile(r'''(username\s*=|["']username["']\s*[:\]])''')


def test_슬랙_게시_코드에_username_덮어쓰기가_없다() -> None:
    hits = []
    for path in SRC.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _POSTING.search(code) and "get(" not in code:
                hits.append(f"{path.relative_to(SRC)}:{no}: {line.strip()}")
    assert not hits, "username 덮어쓰기가 남아 있다:\n" + "\n".join(hits)


def test_진행_카드와_답변이_username_없이_게시된다() -> None:
    from slack_cli_agent.slack.progress import SlackProgressSink, SlackTaskCardProgressSink

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def chat_postMessage(self, **kw):  # noqa: N802
            self.calls.append(kw)
            return {"ts": "1.0"}

    for cls in (SlackProgressSink, SlackTaskCardProgressSink):
        c = Client()
        cls(c, "C1", "9.9", bot_display_name="구치파치").open("시작")
        assert "username" not in c.calls[0]
