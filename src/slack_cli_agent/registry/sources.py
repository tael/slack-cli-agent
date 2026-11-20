"""소스 레지스트리: `sources/<이름>/SOURCE.md` 로더와 검증기.

프론트매터 필드

    name          디렉터리 이름과 같아야 한다
    description   무엇이 있고 언제 뒤지는지
    access        CLI 명령, 경로, 또는 `builtin:<도구,...>`(엔진 내장 도구)
    kind          read | write | send
    capabilities  능력 이름 목록 (예 web.search, local.files)
    enabled       true | false (생략하면 true)
    health        짧은 점검 명령 (생략하면 access 의 경로·실행파일 존재 확인)

잘못된 항목은 그 항목만 거부하고 사유를 `SourceLoad.rejected` 에 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import (
    Rejection,
    RegistryError,
    check_capability_names,
    check_keys,
    check_name,
    optional_bool,
    require_list,
    reject_secret,
    require_text,
    scan_directory,
)
from .frontmatter import parse_document

SOURCE_FILE = "SOURCE.md"
KINDS = ("read", "write", "send")
_KEYS = frozenset({"name", "description", "access", "kind", "capabilities", "enabled", "health"})


class SourceError(RegistryError):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    access: str
    kind: str
    capabilities: tuple[str, ...]
    enabled: bool
    health: str
    path: Path


@dataclass(frozen=True)
class SourceLoad:
    sources: tuple[Source, ...]
    rejected: tuple[Rejection, ...]
    #: 비어 있지 않으면 레지스트리 전체를 못 읽은 것이다(루트 디렉터리 오류).
    error: str = ""


def parse_source(text: str, dir_name: str, path: Path) -> Source:
    try:
        reject_secret(text)
        meta, _ = parse_document(text)
        check_keys(meta, _KEYS)
        name = require_text(meta, "name")
        check_name(name, dir_name, "name")
        kind = require_text(meta, "kind")
        if kind not in KINDS:
            raise SourceError(f"kind 는 {'|'.join(KINDS)} 중 하나여야 한다: {kind!r}")
        caps = require_list(meta, "capabilities")
        if not caps:
            raise SourceError("capabilities 가 비어 있다. 능력 이름이 하나 이상 필요하다")
        check_capability_names(caps)
        enabled = optional_bool(meta, "enabled", True)
        return Source(
            name=name,
            description=require_text(meta, "description"),
            access=require_text(meta, "access"),
            kind=kind,
            capabilities=caps,
            enabled=enabled,
            health=require_text(meta, "health", required=False),
            path=path,
        )
    except SourceError:
        raise
    except RegistryError as exc:
        raise SourceError(str(exc)) from exc


def load_sources(root: Path) -> SourceLoad:
    items, rejected, error = scan_directory(root, SOURCE_FILE, parse_source)
    return SourceLoad(tuple(items), tuple(rejected), error)
