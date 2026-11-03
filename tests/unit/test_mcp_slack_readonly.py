"""슬랙 조회 MCP 서버 — text 뿐 아니라 attachments/blocks/files/링크 미리보기/
thread_broadcast 까지 복원하는지, 그리고 봇 미초대 채널에서 사용자 토큰 폴백이
도는지 본다 (gaps.md H9~H14, C39, C40).

2026-11-03 모니터링-부릉-내부 grafana 알림이 text 빈 값으로만 와 "어떤 알림인지
읽지 못했다" 고 답한 실패가 기준이다 (~/.mametchi/mcp/slack_readonly.py 대조).
네트워크는 전혀 타지 않는다 - call 과 transcript 를 모두 가짜로 넣는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_cli_agent.mcp.slack_readonly import (
    TOOLS,
    LookupResult,
    handle_lookup,
    lookup_message,
    reconstruct_message,
)
from slack_cli_agent.slack.attachments import (
    attachment_body_texts,
    file_metadata,
    is_unfurl_attachment,
    unfurl_matches,
    unfurled_messages,
)
from slack_cli_agent.slack.credentials import USER_TOKEN_ENV, CredentialResolver
from slack_cli_agent.slack.linked_threads import LinkedThread, LinkedThreadReader
from slack_cli_agent.slack.permalinks import SlackLink
from slack_cli_agent.slack.transcript import TranscriptRead

GRAFANA_ATTACHMENTS = [
    {
        "pretext": "DatasourceError 8건",
        "title": "모니터링-부릉-내부 알림",
        "text": "datasource 연결이 끊겼습니다.",
        "fields": [{"title": "severity", "value": "critical"}],
        "fallback": "폴백 본문",
    }
]

UNFURL_ATTACHMENTS = [
    {
        "is_msg_unfurl": True,
        "from_url": "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049",
        "author_name": "홍길동",
        "ts": "1788253544.408049",
        "text": "허지우님 요청 본문 전체",
    }
]


class Test첨부본문_순수함수:
    """attachments.py 에 추가한 재구성 함수들 — mametchi fmt()/blocks_text 와
    같은 판단 기준(펼침 미리보기 vs 경보성 첨부 구분)으로 동작해야 한다."""

    def test_언퍼얼_첨부만_링크_미리보기로_잡는다(self) -> None:
        assert is_unfurl_attachment(UNFURL_ATTACHMENTS[0]) is True
        assert is_unfurl_attachment(GRAFANA_ATTACHMENTS[0]) is False

    def test_경보성_첨부에서_본문을_되살린다(self) -> None:
        texts = attachment_body_texts(GRAFANA_ATTACHMENTS)
        assert len(texts) == 1
        assert "datasource 연결이 끊겼습니다." in texts[0]
        assert "DatasourceError 8건" in texts[0]
        assert "severity: critical" in texts[0]

    def test_언퍼얼_첨부는_경보성_본문에서_제외한다(self) -> None:
        assert attachment_body_texts(UNFURL_ATTACHMENTS) == ()

    def test_본문도_fallback_도_없으면_빈_튜플이다(self) -> None:
        assert attachment_body_texts([{"color": "#ff0000"}]) == ()

    def test_링크_미리보기_본문을_되살린다(self) -> None:
        out = unfurled_messages(UNFURL_ATTACHMENTS)
        assert out == (
            {
                "author": "홍길동",
                "ts": "1788253544.408049",
                "from_url": "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049",
                "text": "허지우님 요청 본문 전체",
            },
        )

    def test_경보성_첨부는_언퍼얼_본문에서_제외한다(self) -> None:
        assert unfurled_messages(GRAFANA_ATTACHMENTS) == ()

    def test_첨부가_없으면_둘_다_빈_튜플이다(self) -> None:
        assert attachment_body_texts(None) == ()
        assert unfurled_messages(None) == ()
        assert unfurled_messages([]) == ()

    def test_파일_메타데이터만_추린다(self) -> None:
        files = [
            {"id": "F1", "name": "식단표.png", "mimetype": "image/png", "url_private": "내부용"},
        ]
        assert file_metadata(files) == ({"id": "F1", "name": "식단표.png", "mimetype": "image/png"},)
        assert file_metadata(None) == ()

    def test_언퍼얼_첨부가_그_링크의_것인지_대조한다(self) -> None:
        link = SlackLink(channel="C0EXAMPLE01", ts="1788253544.408049", thread_ts="1788253544.408049")
        assert unfurl_matches(UNFURL_ATTACHMENTS[0], link.channel, link.thread_ts) is True

    def test_다른_채널의_언퍼얼은_대조에서_떨어진다(self) -> None:
        assert unfurl_matches(UNFURL_ATTACHMENTS[0], "C0OTHER000", "1788253544.408049") is False

    def test_경보성_첨부는_애초에_언퍼얼_대조_대상이_아니다(self) -> None:
        assert unfurl_matches(GRAFANA_ATTACHMENTS[0], "C0EXAMPLE01", "1788253544.408049") is False


class Test메시지_재구성:
    """reconstruct_message() 가 mametchi fmt() 와 같은 관찰 가능한 모양을 내는가."""

    def test_빈_text_알림을_attachments_로_복원한다(self) -> None:
        """2026-11-03 grafana 알림 실패 사례 그대로."""
        msg = {"ts": "1799999999.000001", "text": "", "attachments": GRAFANA_ATTACHMENTS}
        out = reconstruct_message(msg)
        assert out["text"] == ""
        assert len(out["attachments_text"]) == 1
        assert "datasource 연결이 끊겼습니다." in out["attachments_text"][0]
        assert "unfurled_messages" not in out

    def test_링크_공유_메시지는_언퍼얼_본문을_싣는다(self) -> None:
        msg = {"ts": "1788253599.000001", "text": "<링크>", "attachments": UNFURL_ATTACHMENTS}
        out = reconstruct_message(msg)
        assert out["unfurled_messages"] == (
            {
                "author": "홍길동",
                "ts": "1788253544.408049",
                "from_url": "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049",
                "text": "허지우님 요청 본문 전체",
            },
        )

    def test_파일만_있는_메시지는_files_를_낸다(self) -> None:
        msg = {
            "ts": "1788253599.000002", "text": "",
            "files": [{"id": "F1", "name": "식단표.png", "mimetype": "image/png"}],
        }
        out = reconstruct_message(msg)
        assert out["files"] == [{"id": "F1", "name": "식단표.png", "mimetype": "image/png"}]

    def test_thread_broadcast_는_부모_ts_를_같이_낸다(self) -> None:
        """2026-11-01 : 채널에도 보내기 로 올린 답글이 최상위 글처럼 섞여 나온 사례."""
        msg = {
            "ts": "1788253600.000001", "text": "채널에도 보냄",
            "thread_ts": "1788253544.408049", "subtype": "thread_broadcast",
        }
        out = reconstruct_message(msg)
        assert out["thread_ts"] == "1788253544.408049"
        assert out["subtype"] == "thread_broadcast"

    def test_스레드_부모_자신은_thread_ts_를_안_낸다(self) -> None:
        msg = {"ts": "1788253544.408049", "text": "부모글", "thread_ts": "1788253544.408049"}
        out = reconstruct_message(msg)
        assert "thread_ts" not in out

    def test_text_가_있으면_blocks_text_를_안_낸다(self) -> None:
        msg = {"ts": "1", "text": "이미 본문 있음", "blocks": [{"type": "header", "text": {"text": "제목"}}]}
        out = reconstruct_message(msg)
        assert "blocks_text" not in out

    def test_text_가_비어_있으면_blocks_에서_되살린다(self) -> None:
        msg = {
            "ts": "1", "text": "",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "블록 본문"}}],
        }
        out = reconstruct_message(msg)
        assert out["blocks_text"] == "블록 본문"


class Test조회_토큰_선택:
    """lookup_message() — 봇 토큰이 막히면 사용자 토큰으로, 그것도 없으면
    사유를 남기고 물러선다 (gaps.md C40)."""

    def _call(self, responses: dict[str, dict]):
        calls: list[tuple[str, str]] = []

        def _fn(method: str, params: dict, token: str) -> dict:
            calls.append((method, token))
            return responses[token]

        return _fn, calls

    def test_봇_토큰으로_바로_찾으면_사용자_토큰을_안_쓴다(self) -> None:
        call, calls = self._call({
            "봇토큰": {"ok": True, "messages": [{"ts": "1.1", "text": "본문"}]},
        })
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰", user_token="사용자토큰")
        assert result == LookupResult(ok=True, message={"ts": "1.1", "text": "본문"})
        assert calls == [("conversations.replies", "봇토큰")]

    def test_미초대_공개채널은_사용자_토큰으로_재조회한다(self) -> None:
        call, calls = self._call({
            "봇토큰": {"ok": False, "error": "not_in_channel"},
            "사용자토큰": {"ok": True, "messages": [{"ts": "1.1", "text": "본문"}]},
        })
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰", user_token="사용자토큰")
        assert result.ok is True
        assert result.used_user_token is True
        assert [m for m, _ in calls] == ["conversations.replies", "conversations.replies"]
        assert [t for _, t in calls] == ["봇토큰", "사용자토큰"]

    def test_사용자_토큰_미설정이면_사유를_남기고_물러선다(self) -> None:
        call, calls = self._call({"봇토큰": {"ok": False, "error": "not_in_channel"}})
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰", user_token="")
        assert result.ok is False
        assert "not_in_channel" in result.reason
        assert "사용자 토큰" in result.reason
        assert calls == [("conversations.replies", "봇토큰")]  # 사용자 토큰으로는 재시도 안 함

    def test_채널_보임과_무관한_오류는_사용자_토큰으로_재시도하지_않는다(self) -> None:
        call, calls = self._call({"봇토큰": {"ok": False, "error": "rate_limited"}})
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰", user_token="사용자토큰")
        assert result.ok is False
        assert "rate_limited" in result.reason
        assert calls == [("conversations.replies", "봇토큰")]

    def test_둘_다_실패하면_둘_다의_사유를_남긴다(self) -> None:
        call, _ = self._call({
            "봇토큰": {"ok": False, "error": "not_in_channel"},
            "사용자토큰": {"ok": False, "error": "invalid_auth"},
        })
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰", user_token="사용자토큰")
        assert result.ok is False
        assert "not_in_channel" in result.reason
        assert "invalid_auth" in result.reason

    def test_봇_토큰도_사용자_토큰도_없으면_그렇게_말한다(self) -> None:
        call, calls = self._call({})
        result = lookup_message(call, "C1", "1.1", bot_token="", user_token="")
        assert result.ok is False
        assert calls == []

    def test_그_ts_의_메시지를_못_찾으면_사유를_남긴다(self) -> None:
        call, _ = self._call({"봇토큰": {"ok": True, "messages": [{"ts": "9.9", "text": "다른 글"}]}})
        result = lookup_message(call, "C1", "1.1", bot_token="봇토큰")
        assert result.ok is False
        assert "찾지 못했다" in result.reason

    def test_thread_ts_가_있으면_부모_ts_를_앵커로_조회한다(self) -> None:
        seen = {}

        def _fn(method, params, token):
            seen.update(params)
            return {"ok": True, "messages": [{"ts": "1.2", "text": "답글"}]}

        result = lookup_message(_fn, "C1", "1.2", thread_ts="1.1", bot_token="봇토큰")
        assert result.ok is True
        assert seen["ts"] == "1.1"  # 스레드 부모를 앵커로 conversations.replies 를 부른다
        assert seen["latest"] == "1.2"  # 찾는 메시지 자체는 latest/oldest 로 고정한다
        assert seen["oldest"] == "1.2"


class Test도구_핸들러:
    def test_정상_조회는_채널과_재구성_결과를_낸다(self) -> None:
        def _call(method, params, token):
            return {"ok": True, "messages": [{"ts": "1.1", "text": "", "attachments": GRAFANA_ATTACHMENTS}]}

        out = handle_lookup({"channel": "C1", "ts": "1.1"}, call=_call, bot_token="봇", user_token="")
        assert out["channel"] == "C1"
        assert len(out["attachments_text"]) == 1

    def test_실패하면_error_키_하나만_낸다(self) -> None:
        def _call(method, params, token):
            return {"ok": False, "error": "not_in_channel"}

        out = handle_lookup({"channel": "C1", "ts": "1.1"}, call=_call, bot_token="봇", user_token="")
        assert set(out) == {"error"}
        assert "not_in_channel" in out["error"]

    def test_도구_목록에_조회_도구가_하나_있다(self) -> None:
        names = [t["name"] for t in TOOLS]
        assert "slack_message_lookup" in names
        tool = next(t for t in TOOLS if t["name"] == "slack_message_lookup")
        assert set(tool["inputSchema"]["required"]) == {"channel", "ts"}


class Test사용자_토큰_자격:
    """credentials.py 에 추가한 user_token — bot_token/app_token 과 같은
    우선순위(인자 > 환경변수 > 파일)로 읽혀야 한다 (gaps.md C40)."""

    def _해석기(self, tmp_path: Path, env: dict | None = None) -> CredentialResolver:
        return CredentialResolver(
            default_path=tmp_path / "credentials.json", configured_path=None, env=env or {},
        )

    def test_환경변수로_읽는다(self, tmp_path: Path) -> None:
        resolver = self._해석기(tmp_path, env={USER_TOKEN_ENV: "오너토큰"})
        assert resolver.user_token() == "오너토큰"

    def test_파일에서도_읽는다(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({"bot_token": "봇", "user_token": "오너파일토큰"}), encoding="utf-8")
        path.chmod(0o600)
        assert self._해석기(tmp_path).user_token() == "오너파일토큰"

    def test_설정_안_하면_빈_문자열이다(self, tmp_path: Path) -> None:
        assert self._해석기(tmp_path).user_token() == ""

    def test_인자가_가장_앞이다(self, tmp_path: Path) -> None:
        resolver = self._해석기(tmp_path, env={USER_TOKEN_ENV: "환경값"})
        assert resolver.user_token(given="인자값") == "인자값"


class Test링크된스레드_attachments_폴백:
    """C39 — 실시간 조회가 실패한 링크는 이벤트에 이미 실려 온 언퍼얼
    attachments 로 본문을 복원한다 (봇 미초대 비공개 채널 링크 공유 사례)."""

    MSG_LINK = "https://example.slack.com/archives/C0EXAMPLE01/p1788253544408049"

    class _실패하는_대역:
        def read_thread(self, channel: str, thread_ts: str, before_ts: object = None) -> TranscriptRead:
            return TranscriptRead(body="", read_ok=False)

    def _reader(self) -> LinkedThreadReader:
        return LinkedThreadReader(
            transcript=self._실패하는_대역(), channel_name=lambda c: "테스트", max_links=3,
        )

    def test_조회_실패해도_첨부로_본문을_되살린다(self) -> None:
        reader = self._reader()
        out = reader.of(self.MSG_LINK, self_channel="C099", attachments=UNFURL_ATTACHMENTS)
        assert out == (LinkedThread(name="테스트", body="허지우님 요청 본문 전체", read_ok=True),)

    def test_맞는_첨부가_없으면_그대로_읽기_실패다(self) -> None:
        reader = self._reader()
        다른_채널_첨부 = [{**UNFURL_ATTACHMENTS[0], "from_url": "https://example.slack.com/archives/C0OTHER000/p1788253544408049"}]
        out = reader.of(self.MSG_LINK, self_channel="C099", attachments=다른_채널_첨부)
        assert out == (LinkedThread(name="테스트", body="", read_ok=False),)

    def test_attachments_를_안_주면_기존과_같다(self) -> None:
        reader = self._reader()
        assert reader.of(self.MSG_LINK, self_channel="C099") == (
            LinkedThread(name="테스트", body="", read_ok=False),
        )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
