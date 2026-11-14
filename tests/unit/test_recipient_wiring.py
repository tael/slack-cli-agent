"""[[RECIPIENT:...]] 해석·멘션 강제·미표식 차단의 파이프라인 배선 (D3/J11).

마커 파싱과 멘션 강제 자체는 test_bot_interaction.py(RecipientResolver)가,
표식 제거 배선은 test_pipeline_publish_markers.py가 본다. 여기서는 게시 직전
파이프라인이 수신자를 실제로 해석해 (1) 봇 수신자에 첫머리 멘션을 강제하고
(2) require_recipient 가 켜졌을 때 표식 없는 답에 표식만 한 번 다시 받고, 그래도
없으면 침묵하는지(B1)를 본다. bot.py:8085-8133 missing_recipient 재요청.
"""

from __future__ import annotations

from pathlib import Path

from test_pipeline import build_pipeline, fail_response, make_ctx, ok_response

from slack_cli_agent.core.markers import missing_recipient_prompt
from slack_cli_agent.engine.tool_selection import ToolAccess


def _is_bot(bots: set[str]):
    return lambda user_id: user_id in bots


class Test봇_수신자_멘션강제:
    def test_BOT_표식은_첫머리_멘션을_강제한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="검토했습니다.\n[[RECIPIENT:BOT:UBOT]]")],
            asker_is_bot=_is_bot({"UBOT"}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert posted == "<@UBOT> 검토했습니다."
        assert "RECIPIENT" not in posted

    def test_봇화자_ASKER는_그_봇_멘션으로_접힌다(self, tmp_path: Path) -> None:
        # ASKER 인데 말을 건 쪽이 봇이면 그 봇이 알림을 받도록 멘션을 붙인다.
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="확인했습니다.\n[[RECIPIENT:ASKER]]")],
            asker_is_bot=_is_bot({"UPEER"}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(user="UPEER"))
        posted = deps["publisher"].posted[0]["text"]
        assert posted == "<@UPEER> 확인했습니다."

    def test_봇화자에게_표식없이_답하면_그_봇을_멘션한다(self, tmp_path: Path) -> None:
        # 이어받은 세션은 첫 턴의 소유자 지침만 보고 표식과 멘션을 빠뜨린다.
        # 게시 단계에서 말을 건 봇을 깨운다 (2026-11-06 구치파치 -> 마메치).
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="Dear 마메치님, 시험을 돌리고 있습니다.")],
            asker_is_bot=_is_bot({"UPEER"}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(user="UPEER"))
        posted = deps["publisher"].posted[0]["text"]
        assert posted == "<@UPEER> 시험을 돌리고 있습니다."

    def test_봇화자에게_이미_멘션했으면_다시_붙이지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="진행합니다. <@UPEER> 결과는 곧 드립니다.")],
            asker_is_bot=_is_bot({"UPEER"}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(user="UPEER"))
        posted = deps["publisher"].posted[0]["text"]
        assert posted.count("<@UPEER>") == 1

    def test_사람화자에게_표식없이_답하면_멘션을_붙이지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="답입니다.")],
            asker_is_bot=_is_bot({"UPEER"}),
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(user="UHUMAN"))
        posted = deps["publisher"].posted[0]["text"]
        assert "<@" not in posted

    def test_OWNER는_멘션을_붙이지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="보고드립니다.\n[[RECIPIENT:OWNER]]")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert posted == "보고드립니다."
        assert "<@" not in posted


class Test미표식_재요청_B1:
    """표식 없는 답은 본문을 코드가 들고 표식만 한 번 다시 받는다 (B1, bot.py:8085-8133).

    재요청은 도구 없이(no_act) 같은 세션을 이어 돌린다. 앞 턴의 지라 등록이나
    발송을 되풀이하지 않게 하는 장치다. 그래도 표식이 없으면 본문도 안내문도
    올리지 않고 침묵하며 감사에 남긴다.
    """

    def test_표식없으면_도구없이_표식만_다시_받아_원본문을_게시한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="표식 없는 답입니다."),
                ok_response(body="[[RECIPIENT:ASKER]]"),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        calls = deps["runner"].calls
        assert len(calls) == 2
        retry = calls[1]
        assert retry.prompt == missing_recipient_prompt()
        assert retry.resume is True
        assert retry.session_id in (calls[0].session_id, "sess-1")
        assert retry.tools.access is ToolAccess.FORBIDDEN
        posted = [p["text"] for p in deps["publisher"].posted]
        assert posted == ["표식 없는 답입니다."]
        assert outcome.silent is False
        retry_rows = [i for i in deps["audit"].incidents if i["kind"] == "missing_recipient_retry"]
        assert len(retry_rows) == 1
        assert retry_rows[0]["recovered"] is True

    def test_재요청이_본문을_바꿔와도_원본문만_게시한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="원래 답입니다."),
                ok_response(body="새로 쓴 다른 답입니다.\n[[RECIPIENT:OWNER]]"),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert [p["text"] for p in deps["publisher"].posted] == ["원래 답입니다."]

    def test_재요청에도_표식없으면_침묵하고_감사에_남긴다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="표식 없는 답입니다."),
                ok_response(body="여전히 표식이 없습니다."),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        assert len(deps["runner"].calls) == 2
        assert deps["publisher"].posted == []
        assert outcome.ok is True
        assert outcome.silent is True
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "missing_recipient" in kinds
        retry_rows = [i for i in deps["audit"].incidents if i["kind"] == "missing_recipient_retry"]
        assert retry_rows and retry_rows[0]["recovered"] is False

    def test_재요청이_두_수신자를_섞으면_침묵한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="표식 없는 답입니다."),
                ok_response(body="[[RECIPIENT:ASKER]]\n[[RECIPIENT:OWNER]]"),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        assert deps["publisher"].posted == []
        assert outcome.silent is True

    def test_재요청이_침묵을_고르면_침묵한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="표식 없는 답입니다."),
                ok_response(body="[[침묵]]"),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        assert deps["publisher"].posted == []
        assert outcome.silent is True

    def test_재요청_엔진이_실패하면_침묵한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다."), fail_response()],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        assert deps["publisher"].posted == []
        assert outcome.silent is True
        assert outcome.ok is True

    def test_flat_채널도_안내문_없이_침묵한다(self, tmp_path: Path) -> None:
        from slack_cli_agent.config.channel import ChannelConfig

        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다."), ok_response(body="표식 없음")],
            require_recipient=True,
            channels={"C1": ChannelConfig(channel_id="C1", name="피드", flat_reply=True)},
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx(channel="C1"))
        assert deps["publisher"].posted == []

    def test_표식_있으면_재요청하지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="보고드립니다.\n[[RECIPIENT:OWNER]]")],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert len(deps["runner"].calls) == 1
        assert deps["publisher"].posted[0]["text"] == "보고드립니다."

    def test_플래그_꺼지면_표식없어도_재요청없이_게시한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다.")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert len(deps["runner"].calls) == 1
        assert deps["publisher"].posted[0]["text"] == "표식 없는 답입니다."
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "missing_recipient" not in kinds

    def test_재요청의_BOT표식이_봇이_아니면_침묵한다(self, tmp_path: Path) -> None:
        # BOT:ID 인데 그 ID 가 봇이 아니면 미해석(None)이다. 엉뚱한 멘션을 달지 않는다.
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="검토했습니다.\n[[RECIPIENT:BOT:UHUMAN]]"),
                ok_response(body="[[RECIPIENT:BOT:UHUMAN]]"),
            ],
            asker_is_bot=_is_bot(set()),
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        assert deps["publisher"].posted == []
        assert outcome.silent is True

    def test_재요청의_BOT표식이면_그_봇_멘션을_강제한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="검토했습니다."),
                ok_response(body="[[RECIPIENT:BOT:UBOT]]"),
            ],
            asker_is_bot=_is_bot({"UBOT"}),
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert deps["publisher"].posted[0]["text"] == "<@UBOT> 검토했습니다."


    def test_미해석_표식을_지우고_재요청_표식을_쓴다(self, tmp_path: Path) -> None:
        # 첫 답의 BOT:UHUMAN 이 남아 있으면 재요청이 고른 OWNER 대신 그것을 다시 읽는다.
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="검토했습니다.\n[[RECIPIENT:BOT:UHUMAN]]"),
                ok_response(body="[[RECIPIENT:OWNER]]"),
            ],
            asker_is_bot=_is_bot(set()),
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert [p["text"] for p in deps["publisher"].posted] == ["검토했습니다."]

    def test_대체엔진_턴이면_재요청하지_않고_침묵한다(self, tmp_path: Path) -> None:
        # 대체 엔진은 매번 새 세션이라 방금 답을 모른다. 표식을 고르게 할 수 없다.
        from dataclasses import replace
        from types import SimpleNamespace

        pipeline, deps = build_pipeline(
            responses=[replace(ok_response(body="표식 없는 답입니다."), engine="codex")],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline._engine.secondary = SimpleNamespace(name="codex")
        outcome = pipeline.handle(make_ctx())
        assert len(deps["runner"].calls) == 1
        assert deps["publisher"].posted == []
        assert outcome.silent is True


class TestApplication배선_J11:
    """부품(파이프라인)의 동작과 조립(Application)에서의 전달은 다르다 — J11 의
    원래 결함은 require_recipient 가 파이프라인엔 있었지만 application.py 가
    settings 에서 넘기지 않아 상시 꺼져 있던 것이다(readable_dirs 와 같은 함정).
    """

    def test_설정이_켜지면_파이프라인까지_전달된다(self, tmp_path: Path) -> None:
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.core.application import Application

        profile = write_profile(tmp_path, settings={"require_recipient": True})
        app = Application(profile, FakeSlackClient())
        assert app.pipeline()._require_recipient is True

    def test_설정이_없으면_파이프라인은_켜짐이다(self, tmp_path: Path) -> None:
        # B1: 페르소나가 표식을 항상 요구하므로(RECIPIENT_RULE) 기본값은 켜짐이다.
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.core.application import Application

        profile = write_profile(tmp_path)
        app = Application(profile, FakeSlackClient())
        assert app.pipeline()._require_recipient is True

    def test_설정으로_끌_수_있다(self, tmp_path: Path) -> None:
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.core.application import Application

        profile = write_profile(tmp_path, settings={"require_recipient": False})
        app = Application(profile, FakeSlackClient())
        assert app.pipeline()._require_recipient is False


class Test소유자_턴_미표식_폴백:
    """소유자 턴은 ASKER 가 곧 소유자라 잘못된 상대에게 갈 수 없다. 재요청까지
    표식이 없으면 본문을 버리지 않고 소유자에게 게시한다 (2026-11-09 private-taeil 20:31)."""

    def test_재요청에도_표식없으면_소유자에게_원본문을_게시한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[
                ok_response(body="표식 없는 답입니다."),
                fail_response(),
            ],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx(user="UOWNER"))
        assert [p["text"] for p in deps["publisher"].posted] == ["표식 없는 답입니다."]
        assert outcome.silent is False
        rows = [i for i in deps["audit"].incidents if i["kind"] == "missing_recipient_retry"]
        assert rows[-1]["tag"] == "owner_fallback"
        assert rows[-1]["recovered"] is True

    def test_소유자가_아니면_그대로_침묵한다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다."), fail_response()],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx(user="U1"))
        assert deps["publisher"].posted == []
        assert outcome.silent is True

    def test_본문이_표식뿐이면_폴백하지_않는다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="[[RECIPIENT:BOT:UNOTBOT]]"), fail_response()],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx(user="UOWNER"))
        assert deps["publisher"].posted == []
        assert outcome.silent is True
