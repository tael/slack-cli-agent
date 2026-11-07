"""슬랙 조회 전용 MCP 서버 — 읽기만 한다.

마메치의 `~/.mametchi/mcp/slack_readonly.py` 를 동작 기준으로 참고했다(읽기
전용, 수정하지 않음). 그 파일은 봇마다 홈 아래 손으로 복사해 두는 운영물이었고,
그래서 토큰 경로·채널 판정 같은 조직 고유값이 코드에 박혀 있었다. 여기서는
그 반대로 간다 — 이 파일은 설치물이고, 조직 고유값은 프로필의 `mcp_servers`
env 와 `SLACK_BOT_TOKEN`/`SLACK_USER_TOKEN`/자격 파일로만 받는다.

이 서버가 내는 도구는 하나, `slack_message_lookup` 뿐이다. 마메치의
slack_list_channels/slack_search/slack_post_message 같은 나머지 도구는
"공통 조회수단" 이 아니라 user_tools 등재값(부릉 특수 설정)이라 여기 두지
않는다(ownership.md 조언자 메모 참조).

재구성 로직(순수 함수)과 네트워크 I/O 를 갈라 둔다 — 시험이 가짜 payload 로
네트워크 없이 재구성 로직만 볼 수 있게 하기 위해서다.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..slack.attachments import attachment_body_texts, file_metadata, unfurled_messages
from ..slack.credentials import CredentialResolver
from ..slack.transcript import blocks_to_text

API = "https://slack.com/api/"
TIMEOUT = 20

# 프로필이 자격 파일 경로를 가리킬 때 쓰는 환경변수. MCP 서버는 별도
# 프로세스로 뜨므로 Profile 객체를 못 받는다 — 경로는 env 로만 넘어온다.
CREDENTIALS_FILE_ENV = "SLACK_CLI_AGENT_MCP_CREDENTIALS_FILE"
# 자격 파일 자체가 없을 때의 자리. 실제로는 환경변수나 설정된 경로로
# 대체되는 경우가 대부분이라 이 경로가 열릴 일은 드물다.
_DEFAULT_CREDENTIALS_PATH = Path.home() / ".slack-cli-agent" / "credentials.json"
# 이 턴이 프로필 소유자의 턴인지 알리는 환경변수. 프로필의 mcp_servers.<id>.turn_env
# 에 {"SLACK_CLI_AGENT_MCP_OWNER_TURN": "is_owner"} 로 이어 두면 소유자 턴에만
# "1" 이 실린다. 없으면 비소유자 턴으로 본다(D5).
OWNER_TURN_ENV = "SLACK_CLI_AGENT_MCP_OWNER_TURN"

# 봇이 그 채널을 "못 본다" 고 판단하는 오류들. 이 경우에만 사용자 토큰
# 폴백을 시도한다 — 그 밖의 오류(요율 제한, 인증 만료 등)는 사용자 토큰을
# 써도 같은 결과이거나 엉뚱한 재시도가 된다.
_NOT_VISIBLE_ERRORS = frozenset({"not_in_channel", "channel_not_found", "missing_scope"})


# ---------------------------------------------------------------------------
# 내용 재구성 — 순수 함수. payload 만 받고 네트워크를 타지 않는다.
# ---------------------------------------------------------------------------


def reconstruct_message(msg: Mapping[str, Any]) -> dict[str, Any]:
    """메시지 하나를 text 뿐 아니라 attachments/blocks/files/thread_ts 까지
    포함한 모양으로 펼친다.

    마메치 fmt() 의 관찰 가능한 모양을 따른다: blocks_text 는 text 가 빈
    메시지에만 채운다(알림 봇이 text 자리를 비워 두는 경우를 노린 것이라,
    text 가 있는 보통 메시지에까지 블록 본문을 겹쳐 내면 오히려 소음이다).
    """
    out: dict[str, Any] = {"ts": msg.get("ts"), "text": msg.get("text") or ""}
    if msg.get("subtype"):
        out["subtype"] = msg["subtype"]
    # "채널에도 보내기" 로 올린 스레드 답글(thread_broadcast)은 채널 기록에
    # 최상위 글처럼 섞여 나온다. thread_ts 가 ts 와 다를 때만 실어, 어느
    # 스레드의 말인지 알 수 있게 한다(2026-11-01).
    if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
        out["thread_ts"] = msg["thread_ts"]
    files = file_metadata(msg.get("files"))
    if files:
        out["files"] = list(files)
    unfurls = unfurled_messages(msg.get("attachments"))
    if unfurls:
        out["unfurled_messages"] = unfurls
    alerts = attachment_body_texts(msg.get("attachments"))
    if alerts:
        out["attachments_text"] = list(alerts)
    if not out["text"]:
        blocks_body = blocks_to_text(msg.get("blocks"))
        if blocks_body:
            out["blocks_text"] = blocks_body
    return out


# ---------------------------------------------------------------------------
# 토큰 선택 — 봇 토큰 우선, 채널을 못 보는 오류일 때만 사용자 토큰으로.
# ---------------------------------------------------------------------------

#: (method, params, token) -> 슬랙 API 응답. 실제 구현은 urllib 로 호출하고,
#: 시험은 네트워크 없이 가짜 dict 를 돌려주는 함수를 넣는다.
Caller = Callable[[str, Mapping[str, str], str], Mapping[str, Any]]


@dataclass(frozen=True)
class LookupResult:
    ok: bool
    message: Mapping[str, Any] | None = None
    used_user_token: bool = False
    reason: str = ""


def _find(resp: Mapping[str, Any], ts: str) -> Mapping[str, Any] | None:
    for m in resp.get("messages") or ():
        if m.get("ts") == ts:
            return cast("Mapping[str, Any]", m)
    return None


def owner_turn_from(env: Mapping[str, str]) -> bool:
    """turn_env 의 is_owner 게이트는 소유자 턴에 "1" 을 싣고 그 밖에는 변수를 뺀다."""
    return env.get(OWNER_TURN_ENV, "") == "1"


def _user_token_refusal(
    call: Caller, channel: str, user_token: str, owner_turn: bool
) -> str:
    """사용자 토큰으로 이 채널을 읽어도 되는지. 되면 빈 문자열, 안 되면 사유다.

    공개 채널은 조직 구성원 누구나 보는 곳이라 어느 턴이든 연다. 비공개 채널은
    소유자가 멤버인 곳까지 열리므로 소유자 턴에만 연다. 개인 대화는 열지 않는다.
    종류를 확인하지 못하면 열지 않는다(D5).
    """
    resp = call("conversations.info", {"channel": channel}, user_token)
    if not resp.get("ok"):
        return f"채널 종류를 확인하지 못해 사용자 토큰으로 읽지 않았다 : {resp.get('error') or '알 수 없는 오류'}"
    info = resp.get("channel") or {}
    if info.get("is_im") or info.get("is_mpim"):
        return "개인 대화는 조회하지 않는다."
    if info.get("is_private") and not owner_turn:
        return "봇이 초대되지 않은 비공개 채널은 소유자 턴에서만 사용자 세션으로 읽는다."
    return ""


def lookup_message(
    call: Caller,
    channel: str,
    ts: str,
    thread_ts: str | None = None,
    bot_token: str = "",
    user_token: str = "",
    owner_turn: bool = False,
) -> LookupResult:
    """channel+ts(옵션으로 thread_ts) 로 메시지 한 건을 찾는다.

    봇 토큰으로 먼저 본다. 봇이 그 채널에 없어서 막힌 경우(not_in_channel 등)
    에만, 설정된 사용자 토큰으로 한 번 더 본다. 공개 채널은 조직 구성원
    누구나 볼 수 있어 사용자 토큰으로 읽어도 접근 범위가 넓어지지 않는다
    (gaps.md C40). 비공개 채널은 owner_turn 일 때만, 개인 대화는 어느 턴에도
    사용자 토큰으로 읽지 않는다(D5). 사용자 토큰이 없으면 사유를 남기고
    물러선다.
    """
    params = {
        "channel": channel,
        # 답글이면 스레드 부모 ts 를 앵커로 conversations.replies 를 부른다.
        # 최상위 메시지는 thread_ts 가 없으므로 ts 자신이 앵커가 된다 —
        # 슬랙은 답글 없는 메시지도 자신만의 스레드로 다룬다.
        "ts": thread_ts or ts,
        # 그 중 정확히 이 ts 의 메시지로 좁힌다.
        "latest": ts,
        "oldest": ts,
        "inclusive": "true",
        "limit": "1",
    }

    def _try(token: str) -> tuple[Mapping[str, Any] | None, str]:
        resp = call("conversations.replies", params, token)
        if not resp.get("ok"):
            return None, str(resp.get("error") or "알 수 없는 오류")
        msg = _find(resp, ts)
        if msg is None:
            return None, "그 ts 의 메시지를 찾지 못했다. 답글이면 thread_ts 를 함께 준다."
        return msg, ""

    if bot_token:
        msg, bot_err = _try(bot_token)
        if msg is not None:
            return LookupResult(ok=True, message=msg)
        if bot_err not in _NOT_VISIBLE_ERRORS:
            return LookupResult(ok=False, reason=bot_err)
    else:
        bot_err = "봇 토큰이 설정되지 않았다."

    if not user_token:
        return LookupResult(ok=False, reason=f"{bot_err} 사용자 토큰 폴백이 설정되지 않았다.")

    refusal = _user_token_refusal(call, channel, user_token, owner_turn)
    if refusal:
        return LookupResult(ok=False, reason=f"봇 토큰 조회 실패({bot_err}). {refusal}")

    msg, user_err = _try(user_token)
    if msg is not None:
        return LookupResult(ok=True, message=msg, used_user_token=True)
    return LookupResult(
        ok=False,
        used_user_token=True,
        reason=f"봇 토큰 조회 실패({bot_err}) 후 사용자 토큰으로도 실패했다 : {user_err}",
    )


# ---------------------------------------------------------------------------
# 도구 등재 — JSON-RPC 쪽에 노출하는 도구는 이 파일 밖 어디에도 조직 고유값을
# 두지 않는다. 채널/토큰은 모두 호출 인자와 환경변수에서 온다.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "slack_message_lookup",
        "description": (
            "채널의 메시지 한 건을 channel+ts 로 조회한다. 최상위 text 뿐 아니라 "
            "attachments 본문, blocks 본문, 첨부 파일 메타데이터, 링크 미리보기 본문, "
            "thread_broadcast 의 부모 스레드 ts 까지 함께 돌려준다. 봇이 초대되지 "
            "않은 공개 채널은 사용자 토큰이 설정돼 있으면 그걸로 재조회한다. "
            "비공개 채널은 소유자 턴에서만 재조회하고 개인 대화는 조회하지 않는다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "채널 ID"},
                "ts": {"type": "string", "description": "조회할 메시지의 ts"},
                "thread_ts": {"type": "string", "description": "답글이면 스레드 부모 ts"},
            },
            "required": ["channel", "ts"],
        },
    },
]


def handle_lookup(
    arguments: Mapping[str, Any],
    *,
    call: Caller,
    bot_token: str,
    user_token: str,
    owner_turn: bool = False,
) -> dict[str, Any]:
    channel = str(arguments["channel"])
    ts = str(arguments["ts"])
    thread_ts = arguments.get("thread_ts")
    result = lookup_message(
        call, channel, ts,
        thread_ts=str(thread_ts) if thread_ts else None,
        bot_token=bot_token, user_token=user_token, owner_turn=owner_turn,
    )
    if not result.ok or result.message is None:
        return {"error": result.reason}
    out = reconstruct_message(result.message)
    out["channel"] = channel
    return out


HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "slack_message_lookup": handle_lookup,
}


# ---------------------------------------------------------------------------
# 네트워크 I/O + JSON-RPC 루프 — 여기서부터는 실제 프로세스로 띄워졌을 때만 돈다.
# ---------------------------------------------------------------------------


def _credentials() -> CredentialResolver:
    configured = os.environ.get(CREDENTIALS_FILE_ENV, "")
    return CredentialResolver(
        default_path=_DEFAULT_CREDENTIALS_PATH,
        configured_path=Path(configured).expanduser() if configured else None,
        env=os.environ,
    )


def _default_caller(
    timeout: int = TIMEOUT, *, user_token: str = "", user_cookie: str = ""
) -> Caller:
    """사용자 토큰 호출에만 d 쿠키를 싣는다. 브라우저 세션 토큰(xoxc)은 쿠키가
    없으면 invalid_auth 다. 봇 토큰 호출에 쿠키를 실을 이유가 없다."""

    def _call(method: str, params: Mapping[str, str], token: str) -> Mapping[str, Any]:
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        if user_cookie and user_token and token == user_token:
            headers["Cookie"] = "d=" + user_cookie
        req = urllib.request.Request(
            API + method, data=urllib.parse.urlencode(params).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return cast("Mapping[str, Any]", json.load(r))

    return _call


def _reply(msg_id: Any, result: Any = None, error: Any = None) -> None:
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    credentials = _credentials()
    bot_token = credentials.bot_token()
    user_token = credentials.user_token()
    owner_turn = owner_turn_from(os.environ)
    call = _default_caller(user_token=user_token, user_cookie=credentials.user_cookie())

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")

        if method == "initialize":
            _reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "slack-readonly", "version": "1.0.0"},
            })
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg.get("params") or {}
            tool_name = p.get("name")
            handler = HANDLERS.get(tool_name) if isinstance(tool_name, str) else None
            if not handler:
                _reply(msg_id, error={"code": -32601, "message": "알 수 없는 도구"})
                continue
            try:
                result = handler(
                    p.get("arguments") or {}, call=call, bot_token=bot_token,
                    user_token=user_token, owner_turn=owner_turn,
                )
                text = json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as e:  # noqa: BLE001 - 원인을 감추면 같은 오류가 되풀이된다
                text = f"슬랙 조회에 실패했다. {type(e).__name__}: {e}"
            _reply(msg_id, {"content": [{"type": "text", "text": text}]})
        elif msg_id is not None:
            _reply(msg_id, error={"code": -32601, "message": "지원하지 않는 메서드"})


if __name__ == "__main__":
    main()
