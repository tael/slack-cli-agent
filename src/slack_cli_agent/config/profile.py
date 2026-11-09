"""A single bot's definition — the boundary that keeps org-specific values out of code."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar

from ..core.errors import ConfigError
from ..core.secrets import contains_secret, redact
from .paths import StatePaths
from .secret_ref import resolve_mapping

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


KNOWN_KEYS = frozenset(
    {
        "name", "display_name", "primary_engine", "fallback_engine",
        "state_dir", "work_root", "data_dir", "attach_dir", "launch_label",
        "owner_user_id", "controller_user_ids", "troubleshoot_channel", "owner_dm", "credentials_file",
        "plugins", "usage_check_command", "agent_greeting", "agent_prompts",
        "settings", "mcp_servers", "domain_file", "light_workdir", "airflow_ctl", "peer",
        "delegation_ledger", "delegation_by", "delegation_record", "delegation_reply_only",
        "extra_readable_dirs",
    }
)
"""Top-level keys `from_dict` reads. Anything else is dropped, so a mistyped
key turns its feature off while boot still succeeds (sca-4dr)."""


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
    #: 같은 엔진 안에서 모델 하나가 일시 장애일 때 순서대로 시도할 대체 모델들.
    #: 비어 있으면(기본) 모델 폴백을 쓰지 않고 model 하나로만 돈다. 계정 한도는
    #: 모델을 바꿔도 같으므로 이 목록은 모델 단위 장애에만 쓰인다 (B9/B17).
    model_fallbacks: tuple[str, ...] = ()
    #: 부검(dango)에만 쓰는 모델. 비어 있으면 model_for_owner() 다. 마메치
    #: bot_profile.postmortem_model 과 같은 규칙이다 (A13).
    postmortem_model: str = ""
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
            model_fallbacks=tuple(str(m) for m in (data.get("model_fallbacks") or [])),
            postmortem_model=str(data.get("postmortem_model", "")),
            options=dict(data.get("options") or {}),
            home_dir=Path(str(home_dir)).expanduser() if home_dir else None,
        )

    def model_for_owner(self) -> str:
        return self.model_owner or self.model

    def model_for_postmortem(self) -> str:
        return self.postmortem_model or self.model_for_owner()


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
    #: Env vars set from this turn's context rather than a fixed value, keyed
    #: env-var-name -> context key. Three keys are honored: `can_act` sets the
    #: var to "1" on an action turn and drops it on a read-only one (the write
    #: gate); `is_owner` sets the var to "1" on an owner turn and drops it
    #: otherwise (the owner override that lifts a guard); `channel` sets the var
    #: to this turn's opaque channel token and drops it when the token is empty
    #: (the per-channel render guard); `turn_id` sets the var to this turn's
    #: "channel:ts" and drops it when absent (C10: a server's audit row names
    #: the request that called it -- Codex hands MCP servers none of its own
    #: env, so TURN_ID never reaches them otherwise). The var names and the servers are
    #: organization config; the gating is the common skeleton for mametchi's
    #: per-turn WRITE_ENABLED, MAMETCHI_POST_ANY, and MAMETCHI_POST_CHANNEL env.
    turn_env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    disabled: bool = False
    disabled_tools: tuple[str, ...] = ()

    #: Turn-context keys turn_env may point at. Kept small on purpose: an
    #: unknown key is a profile typo that would silently never fire. `can_act`
    #: is the tool-axis write gate and `is_owner` the trust-axis owner override —
    #: both boolean gates the engine derives from the request; `channel` is the
    #: opaque per-turn channel token the caller supplies through
    #: EngineRequest.turn_context.
    _TURN_ENV_KEYS: ClassVar[frozenset[str]] = frozenset(
        {"can_act", "is_owner", "channel", "turn_id"}
    )

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> McpServerSpec:
        command = str(data.get("command") or "")
        url = str(data.get("url") or "")
        if command and url:
            raise ConfigError(f"MCP 서버 {name}: command 와 url 을 동시에 줄 수 없다")
        if not command and not url:
            raise ConfigError(f"MCP 서버 {name}: command(로컬 실행) 또는 url(원격) 중 하나가 필요하다")
        cwd = data.get("cwd")
        turn_env = {str(k): str(v) for k, v in (data.get("turn_env") or {}).items()}
        for var, context_key in turn_env.items():
            if context_key not in cls._TURN_ENV_KEYS:
                allowed = ", ".join(sorted(cls._TURN_ENV_KEYS))
                raise ConfigError(
                    f"MCP 서버 {name}: turn_env 의 {var} 가 가리키는 턴 맥락 "
                    f"'{context_key}' 을 모른다. 쓸 수 있는 값: {allowed}"
                )
        return cls(
            name=name,
            command=command,
            args=tuple(str(a) for a in (data.get("args") or ())),
            env=dict(data.get("env") or {}),
            turn_env=turn_env,
            cwd=Path(str(cwd)).expanduser() if cwd else None,
            url=url,
            headers=dict(data.get("headers") or {}),
            disabled=bool(data.get("disabled", False)),
            disabled_tools=tuple(str(t) for t in (data.get("disabled_tools") or ())),
        )

    def turn_env_for(
        self,
        *,
        can_act: bool,
        is_owner: bool = False,
        turn_context: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """This turn's env additions for the server's process, by turn context.

        `can_act` and `is_owner` are boolean gates: each sets its var to "1" when
        true and omits it otherwise, so a server reading the var stays in its
        default (write gate closed, override not applied) on the opposite turn.
        They are independent — a read-only owner turn still lifts an owner
        override. `channel` sets its var to the matching token from
        `turn_context` and omits it when that token is empty or absent, so a
        server using the var as a per-channel render guard falls back to its
        default. A remote server has no process to receive env, so callers skip
        it the same way they skip `env`.
        """
        ctx = turn_context or {}
        gates = {"can_act": can_act, "is_owner": is_owner}
        out: dict[str, str] = {}
        for var, context_key in self.turn_env.items():
            if context_key in gates:
                if gates[context_key]:
                    out[var] = "1"
            else:
                value = ctx.get(context_key, "")
                if value:
                    out[var] = value
        return out

    @property
    def is_remote(self) -> bool:
        return bool(self.url)

    def resolved_env(self, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        return resolve_mapping(self.env, where=f"MCP 서버 {self.name}", environ=environ)

    def resolved_headers(self, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        return resolve_mapping(self.headers, where=f"MCP 서버 {self.name}", environ=environ)


@dataclass(frozen=True)
class AgentPrompt:
    """One suggested prompt in the agent panel. `title` is the button label,
    `message` is what gets sent when it is pressed."""

    title: str
    message: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentPrompt:
        title = str(data.get("title") or "").strip()
        message = str(data.get("message") or "").strip() or title
        if not title:
            raise ConfigError("제안 프롬프트에 title 이 없다")
        return cls(title=title, message=message)


@dataclass(frozen=True)
class PeerProfile:
    """Where a sibling bot lives, so this bot can revive it on the owner's word.

    A bot that has gone down cannot announce its own death; the owner asks the
    survivor, which then needs — in words — where the peer's state, pid and logs
    are and how to restart it. Only org-specific locations live here; the
    guidance text that uses them is common code (PeerRecoverySection). See
    mametchi peer_recovery_note, bot.py:2614.
    """

    name: str
    display_name: str
    state_dir: Path
    work_root: Path
    launch_label: str
    # Process layout. The default is an sca peer: one launchd job per service
    # (<label>.worker, <label>.ingress) and <state_dir>/<service>.pid. A peer on
    # another runtime (mametchi bot.py: one label, bot.pid, logs/stderr.log,
    # restart.sh) sets services to [] and names its own files and command, so
    # the guidance never points at labels or files that do not exist (C12).
    services: tuple[str, ...] = ("worker", "ingress")
    pid_files: tuple[Path, ...] = ()
    log_files: tuple[Path, ...] = ()
    restart_command: str = ""

    def launch_labels(self) -> tuple[str, ...]:
        if not self.services:
            return (self.launch_label,)
        return tuple(f"{self.launch_label}.{svc}" for svc in self.services)

    def resolved_pid_files(self) -> tuple[Path, ...]:
        if self.pid_files:
            return self.pid_files
        if not self.services:
            return (self.state_dir / "bot.pid",)
        return tuple(self.state_dir / f"{svc}.pid" for svc in self.services)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PeerProfile:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ConfigError("peer 블록에 name 이 없다")
        raw_state = data.get("state_dir")
        if not raw_state:
            raise ConfigError("peer 블록에 state_dir 이 없다")
        state_dir = Path(str(raw_state)).expanduser()
        work_raw = data.get("work_root")
        work_root = Path(str(work_raw)).expanduser() if work_raw else state_dir / "work"
        return cls(
            name=name,
            display_name=str(data.get("display_name") or name),
            state_dir=state_dir,
            work_root=work_root,
            launch_label=str(data.get("launch_label") or f"local.{name}"),
            services=(
                tuple(_peer_str_list(data, "services")) if "services" in data else ("worker", "ingress")
            ),
            pid_files=tuple(_peer_path(state_dir, v) for v in _peer_str_list(data, "pid_files")),
            log_files=tuple(_peer_path(state_dir, v) for v in _peer_str_list(data, "log_files")),
            restart_command=str(data.get("restart_command") or "").strip(),
        )


def _peer_str_list(data: Mapping[str, Any], key: str) -> list[str]:
    raw = data.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise ConfigError(f"peer 블록의 {key} 는 빈 값 없는 문자열 목록이어야 한다")
    return [v.strip() for v in raw]


def _peer_path(state_dir: Path, value: str) -> Path:
    """Relative entries are under the peer's state_dir."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else state_dir / path


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
    #: Bot accounts whose requests this bot accepts. Acceptance only: their
    #: turns are GENERAL unless delegation_ledger vouches for the message.
    controller_user_ids: frozenset[str] = frozenset()
    #: The delegating bot's ledger file (mametchi ~/.mametchi/delegations.json
    #: format), read only. None means no controller turn is ever delegated.
    delegation_ledger: Path | None = None
    #: The `by` a ledger row must carry (mametchi writes "mametchi").
    #: Required with delegation_ledger.
    delegation_by: str = ""
    #: Ledger this bot writes when an owner turn hands work to another bot
    #: (mametchi delegation.record, A8). Rows carry `by` = this profile's
    #: name. None means this bot never delegates.
    delegation_record: Path | None = None
    #: Accept from delegation_ledger only replies to work this bot handed out:
    #: kind=reply rows whose origin_ts is in delegation_record (mametchi
    #: lookup_reply). False keeps the plain delegation check.
    delegation_reply_only: bool = False
    plugins: tuple[str, ...] = ()
    #: Operator-supplied usage check command. Empty means the check is off.
    usage_check_command: tuple[str, ...] = ()
    #: Agent panel greeting. Empty means post nothing when a panel thread opens.
    agent_greeting: str = ""
    #: Agent panel suggested prompts. Empty leaves whatever the app manifest declares.
    agent_prompts: tuple[AgentPrompt, ...] = ()
    settings_override: Mapping[str, Any] = field(default_factory=dict)
    # Empty by default — an installed bot must not carry another bot's MCP
    # servers along. See docs/패키징-경계.md.
    mcp_servers: Mapping[str, McpServerSpec] = field(default_factory=dict)
    # Path only. Defaults to StatePaths.credentials when unset.
    credentials_file: Path | None = None
    # Company domain knowledge appended to the persona when the work directory
    # is a real repo outside home (see pipeline._domain_applies). The path is an
    # org-specific value, so it lives in the profile, not the package. None
    # leaves the persona with no domain file, which is every bot that has none.
    domain_file: Path | None = None
    # Lighter work directory for non-owner turns in a light_context channel
    # (A11, mametchi workdir_for bot.py:282). Running there drops the heavy home
    # CLAUDE.md and the company domain file, so chat-only channels don't pay for
    # domain context the owner alone needs. An org-specific path, so it lives in
    # the profile; None leaves light_context inert and every turn uses the full
    # work directory.
    light_workdir: Path | None = None
    # Path to the Airflow control script that fills <<AIRFLOW_CTL>> in the
    # Airflow note. Org-specific, so it lives in the profile. None leaves the
    # slot unfilled, which only matters to a bot whose prompts use it.
    airflow_ctl: Path | None = None
    # Directories outside the state dir every engine call may also read, such as
    # API spec repos a helpdesk channel answers from (mametchi bot.py:473-477).
    # Org-specific paths, so they live in the profile.
    extra_readable_dirs: tuple[Path, ...] = ()
    # A sibling bot this one can revive on the owner's word. None means there is
    # no peer, and the revival guidance is left out of the prompt entirely
    # (PeerRecoverySection). Org-specific locations, so it lives in the profile.
    peer: PeerProfile | None = None
    # The file this was read from. None when built straight from a dict, which
    # is every test and the init command -- checks about the file itself have
    # nothing to look at then.
    source_file: Path | None = None

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
                return cls._read(candidate)
        # Redacted here too: this runs before any profile is read, so the
        # value check cannot have seen these strings (코덱스 리뷰).
        searched = ", ".join(redact(str(p)) for p in search_paths)
        raise ConfigError(f"프로필 {redact(name)} 을 찾지 못했다. 검색 경로: {searched}")

    @classmethod
    def _read(cls, path: Path) -> Profile:
        """Names the file in every failure.

        `from_dict` only sees the parsed dict, so its messages say what is
        wrong but not where. With several search paths that is not enough to
        find the file to edit -- the profile `init` writes needs values filled
        in, and that error is the first one a fresh install hits.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"프로필 파일을 읽지 못했다 : {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"프로필 파일이 올바른 JSON 이 아니다 : {path} ({exc})") from exc
        try:
            profile = cls.from_dict(data)
        except ConfigError as exc:
            raise ConfigError(f"{exc} : {path}") from exc
        return replace(profile, source_file=path)

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
        # Without the delegator's name the ledger would accept a row any bot
        # wrote into the file (mametchi lookup checks by == DELEGATOR).
        if data.get("delegation_ledger") and not data.get("delegation_by"):
            raise ConfigError("delegation_ledger 를 쓰려면 delegation_by 에 위임자 이름을 적는다")
        if data.get("delegation_reply_only") is True and not data.get("delegation_record"):
            raise ConfigError("delegation_reply_only 를 쓰려면 delegation_record 에 내 위임 원장을 적는다")
        extra_readable_dirs = tuple(
            Path(str(d)).expanduser() for d in (data.get("extra_readable_dirs") or ()) if d
        )
        for d in extra_readable_dirs:
            if not d.is_absolute():
                raise ConfigError(f"extra_readable_dirs 는 절대경로여야 한다 : {redact(str(d))}")
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
            controller_user_ids=frozenset(str(u) for u in (data.get("controller_user_ids") or ()) if u),
            credentials_file=credentials_file or None,
            domain_file=(
                Path(str(data["domain_file"])).expanduser()
                if data.get("domain_file") else None
            ),
            light_workdir=(
                Path(str(data["light_workdir"])).expanduser()
                if data.get("light_workdir") else None
            ),
            airflow_ctl=(
                Path(str(data["airflow_ctl"])).expanduser()
                if data.get("airflow_ctl") else None
            ),
            delegation_ledger=(
                Path(str(data["delegation_ledger"])).expanduser()
                if data.get("delegation_ledger") else None
            ),
            delegation_by=str(data.get("delegation_by") or ""),
            extra_readable_dirs=extra_readable_dirs,
            delegation_reply_only=data.get("delegation_reply_only") is True,
            delegation_record=(
                Path(str(data["delegation_record"])).expanduser()
                if data.get("delegation_record") else None
            ),
            plugins=tuple(data.get("plugins") or ()),
            usage_check_command=tuple(str(part) for part in (data.get("usage_check_command") or ())),
            agent_greeting=str(data.get("agent_greeting") or ""),
            agent_prompts=tuple(
                AgentPrompt.from_dict(p) for p in (data.get("agent_prompts") or ())
            ),
            settings_override=dict(data.get("settings") or {}),
            mcp_servers=mcp_servers,
            peer=(PeerProfile.from_dict(data["peer"]) if data.get("peer") else None),
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
