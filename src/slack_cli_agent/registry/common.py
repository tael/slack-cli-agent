"""레지스트리 로더 공통부: 항목 디렉터리 순회, 거부 기록, 이름 규칙, 비밀 검출."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

#: 파일에 적으면 안 되는 비밀값의 대표 형태. 완전한 검출이 아니라 실수로 붙여
#: 넣은 토큰을 거르는 최소 장치다. 비밀은 환경변수나 키체인에서 읽는다.
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)


class RegistryError(ValueError):
    """한 항목을 거부하는 사유. 메시지가 그대로 사용자에게 나간다."""


@dataclass(frozen=True)
class Rejection:
    """거부된 항목. name 은 디렉터리 이름이다."""

    name: str
    path: Path
    reason: str


def reject_secret(text: str) -> None:
    """프론트매터·본문 어디든 비밀값 형태가 있으면 그 항목을 거부한다."""
    if any(p.search(text) for p in _SECRET_PATTERNS):
        raise RegistryError("비밀값으로 보이는 문자열이 있다. 환경변수나 키체인으로 옮긴다")


def check_name(name: str, dir_name: str, label: str) -> None:
    if not NAME_RE.match(name):
        raise RegistryError(f"{label} 형식 오류: {name!r} (소문자 영숫자와 - _ 만)")
    if name != dir_name:
        raise RegistryError(f"{label} {name!r} 가 디렉터리 이름 {dir_name!r} 와 다르다")


def require_text(meta: Mapping[str, object], key: str, *, required: bool = True) -> str:
    if key not in meta or meta[key] == "":
        if required:
            raise RegistryError(f"{key} 가 필요하다")
        return ""
    value = meta[key]
    if not isinstance(value, str):
        raise RegistryError(f"{key} 는 문자열이어야 한다")
    return value


def optional_bool(meta: Mapping[str, object], key: str, default: bool) -> bool:
    if key not in meta:
        return default
    value = meta[key]
    if value not in ("true", "false"):
        raise RegistryError(f"{key} 는 true 나 false 여야 한다: {value!r}")
    return value == "true"


def require_list(meta: Mapping[str, object], key: str) -> tuple[str, ...]:
    if key not in meta:
        raise RegistryError(f"{key} 가 필요하다")
    value = meta[key]
    if not isinstance(value, list):
        raise RegistryError(f"{key} 는 [a, b] 형태의 목록이어야 한다")
    return tuple(value)


def check_keys(meta: Mapping[str, object], allowed: frozenset[str]) -> None:
    extra = sorted(set(meta) - allowed)
    if extra:
        raise RegistryError(f"알 수 없는 키: {', '.join(extra)} (오타면 조용히 무시되므로 거부한다)")


def check_capability_names(names: tuple[str, ...]) -> None:
    for name in names:
        if not CAPABILITY_RE.match(name):
            raise RegistryError(f"능력 이름 형식 오류: {name!r} (소문자.점.밑줄만)")


T = TypeVar("T")


def scan_directory(
    root: Path, filename: str, parse: Callable[[str, str, Path], T]
) -> tuple[list[T], list[Rejection], str]:
    """root 아래 항목 디렉터리마다 filename 을 읽어 parse 한다.

    반환은 (성공 항목, 거부 목록, 전체 오류). 전체 오류는 root 자체를 못 읽을 때만
    채워지고 그때 나머지는 비어 있다. 항목 하나의 오류는 그 항목만 거부한다.
    점으로 시작하는 이름과 디렉터리가 아닌 것은 건너뛴다.
    """
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError as exc:
        return [], [], f"{root} 를 읽지 못했다: {exc.strerror or exc}"
    items: list[T] = []
    rejected: list[Rejection] = []
    for directory in entries:
        path = directory / filename
        try:
            if not path.is_file():
                raise RegistryError(f"{filename} 가 없다")
            text = path.read_text(encoding="utf-8")
            items.append(parse(text, directory.name, path))
        except RegistryError as exc:
            rejected.append(Rejection(directory.name, path, str(exc)))
        except (OSError, UnicodeDecodeError) as exc:
            rejected.append(Rejection(directory.name, path, f"파일을 읽지 못했다: {exc}"))
    return items, rejected, ""
