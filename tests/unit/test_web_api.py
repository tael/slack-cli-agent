"""웹 콘솔 API 라우터.

ApiRouter 는 HTTP 서버와 분리돼 있다. 실물 편집기 대신 대역을 주입해 시험한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from slack_cli_agent.web.api import ApiRouter


class FakeProfiles:
    def __init__(self, data: dict[str, dict[str, object]], save_errors: list[str] | None = None) -> None:
        self._data = data
        self._save_errors = save_errors if save_errors is not None else []
        self.saved: tuple[str, Mapping[str, object]] | None = None

    def names(self) -> list[str]:
        return sorted(self._data)

    def read(self, name: str) -> dict[str, object]:
        return self._data[name]

    def save(self, name: str, data: Mapping[str, object]) -> list[str]:
        self.saved = (name, data)
        return self._save_errors


class FakeFileEditor:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.written: tuple[str, str] | None = None

    def names(self) -> list[str]:
        return sorted(self._files)

    def read(self, name: str) -> str:
        return self._files[name]

    def write(self, name: str, text: str) -> None:
        self.written = (name, text)
        self._files[name] = text


class FakeChannelEditor:
    def __init__(self, channels: list[dict[str, object]]) -> None:
        self._channels = channels
        self.updated: tuple[str, Mapping[str, object]] | None = None

    def list(self) -> list[dict[str, object]]:
        return self._channels

    def update(self, channel_id: str, changes: Mapping[str, object]) -> dict[str, object]:
        self.updated = (channel_id, changes)
        return {"channel_id": channel_id, **changes}


class FakeMetrics:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.calls: list[int] = []

    def collect(self, days: int) -> dict[str, object]:
        self.calls.append(days)
        return self._result


class FakeRoster:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def rows(self) -> list[dict[str, object]]:
        return self._rows


class Boom:
    def names(self) -> list[str]:
        raise RuntimeError("고장")


def make_router(
    *,
    profiles: FakeProfiles | None = None,
    channels: dict[str, FakeChannelEditor] | None = None,
    prompts: dict[str, FakeFileEditor] | None = None,
    knowledge: dict[str, FakeFileEditor] | None = None,
    metrics: dict[str, FakeMetrics] | None = None,
    roster: FakeRoster | None = None,
) -> ApiRouter:
    profiles = profiles if profiles is not None else FakeProfiles({"mametchi": {"name": "mametchi"}})
    channels = channels if channels is not None else {}
    prompts = prompts if prompts is not None else {}
    knowledge = knowledge if knowledge is not None else {}
    metrics = metrics if metrics is not None else {}
    roster = roster if roster is not None else FakeRoster([])
    return ApiRouter(
        profiles=profiles,
        channels_for=lambda bot: channels[bot],
        prompts_for=lambda bot: prompts[bot],
        knowledge_for=lambda bot: knowledge[bot],
        metrics_for=lambda bot: metrics[bot],
        roster=roster,
    )


class Test경로_판정:
    def test_모르는_경로는_404(self) -> None:
        router = make_router()
        res = router.handle("GET", "/api/없는경로", {}, None)
        assert res.status == 404

    def test_허용하지_않는_메서드는_405(self) -> None:
        router = make_router()
        res = router.handle("DELETE", "/api/health", {}, None)
        assert res.status == 405

    def test_헬스체크는_200(self) -> None:
        router = make_router()
        res = router.handle("GET", "/api/health", {}, None)
        assert res.status == 200
        assert res.body == {"ok": True}


class Test봇명부:
    """봇 선택줄이 쓰는 경로. 지표는 봇 하나만 보므로 명부는 따로 낸다."""

    def test_모든_봇의_한줄_상태를_낸다(self) -> None:
        rows = [{"name": "asuka", "available": True}, {"name": "rei", "available": False}]
        router = make_router(roster=FakeRoster(rows))
        res = router.handle("GET", "/api/bots", {}, None)
        assert res.status == 200
        assert res.body == rows

    def test_GET_이_아니면_405(self) -> None:
        router = make_router()
        assert router.handle("PUT", "/api/bots", {}, None).status == 405


class Test프로필:
    def test_목록을_낸다(self) -> None:
        router = make_router(profiles=FakeProfiles({"b": {}, "a": {}}))
        res = router.handle("GET", "/api/profiles", {}, None)
        assert res.status == 200
        assert res.body == ["a", "b"]

    def test_있는_프로필을_읽는다(self) -> None:
        router = make_router(profiles=FakeProfiles({"mametchi": {"model": "sonnet-5"}}))
        res = router.handle("GET", "/api/profile/mametchi", {}, None)
        assert res.status == 200
        assert res.body == {"model": "sonnet-5"}

    def test_없는_프로필은_404(self) -> None:
        router = make_router(profiles=FakeProfiles({}))
        res = router.handle("GET", "/api/profile/없음", {}, None)
        assert res.status == 404

    def test_검증을_통과하면_200(self) -> None:
        profiles = FakeProfiles({"mametchi": {}}, save_errors=[])
        router = make_router(profiles=profiles)
        res = router.handle("PUT", "/api/profile/mametchi", {}, {"model": "opus-5"})
        assert res.status == 200
        assert profiles.saved == ("mametchi", {"model": "opus-5"})

    def test_검증_오류_목록이_있으면_400과_그_목록(self) -> None:
        profiles = FakeProfiles({"mametchi": {}}, save_errors=["model 이 없다"])
        router = make_router(profiles=profiles)
        res = router.handle("PUT", "/api/profile/mametchi", {}, {})
        assert res.status == 400
        assert res.body == {"errors": ["model 이 없다"]}

    @pytest.mark.parametrize("bad_body", [None, [1, 2], "문자열", 3])
    def test_본문이_객체가_아니면_400(self, bad_body: Any) -> None:
        router = make_router()
        res = router.handle("PUT", "/api/profile/mametchi", {}, bad_body)
        assert res.status == 400


class Test채널:
    def test_모르는_봇은_404(self) -> None:
        router = make_router(profiles=FakeProfiles({"mametchi": {}}))
        res = router.handle("GET", "/api/channels/없는봇", {}, None)
        assert res.status == 404

    def test_채널_목록을_낸다(self) -> None:
        editor = FakeChannelEditor([{"channel_id": "C1"}])
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            channels={"mametchi": editor},
        )
        res = router.handle("GET", "/api/channels/mametchi", {}, None)
        assert res.status == 200
        assert res.body == [{"channel_id": "C1"}]

    def test_채널을_갱신한다(self) -> None:
        editor = FakeChannelEditor([])
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            channels={"mametchi": editor},
        )
        res = router.handle("PUT", "/api/channels/mametchi/C1", {}, {"mode": "always"})
        assert res.status == 200
        assert res.body == {"channel_id": "C1", "mode": "always"}
        assert editor.updated == ("C1", {"mode": "always"})

    def test_채널_갱신_본문이_객체가_아니면_400(self) -> None:
        editor = FakeChannelEditor([])
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            channels={"mametchi": editor},
        )
        res = router.handle("PUT", "/api/channels/mametchi/C1", {}, "문자열")
        assert res.status == 400
        assert editor.updated is None


class Test프롬프트:
    def test_모르는_봇은_404(self) -> None:
        router = make_router(profiles=FakeProfiles({"mametchi": {}}))
        res = router.handle("GET", "/api/prompts/없는봇", {}, None)
        assert res.status == 404

    def test_이름_목록을_낸다(self) -> None:
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            prompts={"mametchi": FakeFileEditor({"greeting": "안녕"})},
        )
        res = router.handle("GET", "/api/prompts/mametchi", {}, None)
        assert res.status == 200
        assert res.body == ["greeting"]

    def test_있는_프롬프트를_읽는다(self) -> None:
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            prompts={"mametchi": FakeFileEditor({"greeting": "안녕"})},
        )
        res = router.handle("GET", "/api/prompt/mametchi/greeting", {}, None)
        assert res.status == 200
        assert res.body == {"name": "greeting", "text": "안녕"}

    def test_없는_프롬프트는_404(self) -> None:
        router = make_router(
            profiles=FakeProfiles({"mametchi": {}}),
            prompts={"mametchi": FakeFileEditor({})},
        )
        res = router.handle("GET", "/api/prompt/mametchi/없음", {}, None)
        assert res.status == 404

    def test_프롬프트를_쓴다(self) -> None:
        editor = FakeFileEditor({})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), prompts={"mametchi": editor})
        res = router.handle("PUT", "/api/prompt/mametchi/greeting", {}, {"text": "새 인사"})
        assert res.status == 200
        assert editor.written == ("greeting", "새 인사")

    def test_text_필드가_없으면_400(self) -> None:
        editor = FakeFileEditor({})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), prompts={"mametchi": editor})
        res = router.handle("PUT", "/api/prompt/mametchi/greeting", {}, {})
        assert res.status == 400
        assert editor.written is None


class Test지식:
    def test_모르는_봇은_404(self) -> None:
        router = make_router(profiles=FakeProfiles({"mametchi": {}}))
        res = router.handle("GET", "/api/knowledge/없는봇", {}, None)
        assert res.status == 404

    def test_있는_지식을_읽고_쓴다(self) -> None:
        editor = FakeFileEditor({"faq": "질문과 답"})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), knowledge={"mametchi": editor})

        res = router.handle("GET", "/api/knowledge/mametchi/faq", {}, None)
        assert res.status == 200
        assert res.body == {"name": "faq", "text": "질문과 답"}

        res = router.handle("PUT", "/api/knowledge/mametchi/faq", {}, {"text": "갱신"})
        assert res.status == 200
        assert editor.written == ("faq", "갱신")


class Test지표:
    def test_모르는_봇은_404(self) -> None:
        router = make_router(profiles=FakeProfiles({"mametchi": {}}))
        res = router.handle("GET", "/api/state/없는봇", {}, None)
        assert res.status == 404

    def test_days_쿼리를_정수로_넘긴다(self) -> None:
        metrics = FakeMetrics({"generated_at": "2026-09-15"})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), metrics={"mametchi": metrics})
        res = router.handle("GET", "/api/state/mametchi", {"days": "14"}, None)
        assert res.status == 200
        assert metrics.calls == [14]

    def test_days_쿼리가_없으면_기본값을_쓴다(self) -> None:
        metrics = FakeMetrics({})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), metrics={"mametchi": metrics})
        router.handle("GET", "/api/state/mametchi", {}, None)
        assert metrics.calls == [7]

    def test_days_쿼리가_숫자가_아니면_기본값을_쓴다(self) -> None:
        metrics = FakeMetrics({})
        router = make_router(profiles=FakeProfiles({"mametchi": {}}), metrics={"mametchi": metrics})
        router.handle("GET", "/api/state/mametchi", {"days": "며칠"}, None)
        assert metrics.calls == [7]


class Test예외_처리:
    def test_핸들러_예외는_밖으로_안_나가고_500이다(self) -> None:
        router = make_router(profiles=Boom())  # type: ignore[arg-type]
        res = router.handle("GET", "/api/profiles", {}, None)
        assert res.status == 500
        assert "RuntimeError" in str(res.body)

    def test_지표_수집_예외도_500이다(self) -> None:
        class BoomMetrics:
            def collect(self, days: int) -> dict[str, object]:
                raise ValueError("깨짐")

        router = make_router(profiles=FakeProfiles({"mametchi": {}}), metrics={"mametchi": BoomMetrics()})  # type: ignore[dict-item]
        res = router.handle("GET", "/api/state/mametchi", {}, None)
        assert res.status == 500


class Test빈_본문_저장:
    """빈 프롬프트는 그 봇을 다음 기동에서 죽인다. 사용자 입력 오류이므로
    500 이 아니라 400 으로 알리고 화면이 그 사유를 보여줄 수 있어야 한다."""

    def test_파일_편집기가_거부하면_400과_사유를_낸다(self) -> None:
        class 거부하는편집기:
            def names(self) -> list[str]:
                return ["persona"]

            def read(self, name: str) -> str:
                return "본문"

            def write(self, name: str, text: str) -> None:
                raise ValueError("빈 본문은 저장하지 않는다")

        router = make_router(
            profiles=FakeProfiles({"mametchi": {"name": "mametchi"}}),
            prompts={"mametchi": 거부하는편집기()},  # type: ignore[dict-item]  # 계약만 만족하면 된다
        )
        res = router.handle("PUT", "/api/prompt/mametchi/persona", {}, {"text": "   "})

        assert res.status == 400
        assert "빈 본문은 저장하지 않는다" in str(res.body)
