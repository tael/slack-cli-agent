"""A single bot's definition — the boundary that keeps org-specific values out of code."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..core.secrets import contains_secret, redact
from .paths import StatePaths

# 저장소에 올리는 견본 프로필. 실제 봇이 아니므로 검색에서 뺀다.
EXAMPLE_SUFFIX = ".example.json"

# The profile may point at a credentials file but never hold a token itself.
# .gitignore only reduces accidental commits; it does not stop a forced add, a
# copy, a backup, or the web console reading and rewriting the profile
# (sca-jl4.4).
_SECRET_KEY_SUFFIX = "_token"
_SECRET_KEY_NAME = "token"
# mcp_servers.<id>.env is a documented pass-through to a third-party process
# that has no other way to receive its own credentials, so key names are not
# checked inside it. Slack-shaped values still are (sca-jl4.4).
_SECRET_KEY_EXEMPT_BLOCK = "mcp_servers"


# A profile name becomes a filename. Anything outside this set either escapes
# the search directory or carries something that must not be written to disk.
_PROFILE_NAME_RE = re.compile(r"[\w.-]+", re.UNICODE)


def validate_profile_name(name: str) -> str:
    """Every entry point that turns a name into `<name>.json` goes through
    here — the CLI, and the web console which takes it straight from a URL
    (코덱스 리뷰)."""
    if not name:
        raise ConfigError("프로필 이름이 비어 있다")
    if not _PROFILE_NAME_RE.fullmatch(name) or name.startswith(".") or ".." in name:
        raise ConfigError(f"프로필 이름에 쓸 수 없는 문자가 있다 : {redact(name)}")
    if contains_secret(name):
        raise ConfigError(f"프로필 이름에 토큰이 들어 있다 : {redact(name)}")
    return name


@dataclass(frozen=True)
class EngineSpec:
    """How one engine is invoked. Grouped as a block so adding an engine
    doesn't grow the profile's top-level keys."""

    type: str
    binary: Path
    model: str
    model_owner: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    # Engine-specific home path (e.g. CODEX_HOME). Keeps sessions/auth
    # separated per bot, so it can't collide with the user's own home.
    home_dir: Path | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EngineSpec:
        for key in ("type", "binary", "model"):
            if not data.get(key):
                raise ConfigError(f"엔진 설정에 {key} 가 없다")
        home_dir = data.get("home_dir")
        return cls(
            type=str(data["type"]),
            binary=Path(str(data["binary"])).expanduser(),
            model=str(data["model"]),
            model_owner=str(data.get("model_owner", "")),
            options=dict(data.get("options") or {}),
            home_dir=Path(str(home_dir)).expanduser() if home_dir else None,
        )

    def model_for_owner(self) -> str:
        return self.model_owner or self.model


@dataclass(frozen=True)
class McpServerSpec:
    """One MCP server entry, keyed by name in `Profile.mcp_servers`.

    Fields use our own naming, not any single engine CLI's. Per-engine
    conversion (e.g. agy's serverUrl/disabledTools) is a separate concern.
    """

    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    disabled: bool = False
    disabled_tools: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> McpServerSpec:
        command = str(data.get("command") or "")
        url = str(data.get("url") or "")
        if command and url:
            raise ConfigError(f"MCP 서버 {name}: command 와 url 을 동시에 줄 수 없다")
        if not command and not url:
            raise ConfigError(f"MCP 서버 {name}: command(로컬 실행) 또는 url(원격) 중 하나가 필요하다")
        cwd = data.get("cwd")
        return cls(
            name=name,
            command=command,
            args=tuple(str(a) for a in (data.get("args") or ())),
            env=dict(data.get("env") or {}),
            cwd=Path(str(cwd)).expanduser() if cwd else None,
            url=url,
            headers=dict(data.get("headers") or {}),
            disabled=bool(data.get("disabled", False)),
            disabled_tools=tuple(str(t) for t in (data.get("disabled_tools") or ())),
        )

    @property
    def is_remote(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class Profile:
    name: str
    display_name: str
    primary_engine: EngineSpec
    fallback_engine: EngineSpec | None
    state_dir: Path
    work_root: Path
    data_dir: Path
    attach_dir: Path
    launch_label: str
    owner_user_id: str
    troubleshoot_channel: str
    owner_dm: str = ""
    plugins: tuple[str, ...] = ()
    settings_override: Mapping[str, Any] = field(default_factory=dict)
    # Empty by default — an installed bot must not carry another bot's MCP
    # servers along. See docs/패키징-경계.md.
    mcp_servers: Mapping[str, McpServerSpec] = field(default_factory=dict)
    # Path only. Defaults to StatePaths.credentials when unset.
    credentials_file: Path | None = None

    @property
    def paths(self) -> StatePaths:
        return StatePaths(self.state_dir)

    @property
    def roster_file(self) -> Path:
        """Account handle to person-name table."""
        return self.data_dir / "roster.md"

    @classmethod
    def discover(cls, search_paths: Sequence[Path]) -> list[str]:
        """검색 경로에 있는 프로필 이름을 정렬해 돌려준다.

        `*.example.json` 은 저장소에 올리는 견본이라 실제 봇이 아니다
        (`docs/패키징-경계.md`). 걸러내지 않으면 웹 콘솔 봇 목록에
        `example.example` 이 실제 봇처럼 섞인다.
        """
        found: set[str] = set()
        for base in search_paths:
            if not Path(base).is_dir():
                continue
            for path in Path(base).glob("*.json"):
                if path.name.endswith(EXAMPLE_SUFFIX):
                    continue
                # A file placed by hand can carry anything. load() would
                # reject such a name anyway, so listing it only exposes it
                # in the web console (코덱스 리뷰).
                try:
                    found.add(validate_profile_name(path.stem))
                except ConfigError:
                    continue
        return sorted(found)

    @classmethod
    def load(cls, name: str, search_paths: Sequence[Path]) -> Profile:
        """Finds `<name>.json` on search_paths in order and uses the first match."""
        name = validate_profile_name(name)
        for base in search_paths:
            candidate = base / f"{name}.json"
            if candidate.is_file():
                return cls.from_dict(json.loads(candidate.read_text(encoding="utf-8")))
        # Redacted here too: this runs before any profile is read, so the
        # value check cannot have seen these strings (코덱스 리뷰).
        searched = ", ".join(redact(str(p)) for p in search_paths)
        raise ConfigError(f"프로필 {redact(name)} 을 찾지 못했다. 검색 경로: {searched}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Profile:
        cls._reject_secrets(data)

        name = data.get("name")
        if not name:
            raise ConfigError("프로필에 name 이 없다")

        engine_block = data.get("primary_engine")
        if not isinstance(engine_block, Mapping):
            raise ConfigError("프로필에 primary_engine 블록이 없다")

        state_dir = Path(str(data.get("state_dir") or f"~/.{name}")).expanduser()
        credentials_file = data.get("credentials_file")
        if credentials_file:
            credentials_file = Path(str(credentials_file)).expanduser()
            # Rejected here, not when the resolver is built: the web console
            # saves profiles and would otherwise store an unbootable one
            # (코덱스 리뷰).
            if not credentials_file.is_absolute():
                raise ConfigError(
                    f"credentials_file 은 절대경로여야 한다 : {redact(str(credentials_file))}"
                )
        fallback = data.get("fallback_engine")
        mcp_servers_block = data.get("mcp_servers") or {}
        mcp_servers = {
            str(server_name): McpServerSpec.from_dict(str(server_name), server_data)
            for server_name, server_data in mcp_servers_block.items()
        }

        return cls(
            name=str(name),
            display_name=str(data.get("display_name") or name),
            primary_engine=EngineSpec.from_dict(engine_block),
            fallback_engine=EngineSpec.from_dict(fallback) if fallback else None,
            state_dir=state_dir,
            work_root=cls._under(data, "work_root", state_dir, "work"),
            data_dir=cls._under(data, "data_dir", state_dir, "data"),
            attach_dir=cls._under(data, "attach_dir", state_dir, "attachments"),
            launch_label=str(data.get("launch_label") or f"local.{name}"),
            owner_user_id=str(data.get("owner_user_id", "")),
            troubleshoot_channel=str(data.get("troubleshoot_channel", "")),
            owner_dm=str(data.get("owner_dm", "")),
            credentials_file=credentials_file or None,
            plugins=tuple(data.get("plugins") or ()),
            settings_override=dict(data.get("settings") or {}),
            mcp_servers=mcp_servers,
        )

    @staticmethod
    def _reject_secrets(data: Mapping[str, Any]) -> None:
        """Nested blocks are walked too: `settings` is kept verbatim, so a
        top-level-only check let {"settings": {"bot_token": ...}} through
        (코덱스 리뷰). The offending key is not echoed — a token pasted as a
        key would print itself.
        """
        for path, reason in Profile._secret_findings(data, (), check_keys=True):
            raise ConfigError(
                f"프로필에 토큰을 넣을 수 없다. credentials_file 로 자격 파일을 가리켜라 : "
                f"{redact(path) or '최상위'} 의 {reason}"
            )

    @staticmethod
    def _secret_findings(
        node: Any, path: tuple[str, ...], *, check_keys: bool
    ) -> Iterator[tuple[str, str]]:
        if isinstance(node, str):
            if contains_secret(node):
                yield ".".join(path[:-1]), "값"
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                text = str(key)
                low = text.lower()
                # Lowercased: SLACK_BOT_TOKEN is the real environment variable
                # name and is easy to copy in as-is (코덱스 리뷰).
                if check_keys and (low.endswith(_SECRET_KEY_SUFFIX) or low == _SECRET_KEY_NAME):
                    yield ".".join(path), "키 이름"
                    continue
                if contains_secret(text):
                    yield ".".join(path), "키 이름"
                    continue
                nested = check_keys and not (not path and text == _SECRET_KEY_EXEMPT_BLOCK)
                yield from Profile._secret_findings(value, (*path, text), check_keys=nested)
            return
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for item in node:
                yield from Profile._secret_findings(item, path, check_keys=check_keys)

    @staticmethod
    def _under(data: Mapping[str, Any], key: str, state_dir: Path, name: str) -> Path:
        given = data.get(key)
        if given:
            return Path(str(given)).expanduser()
        return state_dir / name

    def validate(self) -> list[str]:
        """Config problems; an empty list means the profile is valid."""
        problems: list[str] = []
        if not self.owner_user_id:
            problems.append("owner_user_id 가 비어 있다")
        if not self.troubleshoot_channel:
            problems.append("troubleshoot_channel 이 비어 있다")
        for label, spec in self._engines():
            if spec.binary.is_absolute() and not spec.binary.exists():
                problems.append(f"{label} 엔진 실행 파일이 없다: {spec.binary}")
        if self.fallback_engine and self.fallback_engine.type == self.primary_engine.type:
            problems.append("폴백 엔진이 1차 엔진과 같은 종류다")
        for server in self.mcp_servers.values():
            if server.cwd is not None and server.cwd.is_absolute() and not server.cwd.exists():
                problems.append(f"MCP 서버 {server.name} 의 cwd 가 없다: {server.cwd}")
        return problems

    def _engines(self) -> list[tuple[str, EngineSpec]]:
        pairs = [("1차", self.primary_engine)]
        if self.fallback_engine:
            pairs.append(("2차", self.fallback_engine))
        return pairs
