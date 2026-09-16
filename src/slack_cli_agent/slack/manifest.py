"""Slack app manifests as an object, plus the contract every bot's app must meet.

An app's settings are as much a part of how the bot behaves as its code, but
they live in Slack rather than in this repo — so nothing here could see them.
That gap is what sca-1v7 was: `features.app_home` was missing on all three
apps, Slack read that as "Messages tab off", and the owner could not open a DM
at all. Every unit test passed the whole time, because the code path
(`listener.py`, `channel_type == "im"`) was correct.

Two callers read the same contract so they cannot drift:

- the offline test over the manifests checked into `slack-apps/`
- `tools/slack-app.py diff`, which exports an app's live manifest and
  compares it against its checked-in copy

Deliberately not a preflight check: reading the live manifest needs a network
call and a config-token refresh, and a Slack outage must not stop a bot from
booting (코덱스 리뷰).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

#: `__NAME__` 같은 템플릿 자리. 치환 후에도 남아 있으면 오류다.
_PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


class AppManifest:
    """One Slack app manifest. Reads nested keys without raising on a missing one.

    Wraps the raw mapping rather than parsing it into fields: Slack adds
    manifest keys over time, and a parser that only knows today's keys would
    silently drop whatever it doesn't recognise on the way back out.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    @classmethod
    def from_path(cls, path: Path) -> AppManifest:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def raw(self) -> Mapping[str, Any]:
        return self._data

    #: Placeholders `slack-apps/_template.json` carries, and the keyword each
    #: one is filled from. Declared here rather than in the shell script that
    #: used to do this, so the substitution is reachable by a test (sca-4zy).
    TEMPLATE_SLOTS: ClassVar[tuple[str, ...]] = ("__NAME__", "__DISPLAY__")

    @classmethod
    def from_template(cls, path: Path, *, name: str, display: str) -> AppManifest:
        """Builds a new bot's manifest from the checked-in template.

        `name` is Slack's ASCII username, `display` the name people see.
        They are separate fields and putting one in both slots is what makes
        apps.manifest.update answer bad_username.
        """
        text = path.read_text(encoding="utf-8")
        text = text.replace("__DISPLAY__", display).replace("__NAME__", name)
        # A slot left behind means the template grew a placeholder this method
        # doesn't know, and it would reach Slack verbatim as the app's name.
        leftover = [slot for slot in cls._placeholders(text)]
        if leftover:
            raise ValueError(
                f"템플릿에 채우지 못한 자리가 남았다 : {', '.join(leftover)} — "
                f"{path} 와 AppManifest.TEMPLATE_SLOTS 가 어긋났다"
            )
        return cls(json.loads(text))

    @staticmethod
    def _placeholders(text: str) -> list[str]:
        return sorted(set(_PLACEHOLDER_RE.findall(text)))

    def _dig(self, *keys: str) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, Mapping):
                return None
            node = node.get(key)
        return node

    @property
    def app_home(self) -> Mapping[str, Any]:
        value = self._dig("features", "app_home")
        return value if isinstance(value, Mapping) else {}

    @property
    def bot_events(self) -> tuple[str, ...]:
        return self._as_strings(self._dig("settings", "event_subscriptions", "bot_events"))

    @property
    def bot_scopes(self) -> tuple[str, ...]:
        return self._as_strings(self._dig("oauth_config", "scopes", "bot"))

    @property
    def bot_username(self) -> str:
        """Slack's internal username, not the name people see.

        The visible name is `display_information.name` and may be Korean;
        this one is ASCII-only. Confusing the two is how the export/update
        round trip broke (see ManifestContract).
        """
        value = self._dig("features", "bot_user", "display_name")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _as_strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, str):
            return ()
        return tuple(item for item in value if isinstance(item, str))

    def policy_fingerprint(self) -> str:
        """The parts that must be identical across every bot's app.

        Name, description and colour are per-bot; scopes, events and the
        app-home tabs are policy. Sorted, so the order Slack happens to
        return doesn't read as a difference.
        """
        return json.dumps(
            {
                "app_home": dict(sorted(self.app_home.items())),
                "bot_events": sorted(self.bot_events),
                "bot_scopes": sorted(self.bot_scopes),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class ManifestContract:
    """What every bot's app manifest must contain, and why.

    Each rule here exists because its absence breaks something that no other
    check sees. Keep the reason in the message: the operator reading a
    failure needs to know what stops working, not just which key is missing.
    """

    #: Without this the bot never receives a DM at all.
    REQUIRED_EVENTS: tuple[str, ...] = ("message.im",)

    #: Scope -> what stops working without it. Paired so the failure message
    #: says what breaks rather than only which key is absent, and so adding a
    #: scope forces stating why it is required.
    #:
    #: chat:write belongs here even though it is not DM-specific: a bot that
    #: receives a DM and cannot answer looks, to the owner, exactly like one
    #: whose Messages tab is off (코덱스 리뷰).
    REQUIRED_SCOPES: ClassVar[dict[str, str]] = {
        "im:history": "DM 에서 오간 메시지를 읽을 수 없다",
        "im:read": "DM 채널을 알아볼 수 없다",
        "im:write": "봇이 먼저 DM 을 열 수 없다",
        "chat:write": "DM 을 받아도 답을 보낼 수 없다",
    }

    def violations(self, manifest: AppManifest) -> list[str]:
        """Every rule this manifest breaks. All of them at once, not the first
        one — otherwise fixing a manifest is one round trip per mistake."""
        bad: list[str] = []
        bad.extend(self._app_home_violations(manifest))
        for event in self.REQUIRED_EVENTS:
            if event not in manifest.bot_events:
                bad.append(f"bot_events 에 {event} 가 없다 : 봇이 DM 을 아예 못 받는다")
        for scope, 사유 in self.REQUIRED_SCOPES.items():
            if scope not in manifest.bot_scopes:
                bad.append(f"봇 스코프에 {scope} 가 없다 : {사유}")
        bad.extend(self._username_violations(manifest))
        return bad

    @staticmethod
    def _app_home_violations(manifest: AppManifest) -> list[str]:
        home = manifest.app_home
        bad: list[str] = []
        if home.get("messages_tab_enabled") is not True:
            # Slack treats a missing app_home as "off", so the absent key and
            # an explicit false are the same failure and get the same message.
            bad.append(
                "features.app_home.messages_tab_enabled 가 참이 아니다 : "
                "소유자가 DM 입력창을 열 수 없다"
            )
        if home.get("messages_tab_read_only_enabled") is True:
            bad.append(
                "features.app_home.messages_tab_read_only_enabled 가 참이다 : "
                "DM 탭은 열리지만 보낼 수 없어 꺼진 것과 결과가 같다"
            )
        return bad

    @staticmethod
    def _username_violations(manifest: AppManifest) -> list[str]:
        name = manifest.bot_username
        if not name:
            return ["features.bot_user.display_name 이 없다"]
        if not name.isascii():
            # apps.manifest.export hands back a non-ASCII username that
            # apps.manifest.update then rejects with bad_username, so a
            # manifest read from Slack cannot be written back unchanged.
            return [
                (
                    f"features.bot_user.display_name 이 ASCII 가 아니다 : {name} — "
                    "apps.manifest.update 가 거부한다. 사람이 보는 이름은 "
                    "display_information.name 이므로 이쪽은 ASCII 로 둔다"
                )
            ]
        return []


class ManifestDiff:
    """Compares a checked-in manifest against the one Slack actually holds.

    Asymmetric on purpose: only keys present in the local copy are compared.
    Slack fills in fields we never wrote (`org_deploy_enabled`, a generated
    `long_description`), and counting those as differences would make this
    command report a difference every single time — at which point nobody
    reads it, and a real drift hides among the noise.
    """

    def differences(self, local: AppManifest, remote: AppManifest) -> list[str]:
        """Every difference, not the first, so one run tells the whole story."""
        return self._walk(local.raw, remote.raw, path="")

    def _walk(self, local: Any, remote: Any, *, path: str) -> list[str]:
        if isinstance(local, Mapping):
            return self._walk_mapping(local, remote, path=path)
        if self._is_string_list(local):
            return self._compare_string_list(local, remote, path=path)
        if local != remote:
            return [f"{path} : 정본은 {local!r} 인데 슬랙은 {remote!r} 이다"]
        return []

    def _walk_mapping(self, local: Mapping[str, Any], remote: Any, *, path: str) -> list[str]:
        if not isinstance(remote, Mapping):
            return [f"{path} : 슬랙 쪽에 없다"]
        bad: list[str] = []
        for key, value in local.items():
            child = f"{path}.{key}" if path else key
            if key not in remote:
                bad.append(f"{child} : 슬랙 쪽에 없다")
                continue
            bad.extend(self._walk(value, remote[key], path=child))
        return bad

    @staticmethod
    def _is_string_list(value: Any) -> bool:
        return (
            isinstance(value, Sequence)
            and not isinstance(value, str)
            and all(isinstance(item, str) for item in value)
        )

    @classmethod
    def _compare_string_list(cls, local: Any, remote: Any, *, path: str) -> list[str]:
        """Scopes and events are sets, not sequences — Slack returns them in
        its own order and that is not a change."""
        if not cls._is_string_list(remote):
            return [f"{path} : 슬랙 쪽 값의 형태가 다르다 : {remote!r}"]
        bad: list[str] = []
        빠짐 = sorted(set(local) - set(remote))
        더함 = sorted(set(remote) - set(local))
        if 빠짐:
            bad.append(f"{path} : 슬랙 쪽에 없다 : {', '.join(빠짐)}")
        if 더함:
            bad.append(f"{path} : 정본에 없는 것이 슬랙에 있다 : {', '.join(더함)}")
        return bad
