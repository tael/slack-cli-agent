"""내부 표식 제거 전수화와 누출 감사 (B4, bot.py:1564-1614 strip_control_tags,
bot.py:6511-6530 post 의 control_tag_leak / empty_after_tags).

마메치는 발신 직전 post() 한곳에서 모든 내부 표식을 떼고 감사에 남긴다.
여기서는 같은 동작을 세 층에서 본다.
- core/markers.strip_control_tags : 알려진 표식 전부와 미선언 표식을 뗀다
- core/pipeline : 본답변 경로에서 알려진 표식이 남으면 control_tag_leak 을 기록한다
- slack/publisher.post : 감시 보고 등 다른 경로도 마지막 관문에서 걸러진다
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_pipeline import build_pipeline, make_ctx, ok_response
from test_slack import (  # noqa: F401 - pytest fixtures
    FakeWebClient,
    block_builder,
    make_publisher,
    markdown,
    settings,
    splitter,
    verifier,
)

from slack_cli_agent.core.markers import strip_control_tags


class Test표식_전수_제거:
    @pytest.mark.parametrize(
        "tag",
        [
            "[[ASK]]",
            "[[NOWATCH]]",
            "[[침묵]]",
            "[[WATCH_DONE]]",
            "[[WATCH_STILL]]",
            "[[WATCH_SUPERSEDED]]",
            "[[WATCH: 배포 끝나면 확인]]",
            "[[WATCH@2026-11-10: 정산 결과 확인]]",
            "[[RECIPIENT:OWNER]]",
            "[[RECIPIENT:ASKER]]",
            "[[RECIPIENT:BOT:U0ABC123]]",
        ],
    )
    def test_알려진_표식_단독줄은_모두_떼고_기록한다(self, tag: str) -> None:
        text, dropped = strip_control_tags(f"앞 문장입니다.\n{tag}\n뒤 문장입니다.")
        assert text == "앞 문장입니다.\n뒤 문장입니다."
        assert dropped == [tag]

    def test_미선언_표식은_unknown_접두로_기록한다(self) -> None:
        text, dropped = strip_control_tags("본문\n[[깜짝표식]]")
        assert text == "본문"
        assert dropped == ["unknown:[[깜짝표식]]"]

    def test_여러_줄에_걸친_맨끝_WATCH_를_뗀다(self) -> None:
        text, dropped = strip_control_tags("본문입니다.\n\n[[WATCH: 첫 줄\n둘째 줄]]")
        assert text == "본문입니다."
        assert dropped == ["[[WATCH: 첫 줄\n둘째 줄]]"]

    def test_문장_안의_표식과_코드블록은_건드리지_않는다(self) -> None:
        body = "규약은 [[NOWATCH]] 를 붙이는 것이다.\n```\n[[ASK]]\n[[깜짝]]\n```"
        text, dropped = strip_control_tags(body)
        assert text == body
        assert dropped == []

    def test_표식이_없으면_그대로다(self) -> None:
        assert strip_control_tags("평범한 답") == ("평범한 답", [])
        assert strip_control_tags("") == ("", [])


class Test본답변_누출_감사:
    def test_본문_중간에_남은_알려진_표식은_떼고_control_tag_leak_으로_기록한다(
        self, tmp_path: Path
    ) -> None:
        pipeline, deps = build_pipeline(
            responses=[ok_response(body="첫 문단입니다.\n[[WATCH_SUPERSEDED]]\n둘째 문단입니다.")],
            tmp_path=tmp_path,
        )
        pipeline.handle(make_ctx())
        posted = deps["publisher"].posted[0]["text"]
        assert "[[" not in posted
        leaks = [i for i in deps["audit"].incidents if i["kind"] == "control_tag_leak"]
        assert leaks and leaks[0]["tags"] == ["[[WATCH_SUPERSEDED]]"]


class Test게시_마지막_관문:
    def test_다른_경로의_본문도_표식을_떼고_감사에_남긴다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        client.queue_post({"ts": "111.000000"})
        audit: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=audit
        )
        pub.post("C1", "100.0", "감시 대상이 끝났습니다.\n[[NOWATCH]]", rich=False)
        _, kwargs = client.calls[0]
        assert kwargs["text"] == "감시 대상이 끝났습니다."
        leaks = [a for a in audit if a["kind"] == "control_tag_leak"]
        assert leaks and leaks[0]["tags"] == ["[[NOWATCH]]"]

    def test_표식만_남은_본문은_게시하지_않고_기록한다(
        self, settings, markdown, splitter, verifier, block_builder
    ) -> None:
        client = FakeWebClient()
        audit: list = []
        pub = make_publisher(
            client, settings, markdown, splitter, verifier, block_builder, audit=audit
        )
        assert pub.post("C1", "100.0", "[[WATCH_DONE]]\n[[NOWATCH]]", rich=True) is None
        assert client.calls == []
        assert any(a["kind"] == "empty_after_tags" for a in audit)
