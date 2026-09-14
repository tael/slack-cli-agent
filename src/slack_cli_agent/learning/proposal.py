"""학습 제안 값 객체와 저장소.

원본 bot.py 의 ``latest_proposal()`` 을 대응한다. 하루치 배치 결과가 이
`.json` 파일 하나다 — 파일 이름 자체가 날짜(``YYYY-MM-DD.json``)라 사전순
정렬이 곧 시간순 정렬이다.

원본은 파일이 없는 경우와 JSON 파싱이 깨진 경우를 둘 다 ``(path, None)`` 으로
돌려주고, 호출부(``show_proposal``)는 그 둘을 구분하지 않고 "아직 학습 제안이
없어요."로 답했다. 판정 불가(깨진 파일)를 부재로 읽으면 실제로 제안이 있었는데
사람이 못 보는 상태로 남는다. 여기서는 Outcome 으로 구분해 호출부가 그 둘을
다르게 다룰 수 있게 한다.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.result import Outcome

_DEFAULT_STALE_AFTER = timedelta(hours=6)


def _str_tuple(value: object) -> tuple[str, ...]:
    """JSON 에서 읽은 필드를 문자열 튜플로 만든다. 원본과 런타임 동작이 같다.

    ``data.get(key)`` 는 ``object | None`` 이라 mypy 가 원소 형을 못 본다.
    저장 형식은 이 프로세스가 직접 만든 파일이라 원소가 문자열이라고
    믿는 것은 원본 그대로다 — 여기서 신뢰 경계를 명시할 뿐 판정을
    새로 넣지 않는다.
    """
    return tuple(value or ())  # type: ignore[arg-type]  # 원소가 문자열이라는 저장 계약을 신뢰한다


@dataclass(frozen=True)
class LearningProposal:
    """하루치 학습 제안 한 건.

    원본의 채널 지식 키 이름은 두 곳에서 다르게 쓰인다 — learn.py 가 만들어
    저장하는 필드는 ``channel_knowledge`` 이고, learn.py 의 분석 결과
    (``build_proposal`` 반환값)는 ``channel_facts`` 다. 저장 형식은 항상
    ``channel_knowledge`` 다. 이 클래스는 저장 형식만 다룬다.
    """

    day: str
    writing_style: tuple[str, ...] = ()
    channel_knowledge: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    corrections: tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def from_dict(cls, day: str, data: Mapping[str, object]) -> LearningProposal:
        raw_channels = data.get("channel_knowledge") or {}
        channels = {
            str(name): tuple(items or ())
            for name, items in raw_channels.items()
        } if isinstance(raw_channels, Mapping) else {}
        return cls(
            day=day,
            writing_style=_str_tuple(data.get("writing_style")),
            channel_knowledge=channels,
            corrections=_str_tuple(data.get("corrections")),
            note=str(data.get("note") or ""),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "writing_style": list(self.writing_style),
            "channel_knowledge": {k: list(v) for k, v in self.channel_knowledge.items()},
            "corrections": list(self.corrections),
            "note": self.note,
        }

    @property
    def has_content(self) -> bool:
        """반영할 내용이 하나라도 있는가. 원본 main() 의 has_content 판정과 같다."""
        return bool(
            self.writing_style
            or any(self.channel_knowledge.values())
            or self.corrections
        )


class ProposalStore:
    """제안 파일을 읽고 쓴다. 쓰기는 임시 파일을 거쳐 os.replace 로 교체한다."""

    def __init__(self, proposal_dir: Path) -> None:
        self._dir = proposal_dir
        # 이 인스턴스가 쥔 잠금을 가리는 표식. 남이 쥔 잠금을 풀지 않기 위한
        # 것이라 프로세스마다 달라야 하고, 같은 프로세스 안에서는 한 번에
        # 하나만 잠그므로 인스턴스 하나에 하나면 충분하다.
        self._token = uuid.uuid4().hex

    def latest(self) -> Outcome[LearningProposal]:
        """가장 최근 날짜의 제안. 파일이 없으면 부재, 파싱 실패면 판정 불가."""
        if not self._dir.exists():
            return Outcome.absent()
        files = sorted(self._dir.glob("*.json"))
        if not files:
            return Outcome.absent()
        path = files[-1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return Outcome.unknown(f"{path.name} 파싱 실패: {e}")
        if not isinstance(data, Mapping):
            return Outcome.unknown(f"{path.name} 형식이 맞지 않다")
        return Outcome.found(LearningProposal.from_dict(path.stem, data))

    def save(self, proposal: LearningProposal) -> Path:
        """제안을 `<day>.json` 으로 저장하고 그 경로를 돌려준다."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{proposal.day}.json"
        self._write_json(path, proposal.to_dict())
        return path

    def mark_applied(self, day: str, done: Mapping[str, int]) -> Path:
        """반영 시각과 반영 결과를 남긴다.

        원본은 두 자리(bot.py 의 apply_learning, learn.py 의 main)가 같은
        `.applied` 파일에 서로 다른 형식(시각 문자열 / done 딕셔너리)을 썼다.
        여기서는 하나로 합쳐 둘 다 담는다.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{day}.applied"
        payload = {
            "applied_at": datetime.now(UTC).isoformat(),
            "done": dict(done),
        }
        self._write_json(path, payload)
        return path

    def mark_done(self, day: str) -> Path:
        """그날 배치를 끝냈다는 표식. 다음 주기가 이 날짜를 다시 집을지 본다.

        `.applied` 와 구분한다 — 반영할 내용이 없는 날과 응답 기록이 아예
        없는 날에도 배치는 끝난 것이고, 그것을 미완료로 두면 주기마다 같은
        날짜를 다시 집는다. 반대로 제안 파일 존재로 판정하면 저장 뒤 반영이
        실패한 날이 영영 다시 안 돈다.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{day}.done"
        self._write_json(path, {"done_at": datetime.now(UTC).isoformat()})
        return path

    def is_done(self, day: str) -> bool:
        return (self._dir / f"{day}.done").exists()

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def acquire_lock(
        self, day: str, *, now: datetime, stale_after: timedelta = _DEFAULT_STALE_AFTER,
    ) -> bool:
        """그날 배치 잠금을 가져오면 True, 이미 다른 쪽이 쥐고 있으면 False.

        워커 여럿이 같은 날짜를 동시에 돌리는 것을 막는다. 잠금 파일을
        ``O_CREAT|O_EXCL`` 로 만든다 — 이미 있으면 원자적으로 실패한다.
        오래된 잠금(``stale_after`` 보다 오래된 것)은 죽은 프로세스가 풀지
        못하고 남긴 것으로 보고 다시 가져온다. ``now`` 는 호출부의 시계를
        그대로 받는다 — 이 메서드가 실제 시계를 재지 않는다.

        ``O_EXCL`` 은 생성만 보호한다. 오래된 잠금을 지우고 다시 만드는
        경로는 그 사이에 다른 워커가 만든 잠금을 덮어쓸 수 있어, 둘 다
        잠금을 얻었다고 보고 같은 날 배치를 두 번 돌린다. 그래서 회수를
        ``rename`` 으로 집고 집은 것이 정말 오래된 것인지 다시 확인한다.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(day)
        if self._try_create_lock(path):
            return True
        if not self._reclaim_stale(path, now=now, stale_after=stale_after):
            return False
        return self._try_create_lock(path)

    def _reclaim_stale(self, path: Path, *, now: datetime, stale_after: timedelta) -> bool:
        """오래된 잠금을 치웠으면 True. 치울 것이 없거나 신선하면 False.

        ``rename`` 은 원자적이라 동시에 시도한 여러 워커 중 하나만 성공한다.
        판정과 집기 사이에 앞 잠금이 풀리고 다른 워커가 새 잠금을 만들었을 수
        있으므로, 집은 것이 판정할 때 본 그 잠금인지 소유자로 대조한다.
        """
        owner = self._read_lock_owner(path)
        if not self._is_stale(path, now=now, stale_after=stale_after):
            return False
        claimed = path.with_name(f"{path.name}.stale-{self._token}")
        try:
            os.rename(path, claimed)
        except OSError:
            return False
        return self._confirm_claim(path, claimed, owner)

    @staticmethod
    def _confirm_claim(path: Path, claimed: Path, owner: str | None) -> bool:
        """집은 것이 집으려던 그 잠금이면 지우고 True. 아니면 되돌리고 False."""
        if ProposalStore._read_lock_owner(claimed) != owner:
            os.replace(claimed, path)
            return False
        try:
            claimed.unlink()
        except OSError:
            return False
        return True

    @staticmethod
    def _read_lock_owner(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    @staticmethod
    def _is_stale(path: Path, *, now: datetime, stale_after: timedelta) -> bool:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
        return now - mtime >= stale_after

    def release_lock(self, day: str) -> None:
        """내가 쥔 잠금만 푼다. 남의 잠금이면 그대로 둔다.

        잠금 파일에 적힌 소유자가 나와 다르면 앞 잠금이 이미 풀리고 다른
        워커가 새로 쥔 것이다. 그것을 지우면 그 워커가 도는 중에 또 한
        프로세스가 같은 날 배치를 시작한다.
        """
        path = self._lock_path(day)
        try:
            if path.read_text(encoding="utf-8").strip() != self._token:
                return
            path.unlink()
        except OSError:
            return

    def _lock_path(self, day: str) -> Path:
        return self._dir / f"{day}.lock"

    def _try_create_lock(self, path: Path) -> bool:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, self._token.encode())
        finally:
            os.close(fd)
        return True
