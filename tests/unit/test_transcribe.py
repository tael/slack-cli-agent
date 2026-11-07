"""음성 첨부 받아쓰기 단위 시험 (G6, bot.py transcribe_audio 이식).

mlx-whisper 를 실제로 부르지 않는다. runner 자리에 가짜를 넣는다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from slack_cli_agent.auth.principal import Principal, TrustLevel
from slack_cli_agent.prompt.sections import AttachmentSection, CompositionContext
from slack_cli_agent.slack.attachments import AttachmentStore, DownloadResult
from slack_cli_agent.slack.transcribe import Transcriber


STRANGER = Principal(user_id="U9", channel="C1", trust=TrustLevel.GENERAL, is_direct_message=False)


def _engine(tmp_path: Path) -> Path:
    exe = tmp_path / "mlx_whisper"
    exe.write_text("#!/bin/sh\n")
    return exe


class _Runner:
    """subprocess.run 을 흉내내고, 성공이면 받아쓴 글 파일을 남긴다."""

    def __init__(self, text: str | None = "안녕 하세요  마메치", code: int = 0,
                 error: Exception | None = None, stderr: str = "") -> None:
        self.text, self.code, self.error, self.stderr = text, code, error, stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"args": args, **kwargs})
        if self.error is not None:
            raise self.error
        out = Path(args[args.index("-o") + 1])
        name = args[args.index("--output-name") + 1]
        if self.code == 0 and self.text is not None:
            (out / f"{name}.txt").write_text(self.text)
        return subprocess.CompletedProcess(args, self.code, "", self.stderr)


def test_받아쓴_글을_공백_정리해_돌려준다(tmp_path: Path) -> None:
    clip = tmp_path / "voice.m4a"
    clip.write_bytes(b"x")
    runner = _Runner()
    words, why = Transcriber(binary=_engine(tmp_path), runner=runner)(str(clip))
    assert (words, why) == ("안녕 하세요 마메치", "")
    call = runner.calls[0]
    assert "--language" in call["args"] and "ko" in call["args"]
    # 점을 넣으면 mlx-whisper 가 확장자로 보고 잘라낸다
    assert call["args"][call["args"].index("--output-name") + 1] == "voice_stt"
    assert call["stdin"] is subprocess.DEVNULL
    assert call["env"]["PATH"].startswith("/opt/homebrew/bin")


def test_엔진이_없으면_실패_사유를_돌려준다(tmp_path: Path) -> None:
    words, why = Transcriber(binary=tmp_path / "없음", runner=_Runner())(str(tmp_path / "a.m4a"))
    assert words is None and "받아쓰기 엔진이 없다" in why


def test_종료_코드가_0이_아니면_실패(tmp_path: Path) -> None:
    runner = _Runner(code=1, stderr="line1\nffmpeg not found")
    words, why = Transcriber(binary=_engine(tmp_path), runner=runner)(str(tmp_path / "a.m4a"))
    assert words is None and "종료 코드 1" in why and "ffmpeg not found" in why


def test_시간_초과는_실패(tmp_path: Path) -> None:
    runner = _Runner(error=subprocess.TimeoutExpired("x", 1))
    words, why = Transcriber(binary=_engine(tmp_path), runner=runner, timeout_sec=1)(
        str(tmp_path / "a.m4a"))
    assert words is None and "1초 안에 끝나지 않았다" in why


def test_빈_글은_실패(tmp_path: Path) -> None:
    runner = _Runner(text="  \n ")
    words, why = Transcriber(binary=_engine(tmp_path), runner=runner)(str(tmp_path / "a.m4a"))
    assert words is None and "말소리를 찾지 못했다" in why


def _store(tmp_path: Path, transcriber: Any) -> AttachmentStore:
    return AttachmentStore(
        attach_dir=tmp_path / "attach",
        token_provider=lambda: "xoxb",
        downloader=lambda url, token: DownloadResult("application/octet-stream", b"data"),
        transcriber=transcriber,
    )


def _event() -> dict[str, Any]:
    return {"ts": "1.0", "files": [
        {"name": "voice.m4a", "mimetype": "audio/mp4", "url_private": "https://s/1", "size": 4},
        {"name": "photo.png", "mimetype": "image/png", "url_private": "https://s/2", "size": 4},
    ]}


def test_음성_첨부만_받아쓴다(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake(path: str) -> tuple[str | None, str]:
        calls.append(path)
        return "회의 잡아 줘", ""

    files, missed = _store(tmp_path, fake).download(_event())
    assert missed == 0
    assert len(calls) == 1 and calls[0].endswith("voice.m4a")
    voice, photo = files
    assert voice["transcript"] == "회의 잡아 줘"
    assert "transcript" not in photo and "transcript_error" not in photo


def test_받아쓰기_실패는_사유를_싣는다(tmp_path: Path) -> None:
    files, _ = _store(tmp_path, lambda p: (None, "받아쓰기 실패, 종료 코드 1 : x")).download(_event())
    assert files[0]["transcript_error"] == "받아쓰기 실패, 종료 코드 1 : x"
    assert "transcript" not in files[0]


def test_받아쓰기_예외도_실패로_싣는다(tmp_path: Path) -> None:
    def boom(path: str) -> tuple[str | None, str]:
        raise RuntimeError("터짐")

    files, _ = _store(tmp_path, boom).download(_event())
    assert "받아쓰기를 실행하지 못했다" in files[0]["transcript_error"]


def test_받아쓰기기가_없으면_실패로_싣는다(tmp_path: Path) -> None:
    files, _ = _store(tmp_path, None).download(_event())
    assert "받아쓰기 엔진이 설정되지 않았다" in files[0]["transcript_error"]


def test_프롬프트에_받아쓴_말과_지시를_싣는다() -> None:
    ctx = CompositionContext(principal=STRANGER, files=(
        {"name": "voice.m4a", "mimetype": "audio/mp4", "local_path": "/a/voice.m4a",
         "transcript": "회의 잡아 줘"},
    ))
    text = AttachmentSection().render(ctx)
    assert "  - 받아쓴 말 : 회의 잡아 줘" in text
    assert "받아쓴 말을 그 사람의 요청으로 보고 답한다." in text
    assert "음성 파일은 열지 않는다." in text
    assert "> 들은 말 : <받아쓴 말>" in text
    # 음성만 있으면 "그 경로를 열어" 지시를 내지 않는다
    assert "그 경로를 열어" not in text


def test_프롬프트에_받아쓰기_실패를_싣는다() -> None:
    ctx = CompositionContext(principal=STRANGER, files=(
        {"name": "voice.m4a", "mimetype": "audio/mp4", "local_path": "/a/voice.m4a",
         "transcript_error": "받아쓴 글이 비어 있다"},
        {"name": "photo.png", "mimetype": "image/png", "local_path": "/a/photo.png"},
    ))
    text = AttachmentSection().render(ctx)
    assert "  - 받아쓰기 실패 : 받아쓴 글이 비어 있다" in text
    assert "내용을 짐작하지 않는다" in text
    assert "그 경로를 열어 내용을 직접 보고 답한다." in text
