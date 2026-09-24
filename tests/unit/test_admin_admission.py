"""두 입구가 같은 관리 명령 판정을 쓴다 (sca-oyku).

소켓으로 들어온 요청은 AdminRouter 를 거치는데 캐치업으로 회수한 요청은
안 거쳤다. 그래서 봇이 꺼져 있는 동안 받은 !ping 이 회수된 뒤 명령이 아니라
모델 요청으로 갔다. 원본은 handle_request 안에서 handle_admin 을 먼저
부르고, 캐치업도 그 handle_request 를 부른다(bot.py:4990, 7082).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_admin import make_profile

from slack_cli_agent.admin.admission import AdminAdmission
from slack_cli_agent.admin.command import AdminContext, AdminResult
from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.config.channel import ChannelRegistry
from slack_cli_agent.core.context import RequestContext
from slack_cli_agent.storage.admin_claims import AdminClaims, ClaimState


class 대역라우터:
    def __init__(self, result: AdminResult | None) -> None:
        self._result = result
        self.본문: list[str] = []

    def matches(self, text: str) -> bool:
        return self._result is not None

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
        판정 = 판정기(tmp_path, AdminResult(message="권한 없음", applied=False))
        assert 판정.handled(요청()) is True

    def test_아무것도_안_바꾼_명령도_그_자리에서_끝낸다(self, tmp_path: Path) -> None:
        """AdminResult.applied 는 설정을 바꿨는지를 뜻한다. 요청이 끝났는지가
        아니라서 판정기는 이 값을 보지 않는다(sca-id1h)."""
        판정 = 판정기(tmp_path, AdminResult(message="목록에 없습니다", applied=False))
        assert 판정.handled(요청()) is True

    def test_아무것도_안_바꿔도_답은_보낸다(self, tmp_path: Path) -> None:
        보냄: list[Any] = []
        판정기(
            tmp_path, AdminResult(message="목록에 없습니다", applied=False), 보냄
        ).handled(요청())
        assert 보냄 == [("C1", "1.0", "목록에 없습니다")]

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

    def test_조립이_표식기를_꽂는다(self) -> None:
        """주입을 빠뜨리면 표식이 안 달려 캐치업이 명령을 다시 실행한다."""
        import inspect

        from slack_cli_agent.core.application import Application

        본문 = inspect.getsource(Application._admin_admission)
        assert "markers=self.reactions()" in 본문


class Test명령을_처리했으면_완료_표식을_단다:
    """표식이 없으면 캐치업이 그 메시지를 미처리로 보고 다시 잡는다. 접수기
    프로세스의 중복 방지 기록은 워커 프로세스에 없어 캐치업을 못 막는다.
    원본도 handle_admin 직후 white_check_mark 를 단다(bot.py:4989) (sca-sk9t).
    """

    def _표식(self, tmp_path: Path, result: AdminResult | None) -> list[Any]:
        from slack_cli_agent.slack.reactions import ReactionMarker

        찍힘: list[Any] = []

        class 대역클라이언트:
            def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
                찍힘.append(("add", channel, timestamp, name))

            def reactions_remove(self, channel: str, timestamp: str, name: str) -> None:
                찍힘.append(("remove", channel, timestamp, name))

        판정 = AdminAdmission(
            router=대역라우터(result),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
            markers=ReactionMarker(대역클라이언트()),
        )
        판정.handled(요청())
        return 찍힘

    def test_명령이면_완료_표식을_단다(self, tmp_path: Path) -> None:
        assert ("add", "C1", "1.1", "white_check_mark") in self._표식(
            tmp_path, AdminResult(message="pong")
        )

    def test_명령이_아니면_표식을_안_단다(self, tmp_path: Path) -> None:
        assert self._표식(tmp_path, None) == []

    def test_표식_없이도_판정은_돌아간다(self, tmp_path: Path) -> None:
        """조립이 주입을 빠뜨려도 명령이 모델로 새지는 않는다."""
        판정 = AdminAdmission(
            router=대역라우터(AdminResult(message="pong")),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
        )
        assert 판정.handled(요청()) is True


class Test명령일_때만_점유한다:
    """점유는 DB 쓰기다. 매 요청마다 걸면 소켓 처리기가 DB 락에 묶인다
    (sca-9l1). 명령으로 매치되는 것만 집는다 (sca-8m5p)."""

    def _판정(self, tmp_path, claims, result=..., router=None):
        if result is ...:
            result = AdminResult(message="pong")
        return AdminAdmission(
            router=router or 대역라우터(result),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
            claims=claims,
            owner="ingress",
        )

    def test_명령이_아니면_점유를_안_한다(self, tmp_path: Path, database) -> None:
        claims = AdminClaims(database)
        self._판정(tmp_path, claims, result=None).handled(요청("오늘 날씨"))
        assert claims.state("C1", "1.1") is None

    def test_명령이면_점유하고_완료로_닫는다(self, tmp_path: Path, database) -> None:
        claims = AdminClaims(database)
        self._판정(tmp_path, claims).handled(요청())
        assert claims.state("C1", "1.1") is ClaimState.DONE

    def test_이미_집힌_명령은_다시_실행하지_않는다(self, tmp_path: Path, database) -> None:
        """다른 프로세스가 같은 명령을 이미 실행했다. 두 번 돌면 결과가
        달라지는 명령이 있다."""
        claims = AdminClaims(database)
        claims.claim("C1", "1.1", owner="worker")
        라우터 = 대역라우터(AdminResult(message="pong"))
        판정 = self._판정(tmp_path, claims, router=라우터)
        assert 판정.handled(요청()) is True
        assert 라우터.본문 == []

    def test_실행_중_터지면_실패로_닫고_참을_돌려준다(self, tmp_path: Path, database) -> None:
        """모델로 넘기면 이미 일어난 부작용이 두 번 일어난다."""
        claims = AdminClaims(database)

        class 터지는라우터:
            def matches(self, text: str) -> bool:
                return True

            def dispatch(self, text, ctx):
                raise RuntimeError("터짐")

        판정 = self._판정(tmp_path, claims, router=터지는라우터())
        assert 판정.handled(요청()) is True
        assert claims.state("C1", "1.1") is ClaimState.FAILED

    def test_원장이_없어도_판정은_돌아간다(self, tmp_path: Path) -> None:
        """조립이 주입을 빠뜨려도 명령이 모델로 새지는 않는다."""
        판정 = AdminAdmission(
            router=대역라우터(AdminResult(message="pong")),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
        )
        assert 판정.handled(요청()) is True


class Test원장이_안_되면_실행하지_않는다:
    """점유를 못 쓰면 다른 프로세스도 같은 명령을 실행할 수 있다. 삼키지 않고
    올려 보내 부르는 쪽이 복구를 정한다 (코덱스 리뷰)."""

    def _판정(self, tmp_path, claims, markers=None, 라우터=None):
        return AdminAdmission(
            router=라우터 or 대역라우터(AdminResult(message="pong")),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
            markers=markers,
            claims=claims,
            owner="ingress",
        )

    class _터지는원장:
        def claim(self, channel, ts, *, owner):
            raise RuntimeError("DB 오류")

        def finish(self, channel, ts, *, owner, ok, failure=""):
            raise RuntimeError("DB 오류")

    def test_점유를_못_하면_명령을_안_돌린다(self, tmp_path: Path) -> None:
        from slack_cli_agent.admin.admission import ClaimUnavailable

        라우터 = 대역라우터(AdminResult(message="pong"))
        판정 = self._판정(tmp_path, self._터지는원장(), 라우터=라우터)
        with pytest.raises(ClaimUnavailable):
            판정.handled(요청())
        assert 라우터.본문 == []

    def test_점유를_못_하면_표식도_안_단다(self, tmp_path: Path) -> None:
        """표식을 달면 캐치업이 처리됐다고 보고 다시 안 찾는다."""
        from slack_cli_agent.slack.reactions import ReactionMarker

        찍힘: list[Any] = []

        class 대역클라이언트:
            def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
                찍힘.append(name)

            def reactions_remove(self, channel: str, timestamp: str, name: str) -> None:
                pass

        from slack_cli_agent.admin.admission import ClaimUnavailable

        판정 = self._판정(tmp_path, self._터지는원장(), markers=ReactionMarker(대역클라이언트()))
        with pytest.raises(ClaimUnavailable):
            판정.handled(요청())
        assert 찍힘 == []


class Test회수된_뒤에는_완료_표식을_안_단다:
    """회수기가 실패로 닫고 x 를 달았는데 뒤늦게 완료 표식을 덮으면 회수가
    없던 일이 된다 (코덱스 리뷰)."""

    def test_종료를_못_하면_표식을_안_단다(self, tmp_path: Path, database) -> None:
        from slack_cli_agent.slack.reactions import ReactionMarker

        찍힘: list[Any] = []

        class 대역클라이언트:
            def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
                찍힘.append(name)

            def reactions_remove(self, channel: str, timestamp: str, name: str) -> None:
                pass

        claims = AdminClaims(database)

        class 느린라우터:
            def matches(self, text: str) -> bool:
                return True

            def dispatch(self, text, ctx):
                claims.reclaim_stale(claims._now() + 1)
                return AdminResult(message="pong")

        판정 = AdminAdmission(
            router=느린라우터(),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
            markers=ReactionMarker(대역클라이언트()),
            claims=claims,
            owner="ingress",
        )
        판정.handled(요청())
        assert "white_check_mark" not in 찍힘


class Test매치했는데_결과가_없으면:
    def test_원장을_닫고_모델로_넘긴다(self, tmp_path: Path, database) -> None:
        """RUNNING 으로 남기면 회수 순회가 죽은 프로세스로 잘못 본다."""
        claims = AdminClaims(database)

        class 빈결과라우터:
            def matches(self, text: str) -> bool:
                return True

            def dispatch(self, text, ctx):
                return None

        판정 = AdminAdmission(
            router=빈결과라우터(),
            context_builder=관리맥락(tmp_path),
            reply=lambda channel, thread_ts, message: None,
            claims=claims,
            owner="ingress",
        )
        assert 판정.handled(요청()) is False
        assert claims.state("C1", "1.1") is ClaimState.DONE
