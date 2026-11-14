"""음성 첨부를 이 노트북에서 받아쓴다 (bot.py transcribe_audio 이식, G6).

모델은 소리를 듣지 못한다. 클립을 받아도 경로만 전달되면 내용을 짐작해
답한다. 받아쓰기는 mlx-whisper 로 로컬에서 하므로 음성이 밖으로 나가지 않는다.

launchd 가 주는 PATH 에는 ffmpeg 가 없어 mlx-whisper 가 파일을 열지 못한다.
그래서 실행 경로와 PATH 를 직접 준다.

실패하면 글은 None 이고 사유를 그대로 돌려준다. 받아쓴 척 지어내지 않는다.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: (글, 실패 사유). 성공이면 사유는 빈 문자열, 실패면 글이 None 이다.
TranscribeResult = tuple[str | None, str]
TranscribeFn = Callable[[str], TranscribeResult]

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_TIMEOUT_SEC = 180
STT_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def is_audio(mimetype: str | None) -> bool:
    return (mimetype or "").startswith("audio/")


class Transcriber:
    def __init__(
        self,
        binary: Path | None = None,
        model: str = DEFAULT_MODEL,
        language: str = "ko",
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        # 홈은 호출 시점에 읽는다. 모듈을 읽는 시점에 고정하면 시험 격리가 바꾼 홈을 따르지 않는다.
        self._binary = binary
        self._model = model
        self._language = language
        self._timeout_sec = timeout_sec
        self._runner = runner

    @property
    def binary(self) -> Path:
        return self._binary or Path.home() / ".local/bin/mlx_whisper"

    def __call__(self, path: str) -> TranscribeResult:
        exe = self.binary
        if not exe.exists():
            return None, f"받아쓰기 엔진이 없다 : {exe}"
        # 첨부와 같은 디렉터리에 둔다. 하위 디렉터리에 두면 첨부 정리가 지우지 못한다.
        out = Path(path).parent
        # 점을 넣으면 mlx-whisper 가 확장자로 보고 잘라낸다. 확장자까지 이름에 넣는다.
        # stem 만 쓰면 한 메시지의 voice.m4a 와 voice.webm 이 같은 글 파일을 쓴다.
        name = Path(path).name.replace(".", "_") + "_stt"
        txt = out / f"{name}.txt"
        # 재시도나 되짚기가 같은 ts 디렉터리에 다시 받으면 지난 실행의 글 파일이
        # 남아 있다. 엔진이 글을 다시 쓰지 않고 0 으로 끝나면 그 글을 이번 결과로
        # 읽게 되므로 먼저 지운다.
        try:
            txt.unlink(missing_ok=True)
        except OSError as exc:
            return None, f"지난 받아쓰기 결과를 지우지 못했다 : {exc}"
        env = dict(os.environ, PATH=STT_PATH + ":" + os.environ.get("PATH", ""))
        try:
            proc = self._runner(
                [str(exe), str(path), "--model", self._model, "--language", self._language,
                 "-f", "txt", "-o", str(out), "--output-name", name, "--verbose", "False"],
                capture_output=True, text=True, timeout=self._timeout_sec, env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return None, f"받아쓰기가 {self._timeout_sec}초 안에 끝나지 않았다"
        except Exception as exc:  # noqa: BLE001 - 실패 사유를 그대로 프롬프트에 싣는다
            return None, f"받아쓰기를 실행하지 못했다 : {exc}"
        if proc.returncode != 0 or not txt.exists():
            tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            return None, f"받아쓰기 실패, 종료 코드 {proc.returncode} : {tail[0]}"
        heard = " ".join(txt.read_text().split())
        if not heard:
            return None, "받아쓴 글이 비어 있다. 말소리를 찾지 못했다"
        return heard, ""


def transcribe_files(
    files: Sequence[Mapping[str, Any]], transcriber: TranscribeFn | None
) -> tuple[Mapping[str, Any], ...]:
    """저장된 음성 첨부에 `transcript` 또는 `transcript_error` 를 붙여 돌려준다.

    워커가 큐에서 꺼낸 뒤 부른다. 접수 단계(소켓 핸들러 스레드)에서 부르면
    클립마다 최대 180초 동안 접수 표식이 늦고 뒤 이벤트가 밀린다. 마메치도
    큐에 넣은 뒤 handle_request 에서 받아쓴다(bot.py:7861).

    실패도 사유를 그대로 실어 프롬프트가 실패를 밝히게 한다.
    """
    out: list[Mapping[str, Any]] = []
    for f in files:
        path = f.get("local_path")
        if not path or not is_audio(f.get("mimetype")) or "transcript" in f or "transcript_error" in f:
            out.append(f)
            continue
        heard = dict(f)
        if transcriber is None:
            heard["transcript_error"] = "받아쓰기 엔진이 설정되지 않았다"
            out.append(heard)
            continue
        t0 = time.time()
        try:
            words, why = transcriber(str(path))
        except Exception as exc:  # noqa: BLE001 - 한 클립 실패가 요청 전체를 막지 않는다
            words, why = None, f"받아쓰기를 실행하지 못했다 : {exc}"
        log.info("음성을 받아썼다 : %s %.1f초 %s", f.get("name"),
                 time.time() - t0, "성공" if words else why)
        if words:
            heard["transcript"] = words
        else:
            heard["transcript_error"] = why or "받아쓰기 실패"
        out.append(heard)
    return tuple(out)
