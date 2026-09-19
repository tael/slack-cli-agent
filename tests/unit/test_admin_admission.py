"""두 입구가 같은 관리 명령 판정을 쓴다 (sca-oyku).

소켓으로 들어온 요청은 AdminRouter 를 거치는데 캐치업으로 회수한 요청은
안 거쳤다. 그래서 봇이 꺼져 있는 동안 받은 !ping 이 회수된 뒤 명령이 아니라
모델 요청으로 갔다. 원본은 handle_request 안에서 handle_admin 을 먼저
부르고, 캐치업도 그 handle_request 를 부른다(bot.py:4990, 7082).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_admin import make_profile

from slack_cli_agent.admin.admission import AdminAdmission
from slack_cli_agent.admin.command import AdminContext, AdminResult
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext


class 대역라우터:
    def __init__(self, result: AdminResult | None) -> None:
        self._result = result
        self.본문: list[str] = []

    def dispatch(self, text: str, ctx: AdminContext) -> AdminResult | None:
        self.본문.append(text)
        return self._result


def 요청(text: str = "!ping") -> RequestContext:
    return RequestContext(channel="C1", user="U1", ts="1.1", thread_ts="1.0", text=text)


def 관리맥락(tmp_path: Path):
    profile = make_profile(tmp_path)
    channels = ChannelRegistry(Path("/nonexistent.json"))

    def build(ctx: RequestContext) -> AdminContext:
        return AdminContext(
            principal=Principal(
                user_id=ctx.user, channel=ctx.channel, trust=TrustLevel.OWNER,
                is_direct_message=ctx.is_direct_message,
            ),
            channel=ctx.channel, thread_ts=ctx.thread_ts,
            channels=channels, profile=profile, text=ctx.text,
        )

    return build


def 판정기(
    tmp_path: Path, result: AdminResult | None, 보냄: list[Any] | None = None
) -> AdminAdmission:
    보냄 = 보냄 if 보냄 is not None else []
    return AdminAdmission(
        router=대역라우터(result),
        context_builder=관리맥락(tmp_path),
        reply=lambda channel, thread_ts, message: 보냄.append((channel, thread_ts, message)),
    )


class Test명령이면_그_자리에서_끝낸다:
    def test_명령이면_참을_돌려준다(self, tmp_path: Path) -> None:
        assert 판정기(tmp_path, AdminResult(message="pong")).handled(요청()) is True

    def test_명령이면_답을_보낸다(self, tmp_path: Path) -> None:
        보냄: list[Any] = []
        판정기(tmp_path, AdminResult(message="pong"), 보냄).handled(요청())
        assert 보냄 == [("C1", "1.0", "pong")]

    def test_명령이_아니면_거짓을_돌려준다(self, tmp_path: Path) -> None:
        assert 판정기(tmp_path, None).handled(요청("오늘 날씨")) is False

    def test_명령이_아니면_아무것도_안_보낸다(self, tmp_path: Path) -> None:
        보냄: list[Any] = []
        판정기(tmp_path, None, 보냄).handled(요청("오늘 날씨"))
        assert 보냄 == []

    def test_권한이_모자란_명령도_그_자리에서_끝낸다(self, tmp_path: Path) -> None:
        """모델에게 넘기면 권한 없는 명령이 모델 요청으로 처리된다."""
        판정 = 판정기(tmp_path, AdminResult(message="권한 없음", handled=False))
        assert 판정.handled(요청()) is True

    def test_답을_못_보내도_명령_처리는_유지된다(self, tmp_path: Path) -> None:
        """게시 실패로 요청이 모델에게 넘어가면 같은 명령이 두 번 처리된다."""
        def 터짐(channel: str, thread_ts: str, message: str) -> None:
            raise RuntimeError("슬랙 오류")

        판정 = AdminAdmission(
            router=대역라우터(AdminResult(message="pong")),
            context_builder=관리맥락(tmp_path),
            reply=터짐,
        )
        assert 판정.handled(요청()) is True


class Test두_입구가_같은_판정을_쓴다:
    """한쪽만 고치면 다시 갈라진다. 조립이 같은 클래스를 양쪽에 꽂는지 본다
    (sca-oyku)."""

    def test_접수기가_이_클래스로_판정한다(self) -> None:
        import inspect

        from slack_cli_agent.core import ingress

        본문 = inspect.getsource(ingress.IngressService)
        assert "AdminAdmission" in 본문 or "self._admin.handled" in 본문

    def test_워커도_이_클래스를_받는다(self) -> None:
        import inspect

        from slack_cli_agent.core.worker import Worker

        assert "admin" in inspect.signature(Worker.__init__).parameters

    def test_조립이_양쪽에_같은_판정기를_꽂는다(self) -> None:
        import inspect

        from slack_cli_agent.core.application import Application

        본문 = inspect.getsource(Application)
        assert 본문.count("self._admin_admission()") >= 2
