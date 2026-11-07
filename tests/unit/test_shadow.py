"""그림자 평가 (G4, bot.py shadow_loop).

원본 채널의 새 최상위 질문을 그림자 채널에 링크로 올리고, 원본 스레드의
실제 답을 보지 않고 답한다. 다음 날 실제 답과 대조해 판정한다. 원본
채널에는 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from test_application import FakeSlackClient, write_profile

from slack_cli_agent.core.application import Application
from slack_cli_agent.core.timezones import KST
from slack_cli_agent.shadow.service import (
    FAILED_NOTICE,
    ShadowService,
    TurnResult,
    blind_violations,
    is_question,
    shadow_pairs,
    tool_calls_in,
)
from slack_cli_agent.shadow.store import ShadowStore
from slack_cli_agent.storage.database import Database

SOURCE, TARGET = "CSRC", "CSHADOW"
# 2026-11-02 10:00 KST
Q_TS = f"{datetime(2026, 11, 2, 10, 0, tzinfo=KST).timestamp():.6f}"


@contextmanager
def _slot() -> Iterator[None]:
    yield


class _Api:
    def __init__(self, history: list[dict[str, Any]], replies: list[dict[str, Any]] | None = None) -> None:
        self.history = history
        self.replies = replies or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, **params: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "conversations.history":
            if "latest" in params:
                return {"messages": [m for m in self.history if m["ts"] == params["latest"]]}
            return {"messages": [m for m in self.history if float(m["ts"]) > float(params["oldest"])]}
        if method == "chat.getPermalink":
            return {"permalink": f"https://x.slack.com/archives/{SOURCE}/p1"}
        if method == "conversations.info":
            return {"channel": {"name": "people-support"}}
        if method == "conversations.replies":
            return {"messages": self.replies}
        raise AssertionError(method)


def _service(
    tmp_path: Path, api: _Api | None, turn: TurnResult, *, now: float, judge: bool = False,
) -> tuple[ShadowService, list[tuple[str, str | None, str]], list[str]]:
    posts: list[tuple[str, str | None, str]] = []
    prompts: list[str] = []
    db = Database(tmp_path / "state.db")
    db.migrate()

    def post(channel: str, thread_ts: str | None, text: str) -> str:
        posts.append((channel, thread_ts, text))
        return f"9{len(posts)}.0"

    def run_turn(prompt: str, channel: str, request_id: str) -> TurnResult:
        prompts.append(prompt)
        return turn

    service = ShadowService(
        store=ShadowStore(db, now=lambda: now), source_api=api, post=post, run_turn=run_turn,
        pairs=lambda: {TARGET: SOURCE}, judge_enabled=lambda ch: judge, name_of=lambda u: f"이름{u}",
        audit=lambda kind, **f: None, requests_path=tmp_path / "shadow_requests.json",
        bot_name="구치파치", now=lambda: now,
    )
    return service, posts, prompts


def _question(ts: str = Q_TS, **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "user": "U1", "text": "법인카드 한도 올리려면?", **extra}


def test_질문_선별은_사람의_최상위_글만() -> None:
    assert is_question(_question())
    assert is_question(_question(subtype="file_share"))
    assert not is_question(_question(bot_id="B1"))
    assert not is_question(_question(subtype="channel_join"))
    assert not is_question(_question(thread_ts="1.0"))
    assert not is_question({"ts": Q_TS, "user": "U1", "text": "  "})


def test_블라인드_위반은_원본_접근과_날짜없는_슬랙검색() -> None:
    compact = Q_TS.replace(".", "")
    calls = [
        ("mcp__slack__conversations_replies", json.dumps({"channel_id": SOURCE})),
        ("Read", f"https://x.slack.com/archives/C9/p{compact}"),
        ("mcp__slack__conversations_search_messages", json.dumps({"query": "한도"})),
        ("mcp__slack__conversations_search_messages", json.dumps({"query": "한도 before:2026-11-03"})),
        ("mcp__notion__notion-search", json.dumps({"query": "한도"})),
    ]
    assert blind_violations(calls, SOURCE, Q_TS) == [
        "Read 가 원본 채널이나 원본 글을 열었다",
        "mcp__slack__conversations_replies 가 원본 채널이나 원본 글을 열었다",
        "슬랙 검색에 질문일 이하 before: 가 없다",
    ]
    ok = [("mcp__slack__conversations_search_messages", json.dumps({"query": "한도 before:2026-11-02"}))]
    assert blind_violations(ok, SOURCE, Q_TS) == []


def test_실행기록에서_도구호출을_뽑는다(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join([
        json.dumps({"message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "a"}}]}}),
        json.dumps({"payload": {"type": "function_call", "namespace": "mcp__slack",
                                "name": "search", "arguments": "{\"q\":1}"}}),
        json.dumps({"message": {"content": [{"type": "text", "text": SOURCE}]}}),
        "not json",
    ]), encoding="utf-8")
    assert tool_calls_in(path) == (("Grep", '{"pattern": "a"}'), ("mcp__slack.search", '{"q":1}'))
    assert tool_calls_in(tmp_path / "없음.jsonl") is None
    assert tool_calls_in(None) is None


def test_처음_보는_원본은_지금부터_본다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, posts, _ = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 60)
    service.tick(_slot)
    assert posts == []


def test_새_질문은_링크와_블라인드_답으로_게시된다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, posts, prompts = _service(
        tmp_path, api, TurnResult(True, "답입니다\n[[RECIPIENT:ASKER]]", ()), now=float(Q_TS) + 60,
    )
    service._store.mark_seen(SOURCE, f"{float(Q_TS) - 1:.6f}")
    service.tick(_slot)
    assert posts[0][0] == TARGET and posts[0][1] is None
    assert posts[0][2].startswith("[질문 원문 11/02 10:00](https://x.slack.com/")
    assert posts[1] == (TARGET, "91.0", "답입니다")
    assert "before:2026-11-02" in prompts[0] and "people-support" in prompts[0] and "이름U1" in prompts[0]
    assert service._store.item(SOURCE, Q_TS)["status"] == "answered"
    assert service._store.seen(SOURCE) == Q_TS
    # 원본 채널에는 쓰지 않는다
    assert all(ch == TARGET for ch, _, _ in posts)
    service.tick(_slot)
    assert len(posts) == 2


def test_위반과_검사불가를_답_아래에_표시한다(tmp_path: Path) -> None:
    api = _Api([_question()])
    calls = (("mcp__slack__conversations_replies", SOURCE),)
    service, posts, _ = _service(tmp_path, api, TurnResult(True, "답", calls), now=float(Q_TS) + 60)
    service._store.mark_seen(SOURCE, "1.0")
    service.tick(_slot)
    assert posts[1][2].endswith("> 블라인드 위반 : mcp__slack__conversations_replies 가 원본 채널이나 원본 글을 열었다")

    api2 = _Api([_question()])
    service2, posts2, _ = _service(tmp_path / "b", api2, TurnResult(True, "답", None), now=float(Q_TS) + 60)
    service2._store.mark_seen(SOURCE, "1.0")
    service2.tick(_slot)
    assert "블라인드 위반 검사 : 실행 기록을 읽지 못해 검사하지 못했다" in posts2[1][2]


def test_실패는_세번째에만_알리고_같은_링크를_다시_쓴다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, posts, _ = _service(tmp_path, api, TurnResult(False, "", None), now=float(Q_TS) + 60)
    service._store.mark_seen(SOURCE, "1.0")
    service.tick(_slot)
    assert len(posts) == 1
    service.tick(_slot)
    assert len(posts) == 1
    service.tick(_slot)
    assert posts[1:] == [(TARGET, "91.0", FAILED_NOTICE)]
    assert service._store.item(SOURCE, Q_TS)["attempts"] == 3
    service.tick(_slot)
    assert len(posts) == 2


def test_사용자_토큰이_없으면_읽지_않고_로그를_남긴다(tmp_path: Path, caplog: Any) -> None:
    service, posts, _ = _service(tmp_path, None, TurnResult(True, "답", ()), now=float(Q_TS))
    service.tick(_slot)
    assert posts == []
    assert "사용자 세션 토큰이 없어" in caplog.text


def test_소급_요청은_지난_질문까지_돌고_파일을_지운다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, posts, _ = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 86400)
    (tmp_path / "shadow_requests.json").write_text(json.dumps([{"target": TARGET, "days": 2}]))
    service.tick(_slot)
    assert [p[2] for p in posts][1:] == ["답"]
    assert not (tmp_path / "shadow_requests.json").exists()


def test_판정은_다음날_10시_이후_실제_답이_있을_때(tmp_path: Path) -> None:
    next_day_9 = datetime(2026, 11, 3, 9, 0, tzinfo=KST).timestamp()
    next_day_11 = datetime(2026, 11, 3, 11, 0, tzinfo=KST).timestamp()
    replies = [_question(), {"ts": f"{float(Q_TS) + 100:.6f}", "user": "U2", "text": "재무팀에 요청"}]
    service, posts, prompts = _service(
        tmp_path, _Api([], replies), TurnResult(True, "판정 : 일부 맞음\n- 요지", ()),
        now=next_day_11, judge=True,
    )
    service._store.save({"source": SOURCE, "ts": Q_TS, "target": TARGET, "status": "answered",
                         "root_ts": "91.0", "question": "q", "answer": "a"})
    assert not _service(tmp_path / "c", None, TurnResult(True, "", ()), now=next_day_9)[0].is_due(
        service._store.item(SOURCE, Q_TS))
    service._store.mark_seen(SOURCE, "1.0")
    service.tick(_slot)
    assert posts == [(TARGET, "91.0", "판정 : 일부 맞음\n- 요지")]
    assert "이름U2] 재무팀에 요청" in prompts[0]
    assert service._store.item(SOURCE, Q_TS)["verdict"] == "일부 맞음"


def test_질문자_보충글만_있으면_판정을_미룬다(tmp_path: Path) -> None:
    next_day_11 = datetime(2026, 11, 3, 11, 0, tzinfo=KST).timestamp()
    replies = [_question(), {"ts": f"{float(Q_TS) + 100:.6f}", "user": "U1", "text": "추가로"}]
    service, posts, prompts = _service(tmp_path, _Api([], replies), TurnResult(True, "x", ()),
                                       now=next_day_11, judge=True)
    service._store.save({"source": SOURCE, "ts": Q_TS, "target": TARGET, "status": "answered"})
    service._store.mark_seen(SOURCE, "1.0")
    service.tick(_slot)
    assert posts == [] and prompts == []
    assert service._store.item(SOURCE, Q_TS)["judge_tried_on"] == "2026-11-03"


def test_채널쌍은_extra_의_shadow_source(tmp_path: Path) -> None:
    class C:
        def __init__(self, extra: dict[str, Any]) -> None:
            self.extra = extra

    assert shadow_pairs({"A": C({"shadow_source": "S"}), "B": C({}), "D": C({"shadow_source": ""})}) == {"A": "S"}


def test_워커_서비스에_그림자_루프가_있다(tmp_path: Path) -> None:
    app = Application(write_profile(tmp_path), FakeSlackClient())
    assert "shadow:loop" in app.worker_services(app.worker()).runner_names


def test_원본을_읽지_못하는_쌍이_있어도_다른_쌍은_돈다(tmp_path: Path) -> None:
    dead_ts = f"{float(Q_TS) - 600:.6f}"

    class _Mixed(_Api):
        def __call__(self, method: str, **params: Any) -> dict[str, Any]:
            if params.get("channel") == "CDEAD":
                self.calls.append((method, params))
                raise RuntimeError("channel_not_found")
            return super().__call__(method, **params)

    api = _Mixed([_question()])
    service, posts, prompts = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 60)
    service._pairs = lambda: {"CSHADOW2": "CDEAD", TARGET: SOURCE}
    service._store.save({"source": "CDEAD", "ts": dead_ts, "target": "CSHADOW2", "status": "failed",
                         "attempts": 1, "root_ts": "80.0"})
    service._store.mark_seen(SOURCE, "1.0")
    service._store.mark_seen("CDEAD", "1.0")
    service.tick(_slot)
    assert [p[2] for p in posts if p[0] == TARGET][1:] == ["답"]
    assert len(prompts) == 1


def test_링크_게시가_실패해도_시도횟수를_세고_세번에서_멈춘다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, _, prompts = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 60)
    link_posts: list[str] = []

    def no_post(channel: str, thread_ts: str | None, text: str) -> None:
        link_posts.append(text)

    service._post = no_post
    service._store.mark_seen(SOURCE, "1.0")
    for _ in range(5):
        service.tick(_slot)
    assert len(link_posts) == 3
    assert service._store.item(SOURCE, Q_TS)["attempts"] == 3
    assert prompts == []


def test_엔진이_예외를_내도_세번에서_멈춘다(tmp_path: Path) -> None:
    api = _Api([_question()])
    service, posts, _ = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 60)
    turns: list[str] = []

    def boom(prompt: str, channel: str, request_id: str) -> TurnResult:
        turns.append(request_id)
        raise RuntimeError("engine crashed")

    service._run_turn = boom
    service._store.mark_seen(SOURCE, "1.0")
    for _ in range(5):
        service.tick(_slot)
    assert len(turns) == 3
    assert service._store.item(SOURCE, Q_TS)["attempts"] == 3
    assert len([p for p in posts if p[1] is None]) == 1
    assert [p[2] for p in posts if p[1] is not None] == [FAILED_NOTICE]


def test_자리를_얻지_못한_회차는_시도횟수를_쓰지_않는다(tmp_path: Path) -> None:
    from slack_cli_agent.plugin.hooks import SlotUnavailable

    @contextmanager
    def _busy() -> Iterator[None]:
        raise SlotUnavailable("no slot")
        yield  # pragma: no cover

    api = _Api([_question()])
    service, posts, prompts = _service(tmp_path, api, TurnResult(True, "답", ()), now=float(Q_TS) + 60)
    service._store.mark_seen(SOURCE, "1.0")
    for _ in range(3):
        try:
            service.tick(_busy)
        except SlotUnavailable:
            pass
    item = service._store.item(SOURCE, Q_TS)
    assert item["attempts"] == 0
    assert item["status"] == "posting"
    service.tick(_slot)
    assert len(prompts) == 1
    assert [p[2] for p in posts] == [posts[0][2], "답"]
    assert service._store.item(SOURCE, Q_TS)["attempts"] == 1


def test_그림자_턴의_effort_는_원본_글로_오르지_않는다(tmp_path: Path) -> None:
    from types import SimpleNamespace

    app = Application(write_profile(tmp_path), FakeSlackClient())
    seen: list[str] = []

    class _Invoker:
        def invoke(self, request: Any, origin: Any) -> Any:
            seen.append(request.effort)
            return SimpleNamespace(ok=False, body="", session_id=None, engine=None)

    app._invoker = _Invoker()  # type: ignore[assignment]
    app._shadow_turn("원본 답글 : ultrathink 로 봐라", "C1", "shadow-judge-x")
    assert seen and seen[0] != "high"


def test_그림자_턴의_모델별_사용량을_감사에_남긴다(tmp_path: Path) -> None:
    """A14 : 그림자 답변과 판정도 엔진 호출이라 모델별 집계에 들어가야 한다."""
    from types import SimpleNamespace

    from slack_cli_agent.engine.base import MODEL_USAGE_KEY, ModelUsage
    from slack_cli_agent.observability.audit import ENGINE_SPEND_KIND

    app = Application(write_profile(tmp_path), FakeSlackClient())
    기록: list[tuple[str, dict[str, Any]]] = []
    app._audit = SimpleNamespace(record=lambda kind, **f: 기록.append((kind, f)))  # type: ignore[assignment]

    class _Invoker:
        def invoke(self, request: Any, origin: Any) -> Any:
            return SimpleNamespace(
                ok=False, body="", session_id="s-shadow", engine="claude", usage=None,
                model_asked="sonnet",
                model_usage={"claude-sonnet-4": ModelUsage(output_tokens=7, cost_usd=0.01)},
            )

    app._invoker = _Invoker()  # type: ignore[assignment]
    app._shadow_turn("질문", "C1", "shadow-judge-x")
    [(_, 행)] = [r for r in 기록 if r[0] == ENGINE_SPEND_KIND]
    assert 행["purpose"] == "shadow_turn"
    assert 행["channel"] == "C1"
    assert 행[MODEL_USAGE_KEY]["claude-sonnet-4"]["output_tokens"] == 7
    assert 행["session_id"] == "s-shadow"


def test_그림자_턴이_모델별_표가_없으면_비용_행을_쓰지_않는다(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from slack_cli_agent.observability.audit import ENGINE_SPEND_KIND

    app = Application(write_profile(tmp_path), FakeSlackClient())
    기록: list[tuple[str, dict[str, Any]]] = []
    app._audit = SimpleNamespace(record=lambda kind, **f: 기록.append((kind, f)))  # type: ignore[assignment]

    class _Invoker:
        def invoke(self, request: Any, origin: Any) -> Any:
            return SimpleNamespace(ok=False, body="", session_id=None, engine="codex", usage=None,
                                   model_asked=None, model_usage=None)

    app._invoker = _Invoker()  # type: ignore[assignment]
    app._shadow_turn("질문", "C1", "shadow-x")
    assert not [r for r in 기록 if r[0] == ENGINE_SPEND_KIND]
