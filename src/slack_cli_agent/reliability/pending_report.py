"""보내지 못한 보고 — 재기동 순간에 실패한 발송을 다음 기회에 대신 보낸다.

봇이 스스로 재기동할 때(`health.py` 의 `SelfRestarter`) 그 사유를 소유자에게
알린다. 그런데 재기동하는 그 순간은 슬랙 소켓 자체가 불안정한 시점이라 그
발송이 실패하기 쉽다. `SelfRestarter._announce` 는 발송 실패를 경고 로그만
남기고 그대로 종료해, 그러면 운영자는 장애가 났다는 사실 자체를 영영 못
받는다.

원본 `bot.py` 의 `save_pending_report`(`bot.py:6570`)와
`flush_pending_report`(`bot.py:6687`)를 클래스로 재구성했다. 전역
`PENDING_REPORT` 파일 경로와 모듈 함수 대신, 경로와 발송 함수를 생성자로
주입받는 `PendingReportStore` 하나로 묶는다 — 시험에서 임시 경로와 가짜
발송 함수를 넣어 재기동 없이도 검증할 수 있게 하기 위해서다.

동작은 원본과 같다.

- 단일 슬롯이다. 새로 `save` 하면 이전에 못 보낸 보고를 덮어쓴다. 원본도
  `write_text` 로 매번 파일 전체를 새로 쓰지, 목록에 이어 붙이지 않는다.
  보고가 밀리는 사이 여러 번 재기동해도 마지막 사유만 남는다.
- 발송에 성공했을 때만 저장 파일을 지운다. 실패했는데 지우면 그 보고가
  다음 기회에도 못 나가고 그대로 사라진다.
- 저장할 것이 없거나 저장 파일이 깨져 있어도 예외를 던지지 않는다. 이
  파일은 재기동 도중에 강제 종료돼 쓰다 만 상태로 남을 수 있는 자리라,
  읽기 실패를 그대로 올리면 그 실패가 재기동 절차 전체를 막는다.

원본과 다르게 만든 곳 — 저장 경로의 상위 디렉터리가 없으면 만든다.
원본은 `STATE_DIR` 이 기동 시점에 이미 만들어져 있다는 것을 전제하고
`write_text` 만 부른다. 이 클래스는 그 전제 없이 독립적으로 쓰이므로
직접 만든다.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


class PendingReportStore:
    """보내지 못한 보고를 파일 하나에 남겼다가, 발송 함수가 다시 성공하면 지운다."""

    def __init__(
        self,
        *,
        path: Path,
        sender: Callable[[str], None],
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._sender = sender
        self._now = now

    def save(self, text: str) -> None:
        """지금 보낼 수 없는 보고를 남긴다.

        이전에 못 보낸 보고가 있어도 그 위에 덮어쓴다 — 이 슬롯이 지금
        지키려는 것은 "가장 최근에 무엇이 있었는가" 이지 전체 이력이 아니다.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"at": self._now(), "text": text}, ensure_ascii=False)
            )
        except OSError as exc:
            log.error("보고를 남기지 못했다 : %s", exc)

    def flush(self) -> None:
        """남겨둔 보고가 있으면 다시 보낸다.

        발송에 성공했을 때만 파일을 지운다. 저장할 것이 없으면 그대로
        끝난다 — 재기동마다 매번 부르는 자리라 없는 것이 정상 상태다.
        """
        if not self._path.exists():
            return

        try:
            raw = self._path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            # 재기동 도중 쓰다 만 파일일 수 있다. 다시 시도할 근거(text)가
            # 없으니 지우지도 못하고 보내지도 못한다 — 있는 그대로 로그만
            # 남기고 다음 기회를 기다린다.
            log.error("남겨둔 보고를 읽지 못했다 : %s", exc)
            return

        text = data.get("text", "")
        try:
            self._sender(text)
        except Exception as exc:  # noqa: BLE001 — 발송 실패 원인이 슬랙 SDK 예외부터 네트워크 오류까지 다양해 한 자리에서 좁혀 잡을 수 없다. 실패하면 파일을 지우지 않고 다음 기회로 넘긴다
            log.error("남겨둔 보고를 보내지 못했다 : %s", exc)
            return

        try:
            self._path.unlink()
        except OSError as exc:
            log.warning("보낸 보고 파일을 지우지 못했다 : %s", exc)
        else:
            log.info("남겨둔 보고를 보냈다.")
