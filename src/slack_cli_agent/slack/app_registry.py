"""어느 앱이 어느 정본에 대응하는지의 운영 기록, 그리고 그 전체를 도는 감사.

`tools/slack-app.py diff` 는 봇 하나의 차이를 볼 뿐이고, 어느 봇이 있는지는
아무 데도 없었다. new-bot.sh 는 app ID 를 화면에 찍고 버린다. 그래서 대조는
사람이 기억해서 손으로 돌려야 하는 일이었다(sca-4eo).

레지스트리는 app ID 와 워크스페이스 이름을 담는다. 조직 고유값이라 저장소에
넣지 않고 설정 토큰과 같은 `~/.slack-app-config/` 에 같은 권한으로 둔다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 설정 토큰이 있는 자리. 레지스트리도 소유자만 읽는다.
DEFAULT_REGISTRY_PATH = Path.home() / ".slack-app-config" / "registry.json"


@dataclass(frozen=True)
class AppEntry:
    name: str
    workspace: str
    app_id: str
    manifest: Path


@dataclass(frozen=True)
class AuditFinding:
    """차이가 있었거나 조회 자체가 안 된 봇 하나."""

    name: str
    differences: tuple[str, ...]
    error: str = ""


class AppRegistry:

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_REGISTRY_PATH

    def entries(self) -> list[AppEntry]:
        return [self._entry(name, row) for name, row in sorted(self._load().items())]

    def get(self, name: str) -> AppEntry | None:
        row = self._load().get(name)
        return None if row is None else self._entry(name, row)

    def register(self, entry: AppEntry) -> None:
        data = self._load()
        data[entry.name] = {
            "workspace": entry.workspace,
            "app_id": entry.app_id,
            "manifest": str(entry.manifest),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._path.chmod(0o600)

    def _load(self) -> dict[str, dict[str, str]]:
        # A missing file means no bot has been created yet, and a hand-edited
        # broken one must not stop the sweep over every other bot.
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    @staticmethod
    def _entry(name: str, row: dict[str, str]) -> AppEntry:
        return AppEntry(
            name=name,
            workspace=str(row.get("workspace", "")),
            app_id=str(row.get("app_id", "")),
            manifest=Path(str(row.get("manifest", ""))),
        )


def audit(
    entries: Iterable[AppEntry], compare: Callable[[AppEntry], Sequence[str]]
) -> list[AuditFinding]:
    """전체를 돌며 차이를 모은다. 이상 없는 봇은 결과에 안 담는다.

    한 봇의 조회 실패가 나머지를 멎게 하면 그 뒤 봇들은 조회 실패와 이상
    없음이 구분되지 않는다. 그래서 예외는 그 봇의 소견으로만 남긴다.
    """
    findings: list[AuditFinding] = []
    for entry in entries:
        try:
            differences = tuple(compare(entry))
        except Exception as exc:  # noqa: BLE001 — 한 봇의 실패가 순회를 끊으면 안 된다
            findings.append(AuditFinding(name=entry.name, differences=(), error=str(exc)))
            continue
        if differences:
            findings.append(AuditFinding(name=entry.name, differences=differences))
    return findings
