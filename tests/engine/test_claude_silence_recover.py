"""침묵 앞의 위임 답을 session transcript 에서 되살린다.

2026-11-15 휴가 위임 거짓 보고, #226 은 dict 출력에서 무동작. 실제
`claude -p --output-format json` 은 이벤트 배열이 아니라 result 단일 객체를 내므로
앞 답은 ~/.claude/projects/<slug>/<session_id>.jsonl 에만 있다. fixture 는 사건
transcript(5c3c1d32...) 에서 해당 턴 구간만 잘라 본문을 비운 것이다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from slack_cli_agent.auth.principal import TrustLevel
from slack_cli_agent.config.profile import Profile
from slack_cli_agent.config.settings import RuntimeSettings
from slack_cli_agent.engine.base import EngineRequest
from slack_cli_agent.engine.claude import SILENCE_RECOVERED_RAW_KEY, ClaudeEngine
from slack_cli_agent.engine.tool_selection import ToolSelection

FIXTURE = Path(__file__).parent.parent / "fixtures" / "claude" / "silence_after_handoff_20261115.jsonl"
WORKDIR = Path("/Users/Shared/guchipachi-work")
SLUG = "-Users-Shared-guchipachi-work"
SESSION = "5c3c1d32-eb54-47c6-ba6c-40e07292e022"


def engine() -> ClaudeEngine:
    profile = Profile.from_dict({
        "name": "t",
        "primary_engine": {"type": "claude", "binary": "claude", "model": "claude-opus-5"},
        "state_dir": "/tmp/engine-parse-test",
    })
    return ClaudeEngine(profile, RuntimeSettings())


def request() -> EngineRequest:
    return EngineRequest(
        prompt="p", system_prompt="s", session_id=SESSION, resume=False, model="m",
        effort="medium", workdir=WORKDIR, readable_dirs=(), tools=ToolSelection.allow(["Read"]),
        trust_level=TrustLevel.GENERAL,
    )


def single_dict_stdout(result: str = "[[침묵]]") -> str:
    # 2026-11-15 실측: 배열이 아니라 단일 dict
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": result,
        "session_id": SESSION, "num_turns": 3, "usage": {"input_tokens": 2, "output_tokens": 30},
    }, ensure_ascii=False)


def put_transcript(home: Path, rows: list[dict[str, Any]]) -> None:
    path = home / ".claude" / "projects" / SLUG / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def fixture_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def run(home: Path) -> Any:
    eng = engine()
    response = eng.parse(single_dict_stdout(), "", 0)
    return eng.recover_silenced_answer(request(), response, {"HOME": str(home)})


def text_row(role: str, text: str, ts: str) -> dict[str, Any]:
    content: Any = [{"type": "text", "text": text}] if role == "assistant" else text
    return {"type": role, "isSidechain": False, "timestamp": ts,
            "message": {"role": role, "content": content}}


def test_사건_transcript_단일_dict_출력에서_위임_답을_채택한다(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    put_transcript(tmp_path, fixture_rows())

    with caplog.at_level(logging.WARNING):
        response = run(tmp_path)

    assert response.ok
    assert "[[RECIPIENT:BOT:U0BS6LBPHJ6]]" in response.body
    assert response.body.startswith("<@U0BS6LBPHJ6> 오너 휴가신청 상신 부탁드림.")
    assert "[[침묵]]" not in response.body
    assert response.raw[SILENCE_RECOVERED_RAW_KEY] is True
    assert "silence_recovered" in caplog.text


def test_task_notification_이_아닌_user_메시지가_끼면_채택하지_않는다(tmp_path: Path) -> None:
    rows = fixture_rows()
    # task-notification 자리를 사람이 한 말로 바꾼다
    rows[4] = text_row("user", "그거 말고 다른 걸로 해줘", rows[4]["timestamp"])
    put_transcript(tmp_path, rows)

    response = run(tmp_path)

    assert response.body == "[[침묵]]"
    assert SILENCE_RECOVERED_RAW_KEY not in response.raw


def test_task_notification_옆에_다른_user_메시지가_섞여도_채택하지_않는다(tmp_path: Path) -> None:
    rows = fixture_rows()
    rows.insert(5, text_row("user", "Stop hook feedback:\n철회", rows[4]["timestamp"]))
    put_transcript(tmp_path, rows)

    assert run(tmp_path).body == "[[침묵]]"


def test_수신자_표식이_없으면_침묵이_남는다(tmp_path: Path) -> None:
    rows = fixture_rows()
    draft = rows[3]["message"]["content"][0]
    draft["text"] = draft["text"].replace("[[RECIPIENT:BOT:U0BS6LBPHJ6]]", "")
    put_transcript(tmp_path, rows)

    assert run(tmp_path).body == "[[침묵]]"


def test_철회된_초안은_채택하지_않는다(tmp_path: Path) -> None:
    # 초안 바로 뒤에 Stop hook feedback 이 오면 그 초안은 철회된 것이다 (사건 transcript 의 첫 초안)
    rows = fixture_rows()
    retracted = [rows[0], rows[1], rows[2]]
    tail = [rows[4], rows[5], rows[6], rows[7]]
    put_transcript(tmp_path, retracted + tail)

    assert run(tmp_path).body == "[[침묵]]"


def test_앞_텍스트가_침묵이면_침묵이_남는다(tmp_path: Path) -> None:
    rows = fixture_rows()
    rows[3]["message"]["content"][0]["text"] = "[[침묵]]\n[[RECIPIENT:OWNER]]"
    put_transcript(tmp_path, rows)

    assert run(tmp_path).body == "[[침묵]]"


def test_침묵이_아닌_최종_답은_그대로다(tmp_path: Path) -> None:
    put_transcript(tmp_path, fixture_rows())
    eng = engine()
    response = eng.parse(single_dict_stdout("최종 답"), "", 0)

    assert eng.recover_silenced_answer(request(), response, {"HOME": str(tmp_path)}).body == "최종 답"


def test_transcript_가_없으면_침묵이_남는다(tmp_path: Path) -> None:
    assert run(tmp_path).body == "[[침묵]]"


def test_앞_턴의_위임_답은_마지막_프롬프트_뒤가_아니라서_채택하지_않는다(tmp_path: Path) -> None:
    rows = fixture_rows()
    rows.append(text_row("user", "다음 요청입니다", "2026-11-15T12:00:00.000Z"))
    rows.append(text_row("assistant", "[[침묵]]", "2026-11-15T12:00:05.000Z"))
    put_transcript(tmp_path, rows)

    assert run(tmp_path).body == "[[침묵]]"


def test_러너가_실행_뒤_transcript_복구를_거친다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from slack_cli_agent.engine.runner import EngineRunner

    put_transcript(tmp_path, fixture_rows())
    monkeypatch.setenv("HOME", str(tmp_path))

    class Done:
        stdout = single_dict_stdout()
        stderr = ""
        returncode = 0

    def fake_run(cmd: list[str], cwd: str, timeout: float, **_: Any) -> Done:
        return Done()

    response = EngineRunner(RuntimeSettings(), subprocess_runner=fake_run).run(engine(), request())

    assert response.ok
    assert "[[RECIPIENT:BOT:U0BS6LBPHJ6]]" in response.body
    assert response.raw[SILENCE_RECOVERED_RAW_KEY] is True


def test_dict_출력만으로는_parse_가_침묵을_바꾸지_않는다() -> None:
    # #226 의 배열 경로는 dict 출력에서 무동작이다. 복구는 transcript 경로가 맡는다.
    assert engine().parse(single_dict_stdout(), "", 0).body == "[[침묵]]"


def test_transcript_path_falls_back_to_realpath(tmp_path):
    """2026-11-15 E2E : CLI 는 심링크를 푼 실경로로 slug 를 만든다."""
    from slack_cli_agent.engine.transcript import ClaudeTranscriptReader
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home = tmp_path / "home"
    slug = str(real.resolve()).replace("/", "-").replace(".", "-")
    target = home / ".claude" / "projects" / slug / "s1.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n")
    assert ClaudeTranscriptReader(link, home=home).transcript_path("s1") == target
