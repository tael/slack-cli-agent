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

        먼저 파일 이름을 바꿔 집어 간다. `rename` 은 원자적이라 두 프로세스가
        동시에 봐도 한쪽만 성공한다. 접수와 워커가 같은 파일을 보므로 이것이
        없으면 같은 보고가 두 번 발송된다.

        집어 간 뒤에 새로 저장되는 보고는 원래 경로에 쓰인다. 그래서 발송 중에
        생긴 보고를 이 발송이 지우지 않는다.

        발송에 실패하면 되돌려 놓는다. 실패했는데 버리면 그 보고가 다음
        기회에도 못 나가고 그대로 사라진다.
        """
        claimed = self._path.with_suffix(self._path.suffix + ".sending")
        try:
            self._path.rename(claimed)
        except OSError:
            # 없거나 이미 다른 쪽이 집어 갔다. 둘 다 여기서 끝내는 것이 맞다.
            return

        try:
            data = json.loads(claimed.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # 재기동 도중 쓰다 만 파일일 수 있다. 다시 시도할 근거(text)가
            # 없으니 되돌려 두고 로그만 남긴다.
            log.error("남겨둔 보고를 읽지 못했다 : %s", exc)
            self._restore(claimed)
            return

        text = data.get("text", "")
        try:
            self._sender(text)
        except Exception as exc:  # noqa: BLE001 — 발송 실패 원인이 슬랙 SDK 예외부터 네트워크 오류까지 다양해 한 자리에서 좁혀 잡을 수 없다. 실패하면 되돌려 다음 기회로 넘긴다
            log.error("남겨둔 보고를 보내지 못했다 : %s", exc)
            self._restore(claimed)
            return

        try:
            claimed.unlink()
        except OSError as exc:
            log.warning("보낸 보고 파일을 지우지 못했다 : %s", exc)
        else:
            log.info("남겨둔 보고를 보냈다.")

    def _restore(self, claimed: Path) -> None:
        """집어 간 것을 원래 자리로 되돌린다.

        그 사이 새 보고가 저장됐으면 그것이 더 최근이므로 되돌리지 않고 버린다.
        단일 슬롯이라 남길 것은 가장 최근 하나다.
        """
        if self._path.exists():
            try:
                claimed.unlink()
            except OSError:
                pass
            return
        try:
            claimed.rename(self._path)
        except OSError as exc:
            log.error("보고를 되돌리지 못했다 : %s", exc)
