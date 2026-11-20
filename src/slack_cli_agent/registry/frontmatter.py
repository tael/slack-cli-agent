"""SOURCE.md·WORKFLOW.md 의 프론트매터 파서.

yaml 의존을 늘리지 않으려고 이 파일 형식이 쓰는 부분만 받는다.

    ---
    key: 문자열            # 따옴표는 선택
    flag: true             # 스칼라는 전부 문자열이다. 불리언 변환은 쓰는 쪽이 한다
    caps: [a.b, c]         # 인라인 목록
    more:                  # 블록 목록
      - x
    ---
    본문

그 밖의 형식(중첩 매핑, 여러 줄 문자열, 앵커)은 줄 번호와 함께 거부한다.
조용히 다르게 읽는 것보다 항목 하나를 거부하는 편이 낫다.
"""

from __future__ import annotations

import re

from .common import RegistryError

Value = str | list[str]

_FENCE = "---"
_KEY = re.compile(r"^([a-z][a-z0-9_]*)\s*:(?:\s+(.*)|\s*)$")
_ITEM = re.compile(r"^\s+-\s+(.*?)\s*$")


class FrontmatterError(RegistryError):
    pass


def _scalar(raw: str) -> str:
    """따옴표를 벗긴 문자열. `health: true` 가 명령 true 일 수 있어 불리언으로 안 바꾼다."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _inline_list(raw: str, number: int) -> list[str]:
    if not raw.endswith("]"):
        raise FrontmatterError(f"{number}번째 줄: 목록의 닫는 ] 가 없다")
    inner = raw[1:-1].strip()
    return [_scalar(p) for p in inner.split(",") if p.strip()] if inner else []


def parse_document(text: str) -> tuple[dict[str, Value], str]:
    """(프론트매터 dict, 본문). 형식이 틀리면 FrontmatterError."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise FrontmatterError("프론트매터가 없다. 첫 줄이 --- 여야 한다")
    meta: dict[str, Value] = {}
    pending: str | None = None
    end = None
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == _FENCE:
            end = index
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _ITEM.match(line)
        if item:
            if pending is None:
                raise FrontmatterError(f"{index}번째 줄: 목록 항목이 키 아래에 있지 않다")
            current = meta[pending]
            meta[pending] = ([*current, _scalar(item.group(1))] if isinstance(current, list)
                             else [_scalar(item.group(1))])
            continue
        key = _KEY.match(line)
        if not key:
            raise FrontmatterError(f"{index}번째 줄을 읽지 못했다: {line.strip()[:40]!r} (키 형식 오류)")
        name, raw = key.group(1), (key.group(2) or "").strip()
        if name in meta:
            raise FrontmatterError(f"{index}번째 줄: 키 중복 {name}")
        if raw.startswith("["):
            meta[name] = _inline_list(raw, index)
            pending = None
        else:
            meta[name] = _scalar(raw) if raw else ""
            pending = name if not raw else None
    if end is None:
        raise FrontmatterError("닫는 --- 가 없다")
    return meta, "\n".join(lines[end:]) + ("\n" if lines[end:] else "")
