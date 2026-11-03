"""DM 세션 스코프 (A39).

DM 에는 스레드가 없다. 모든 메시지가 저마다의 ts 를 가진 최상위 메시지라,
thread 스코프면 메시지마다 새 세션이 열려 대화가 이어지지 않는다. 마메치는
DM 을 항상 channel 스코프로 둔다(bot.py:3064). sca 는 설정이 없으면 기본값
THREAD 로 떨어져 DM 이 thread 로 역전됐다. 이 시험은 DM 이면 설정 유무와
무관하게 channel 스코프로 세션이 이어지는지 본다.
"""

from __future__ import annotations

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.session.ports import SessionKey


def test_DM은_설정없어도_채널스코프로_세션이_이어진다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response(session_id="엔진발급")])
    ctx = make_ctx(channel="D1", user="U1", thread_ts="1700000001.000100")

    pipeline.handle(ctx)

    # 채널 스코프 키에 세션이 저장돼 다음 요청이 이어받을 수 있다.
    decision = parts["sessions"].resolve(
        SessionKey(scope="channel", key="D1"), engine="fake-engine"
    )
    assert decision.session_id == "엔진발급"
    assert decision.resume is True


def test_DM은_ts가_달라도_같은_세션으로_이어진다() -> None:
    # 같은 DM 의 두 번째 메시지는 ts 가 다르지만 같은 채널 세션을 이어야 한다.
    pipeline, parts = build_pipeline(
        responses=[ok_response(session_id="엔진발급"), ok_response(session_id="엔진발급")]
    )
    pipeline.handle(make_ctx(channel="D1", user="U1", thread_ts="1700000001.000100"))
    pipeline.handle(make_ctx(channel="D1", user="U1", thread_ts="1700000002.000200"))

    # thread 스코프에는 아무 세션도 열리지 않았다.
    thread_rec = parts["sessions"]._store.get(
        SessionKey(scope="thread", key="D1:1700000001.000100")
    )
    assert thread_rec is None


def test_일반_채널은_여전히_스레드_스코프다() -> None:
    pipeline, parts = build_pipeline(responses=[ok_response(session_id="엔진발급")])
    ctx = make_ctx(channel="C1", user="U1", thread_ts="1700000001.000100")

    pipeline.handle(ctx)

    decision = parts["sessions"].resolve(
        SessionKey(scope="thread", key="C1:1700000001.000100"), engine="fake-engine"
    )
    assert decision.session_id == "엔진발급"
