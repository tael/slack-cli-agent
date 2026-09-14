"""상태 디렉터리 하위 경로. 경로 조립을 한 곳에 모은다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @classmethod
    def for_bot(cls, name: str, home: Path | None = None) -> "StatePaths":
        return cls((home or Path.home()) / f".{name}")

    @property
    def profile(self) -> Path:
        return self.root / "profile.json"

    @property
    def channels(self) -> Path:
        return self.root / "channels.json"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def persona(self) -> Path:
        return self.root / "persona"

    @property
    def proposals(self) -> Path:
        """학습 제안 파일이 날짜별로 쌓이는 곳. 원본 PROPOSAL_DIR."""
        return self.root / "proposals"

    @property
    def knowledge(self) -> Path:
        """채널별 지식 파일. 학습 반영이 이 아래에 줄을 더한다. 원본 KNOWLEDGE_DIR."""
        return self.persona / "knowledge"

    @property
    def engine_dir(self) -> Path:
        return self.root / "engine"

    @property
    def engine_state(self) -> Path:
        return self.root / "engine_state.json"

    @property
    def database(self) -> Path:
        """기계 상태 전부. 큐·세션·감사·부검 기록·지켜보기 큐."""
        return self.root / "state.db"

    @property
    def audit_log(self) -> Path:
        """감사 기록 사본. 외부 도구가 읽는 형식이라 DB 와 함께 남긴다."""
        return self.root / "audit.jsonl"

    @property
    def state_snapshot(self) -> Path:
        """프로세스 상태 기록. 주기적으로 통째로 갈아 끼운다. 원본 STATE_FILE."""
        return self.root / "state.json"

    @property
    def pid_file(self) -> Path:
        return self.root / "bot.pid"

    @property
    def mcp_config(self) -> Path:
        return self.engine_dir / "mcp.json"

    def engine_settings(self, engine_type: str) -> Path:
        return self.engine_dir / f"settings-{engine_type}.json"

    def ensure(self) -> None:
        for path in (self.root, self.prompts, self.persona, self.engine_dir):
            path.mkdir(parents=True, exist_ok=True)
