"""[[RECIPIENT:...]] 해석·멘션 강제·미표식 차단의 파이프라인 배선 (D3/J11).

마커 파싱과 멘션 강제 자체는 test_bot_interaction.py(RecipientResolver)가,
표식 제거 배선은 test_pipeline_publish_markers.py가 본다. 여기서는 게시 직전
파이프라인이 수신자를 실제로 해석해 (1) 봇 수신자에 첫머리 멘션을 강제하고
(2) require_recipient 플래그가 켜졌을 때 표식 없는 답을 게시하지 않고 안내만
올리는지(J11)를 본다. bot.py:7860-7907 take_recipient_tag + NOTICE_NO_RECIPIENT.
"""

from __future__ import annotations

from pathlib import Path

from test_pipeline import build_pipeline, make_ctx, ok_response

from slack_cli_agent.core.pipeline import NOTICE_NO_RECIPIENT


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
        assert posted.startswith("<@UPEER> ")

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


class Test미표식_차단_J11:
    def test_플래그_켜고_표식없으면_본문대신_안내만_올린다(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다.")],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        outcome = pipeline.handle(make_ctx())
        posted = [p["text"] for p in deps["publisher"].posted]
        assert posted == [NOTICE_NO_RECIPIENT]
        assert "표식 없는 답입니다." not in NOTICE_NO_RECIPIENT
        assert outcome.ok is True
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "missing_recipient" in kinds

    def test_플래그_켜도_표식_있으면_정상_게시(self, tmp_path: Path) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="보고드립니다.\n[[RECIPIENT:OWNER]]")],
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert deps["publisher"].posted[0]["text"] == "보고드립니다."

    def test_플래그_꺼지면_표식없어도_게시한다(self, tmp_path: Path) -> None:
        # 기본값. owner/asker 설정이 없으면 "그냥 게시"로 degrade, 침묵으로 가지 않는다.
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="표식 없는 답입니다.")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert deps["publisher"].posted[0]["text"] == "표식 없는 답입니다."
        kinds = [i["kind"] for i in deps["audit"].incidents]
        assert "missing_recipient" not in kinds

    def test_봇아닌_ID의_BOT표식은_미해석이라_차단된다(self, tmp_path: Path) -> None:
        # BOT:ID 인데 그 ID 가 봇이 아니면 엉뚱한 멘션 대신 미해석(None)으로 본다.
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="검토했습니다.\n[[RECIPIENT:BOT:UHUMAN]]")],
            asker_is_bot=_is_bot(set()),
            require_recipient=True,
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        assert deps["publisher"].posted[0]["text"] == NOTICE_NO_RECIPIENT


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

    def test_설정이_없으면_파이프라인도_꺼짐이다(self, tmp_path: Path) -> None:
        from test_application import FakeSlackClient, write_profile

        from slack_cli_agent.core.application import Application

        profile = write_profile(tmp_path)
        app = Application(profile, FakeSlackClient())
        assert app.pipeline()._require_recipient is False
