"""tools/owner-turn-probe.py — --set 으로 프로필 settings 를 이번 실행에만 덮어쓴다."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from slack_cli_agent.config.profile import Profile

_SPEC = importlib.util.spec_from_file_location(
    "owner_turn_probe", Path(__file__).resolve().parents[2] / "tools" / "owner-turn-probe.py"
)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

EXAMPLE = Path(__file__).resolve().parents[2] / "profiles" / "example.example.json"


def _profile() -> Profile:
    return Profile.from_dict(json.loads(EXAMPLE.read_text(encoding="utf-8")))


class Test_set_파싱:
    def test_json_값은_형을_살린다(self) -> None:
        got = probe.parse_set_options(["a=true", "b=12", "c=1.5", "d=[1,2]"])
        assert got == {"a": True, "b": 12, "c": 1.5, "d": [1, 2]}

    def test_json_이_아니면_문자열이다(self) -> None:
        assert probe.parse_set_options(["name=hello world"]) == {"name": "hello world"}

    def test_값에_등호가_있어도_첫_등호에서만_나눈다(self) -> None:
        assert probe.parse_set_options(["q=a=b"]) == {"q": "a=b"}

    def test_같은_키는_뒤가_이긴다(self) -> None:
        assert probe.parse_set_options(["a=1", "a=2"]) == {"a": 2}

    @pytest.mark.parametrize("bad", ["novalue", "=1"])
    def test_키_없거나_등호_없으면_종료한다(self, bad: str) -> None:
        with pytest.raises(SystemExit):
            probe.parse_set_options([bad])


class Test_덮어쓰기_적용:
    def test_기존_키를_덮고_새_키를_더한다(self) -> None:
        out = probe.apply_setting_overrides(_profile(), {"plan_intake": True, "intake_ack_after_sec": 12})
        assert out.settings_override["plan_intake"] is True
        assert out.settings_override["intake_ack_after_sec"] == 12

    def test_원본_프로필은_바뀌지_않는다(self) -> None:
        base = _profile()
        before = dict(base.settings_override)
        probe.apply_setting_overrides(base, {"plan_intake": True})
        assert dict(base.settings_override) == before

    def test_빈_덮어쓰기는_같은_프로필을_돌려준다(self) -> None:
        base = _profile()
        assert probe.apply_setting_overrides(base, {}) is base
