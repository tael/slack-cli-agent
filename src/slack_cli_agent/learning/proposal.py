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
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_path(day)
        if self._try_create_lock(path):
            return True
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
        if now - mtime < stale_after:
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return self._try_create_lock(path)

    def release_lock(self, day: str) -> None:
        """잠금을 푼다. 이미 없으면 조용히 넘어간다."""
        try:
            self._lock_path(day).unlink()
        except FileNotFoundError:
            pass

    def _lock_path(self, day: str) -> Path:
        return self._dir / f"{day}.lock"

    @staticmethod
    def _try_create_lock(path: Path) -> bool:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.close(fd)
        return True
