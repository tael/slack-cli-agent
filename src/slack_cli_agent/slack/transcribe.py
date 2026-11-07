"""음성 첨부를 이 노트북에서 받아쓴다 (bot.py transcribe_audio 이식, G6).

모델은 소리를 듣지 못한다. 클립을 받아도 경로만 전달되면 내용을 짐작해
답한다. 받아쓰기는 mlx-whisper 로 로컬에서 하므로 음성이 밖으로 나가지 않는다.

launchd 가 주는 PATH 에는 ffmpeg 가 없어 mlx-whisper 가 파일을 열지 못한다.
그래서 실행 경로와 PATH 를 직접 준다.

실패하면 글은 None 이고 사유를 그대로 돌려준다. 받아쓴 척 지어내지 않는다.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
        name = Path(path).stem + "_stt"  # 점을 넣으면 mlx-whisper 가 확장자로 보고 잘라낸다
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
        txt = out / f"{name}.txt"
        if proc.returncode != 0 or not txt.exists():
            tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            return None, f"받아쓰기 실패, 종료 코드 {proc.returncode} : {tail[0]}"
        heard = " ".join(txt.read_text().split())
        if not heard:
            return None, "받아쓴 글이 비어 있다. 말소리를 찾지 못했다"
        return heard, ""
